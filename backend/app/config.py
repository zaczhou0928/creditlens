from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """加载项目内 .env，不覆盖系统已经提供的变量。"""
    env_path = path or BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


@dataclass(frozen=True)
class Settings:
    external_model_enabled: bool = os.getenv(
        "CREDIT_EXTERNAL_MODEL_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    credit_demo_mode: bool = os.getenv("CREDIT_DEMO_MODE", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    credit_reference_date: str = os.getenv("CREDIT_REFERENCE_DATE", "")
    api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv(
        "LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).rstrip("/")
    llm_model: str = os.getenv("LLM_MODEL", "qwen-plus")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "").rstrip("/")
    rerank_model: str = os.getenv("RERANK_MODEL", "qwen3-rerank")
    rerank_base_url: str = os.getenv(
        "RERANK_BASE_URL", "https://dashscope.aliyuncs.com/compatible-api/v1"
    ).rstrip("/")
    timeout: int = int(os.getenv("LLM_TIMEOUT", "60"))
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "2"))
    schema_recall_threshold: float = float(os.getenv("SCHEMA_RECALL_THRESHOLD", "0.55"))
    bm25_top_k: int = int(os.getenv("BM25_TOP_K", "30"))
    dense_top_k: int = int(os.getenv("DENSE_TOP_K", "30"))
    rrf_top_k: int = int(os.getenv("RRF_TOP_K", "40"))
    max_schema_fields: int = int(os.getenv("MAX_SCHEMA_FIELDS", "20"))
    session_archive_path: str = os.getenv(
        "CREDIT_ARCHIVE_PATH", str(BASE_DIR / "data" / "credit_sessions.db")
    )

    @property
    def session_archive_file(self) -> Path:
        path = Path(self.session_archive_path)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def chat_url(self) -> str:
        return f"{self.llm_base_url}/chat/completions"

    @property
    def embeddings_url(self) -> str:
        base = self.embedding_base_url or self.llm_base_url
        return f"{base}/embeddings"

    @property
    def rerank_url(self) -> str:
        return f"{self.rerank_base_url}/reranks"

    def public_status(self) -> dict[str, object]:
        return {
            "api_key_configured": bool(self.api_key),
            "llm_model": self.llm_model,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "rerank_model": self.rerank_model,
            "semantic_fallbacks": False,
            "credit_demo_mode": self.credit_demo_mode,
            "external_model_enabled": self.external_model_enabled,
            "query_mode": "offline_metric_demo"
            if self.credit_demo_mode
            else "open_model_agent",
            "credit_reference_date": self.credit_reference_date or "真实业务日期",
            "result_source": "server_verified_only",
            "schema_recall_threshold": self.schema_recall_threshold,
            "bm25_top_k": self.bm25_top_k,
            "dense_top_k": self.dense_top_k,
            "rrf_top_k": self.rrf_top_k,
            "max_schema_fields": self.max_schema_fields,
        }


settings = Settings()
