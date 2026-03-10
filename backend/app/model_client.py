from __future__ import annotations

import json
import hashlib
import math
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

import certifi

from .config import Settings, settings


class ModelClient:
    """Chat、Embedding 和 Rerank 接口客户端。"""

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        self._blocked_until: dict[str, float] = {}
        self._ssl_context = ssl.create_default_context()
        # 部分 macOS Python 安装没有系统根证书，使用可信 CA 包补齐，保持 TLS 校验。
        if not self._ssl_context.get_ca_certs():
            self._ssl_context.load_verify_locations(cafile=certifi.where())

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature
            if temperature is None
            else temperature,
            "stream": False,
        }
        result = self._post(self.config.chat_url, payload)
        try:
            return str(result["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("大模型响应缺少 message.content") from exc

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if not match:
                raise RuntimeError("大模型未返回有效 JSON")
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise RuntimeError("大模型未返回有效 JSON") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.config.credit_demo_mode:
            vectors = []
            for text in texts:
                vector = [0.0] * 128
                for i in range(max(1, len(text) - 1)):
                    token = text[i : i + 2]
                    vector[
                        int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 128
                    ] += 1
                vectors.append(self._normalize(vector))
            return vectors
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 10):
            payload = {
                "model": self.config.embedding_model,
                "input": texts[start : start + 10],
                "dimensions": self.config.embedding_dimensions,
                "encoding_format": "float",
            }
            result = self._post(self.config.embeddings_url, payload)
            items = sorted(
                result.get("data", []), key=lambda item: item.get("index", 0)
            )
            vectors.extend(self._normalize(item["embedding"]) for item in items)
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding 返回数量与输入不一致")
        return vectors

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        if self.config.credit_demo_mode:
            tokens = set(query[i : i + 2] for i in range(max(1, len(query) - 1)))
            return sorted(
                [
                    (
                        i,
                        min(
                            1.0,
                            0.4 + sum(t in doc for t in tokens) / max(1, len(tokens)),
                        ),
                    )
                    for i, doc in enumerate(documents)
                ],
                key=lambda x: x[1],
                reverse=True,
            )[:top_n]
        if not documents:
            return []
        payload = {
            "model": self.config.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "instruct": "Rank database schema fields by relevance to the user's analytics query.",
        }
        result = self._post(self.config.rerank_url, payload)
        output = [
            (
                int(item["index"]),
                float(item.get("relevance_score", item.get("score", 0))),
            )
            for item in result.get("results", [])
        ]
        return sorted(output, key=lambda item: item[1], reverse=True)

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.config.credit_demo_mode or not self.config.external_model_enabled:
            raise RuntimeError(
                "外部模型请求未启用。确认发送目的地及问题、Schema、授权查询结果后，显式设置CREDIT_EXTERNAL_MODEL_ENABLED=true。"
            )
        if not self.config.api_key:
            raise RuntimeError("LLM_API_KEY 尚未配置")
        if time.monotonic() < self._blocked_until.get(url, 0):
            raise RuntimeError("模型接口暂时处于30秒熔断窗口，请稍后重试")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout, context=self._ssl_context
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = RuntimeError(f"模型接口返回 HTTP {exc.code}: {detail}")
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.config.max_retries:
                time.sleep(0.4 * (2**attempt))
        # 网络请求失败后对当前接口启用短时熔断。
        self._blocked_until[url] = time.monotonic() + 30
        raise RuntimeError(f"模型接口调用失败: {last_error}") from last_error

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [float(value) / norm for value in vector]
