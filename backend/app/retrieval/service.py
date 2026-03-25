from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, Settings, settings
from ..database import RELATIONS, SCHEMA, physical_table_name
from ..errors import PipelineStageError
from ..model_client import ModelClient
from ..querying.duckdb_engine import DuckDbEngine
from ..security import AccessScope
from .store import FieldDocument, LocalSchemaStore


SYNONYMS = {
    "outstanding_principal": ["贷款余额", "本金余额", "未偿本金"],
    "overdue_principal": ["逾期应还本金"],
    "dpd": ["逾期天数"],
    "disbursed_on": ["放款日期"],
    "snapshot_on": ["观察日", "月末", "日终"],
    "principal_paid": ["归还本金", "实还本金"],
    "decision": ["审批结果"],
}


class SchemaIndex:
    """字段级混合检索服务。"""

    def __init__(
        self,
        model_client: ModelClient | None = None,
        config: Settings | None = None,
        index_path: Path | None = None,
    ) -> None:
        self.config = config or settings
        self.model_client = model_client or ModelClient(self.config)
        self.index_path = index_path or BASE_DIR / "data" / "credit_schema_store.json"
        self.store = LocalSchemaStore(self.index_path)
        self.embedding_source = "not_built"
        self.last_build_error: str | None = None

    @property
    def documents(self) -> list[FieldDocument]:
        return self.store.active_documents()

    def ensure_built(self, *, force: bool = False) -> None:
        signature = self._schema_signature()
        expected_source = (
            "demo_local_hash"
            if self.config.credit_demo_mode
            else self.config.embedding_model
        )
        if not force and self.store.load(signature):
            source = self.store.embedding_source
            # 仅加载由当前 Embedding 模型生成的索引。
            if source == expected_source and (
                self.model_client.enabled or self.config.credit_demo_mode
            ):
                self.embedding_source = source
                return

        raw_documents = self._build_raw_documents()
        semantic_texts = [item["semantic_text"] for item in raw_documents]
        try:
            vectors = self.model_client.embed(semantic_texts)
            self.embedding_source = expected_source
            self.last_build_error = None
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            self.last_build_error = str(exc)
            raise PipelineStageError("schema_embedding", str(exc)) from exc

        documents: list[FieldDocument] = []
        for raw, vector in zip(raw_documents, vectors):
            content_hash = hashlib.sha256(
                json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            documents.append(
                FieldDocument(
                    **raw,
                    dense_vector=vector,
                    schema_version=signature[:12],
                    content_hash=content_hash,
                )
            )
        self.store.replace_all(signature, documents, self.embedding_source)

    def retrieve(
        self,
        query: str,
        *,
        retrieval_terms: list[str] | None = None,
        threshold: float | None = None,
        access_scope: AccessScope | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_built()
        terms = list(dict.fromkeys(retrieval_terms or []))
        retrieval_text = " ".join(terms).strip()
        recall_threshold = max(
            0.0,
            min(
                1.0,
                threshold
                if threshold is not None
                else self.config.schema_recall_threshold,
            ),
        )

        scope = (
            access_scope
            if isinstance(access_scope, AccessScope)
            else AccessScope.from_dict(access_scope)
            if access_scope is not None
            else None
        )
        allowed_doc_ids = (
            {
                document.doc_id
                for document in self.documents
                if scope and scope.allows_field(document.table_id, document.field_name)
            }
            if scope
            else None
        )
        if allowed_doc_ids == set():
            return self._empty_retrieval(query, terms, recall_threshold)

        # BM25 与向量召回使用同一组检索词。
        bm25 = (
            self.store.bm25_search(
                retrieval_text,
                self.config.bm25_top_k,
                allowed_doc_ids,
            )
            if retrieval_text
            else []
        )
        dense = (
            self.store.dense_search(
                self._query_vector(retrieval_text),
                self.config.dense_top_k,
                allowed_doc_ids,
            )
            if retrieval_text
            else []
        )
        bm25_rank = {index: rank for rank, (index, _) in enumerate(bm25, 1)}
        dense_rank = {index: rank for rank, (index, _) in enumerate(dense, 1)}
        bm25_scores = dict(bm25)
        dense_scores = dict(dense)

        fused: dict[int, float] = {}
        for rank_map in (bm25_rank, dense_rank):
            for index, rank in rank_map.items():
                fused[index] = fused.get(index, 0.0) + 1 / (60 + rank)
        rrf = sorted(fused.items(), key=lambda item: item[1], reverse=True)[
            : self.config.rrf_top_k
        ]
        candidate_indexes = [index for index, _ in rrf]

        if not candidate_indexes:
            return self._empty_retrieval(query, terms, recall_threshold)

        rerank_source = (
            "demo_lexical" if self.config.credit_demo_mode else self.config.rerank_model
        )
        try:
            texts = [self.documents[index].rerank_text for index in candidate_indexes]
            # Re-rank against the extracted schema intent as well: long questions contain
            # formatting/ranking instructions that are unrelated to any individual field.
            result = self.model_client.rerank(
                retrieval_text or query, texts, len(texts)
            )
            rerank_scores = {
                candidate_indexes[local_index]: max(0.0, min(1.0, float(score)))
                for local_index, score in result
                if 0 <= local_index < len(candidate_indexes)
            }
            if not rerank_scores:
                raise RuntimeError("Rerank返回为空")
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise PipelineStageError("schema_rerank", str(exc)) from exc

        # Rerank 分数低于阈值的字段不进入 Schema 图。
        ranked = sorted(
            ((index, rerank_scores.get(index, 0.0)) for index in candidate_indexes),
            key=lambda item: item[1],
            reverse=True,
        )
        selected = [item for item in ranked if item[1] >= recall_threshold][
            : self.config.max_schema_fields
        ]

        hits = [
            self._hit(
                index, score, bm25_rank, dense_rank, bm25_scores, dense_scores, fused
            )
            for index, score in selected
        ]
        low_confidence = [
            {
                "doc_id": self.documents[index].doc_id,
                "table_id": self.documents[index].table_id,
                "table_label": self.documents[index].table_label,
                "field_name": self.documents[index].field_name,
                "field_label": self.documents[index].field_label,
                "score": round(float(score), 6),
            }
            for index, score in ranked[:8]
        ]
        return {
            "query": query,
            "retrieval_terms": terms,
            "rerank_query": retrieval_text or query,
            "embedding_source": self.embedding_source,
            "rerank_source": rerank_source,
            "threshold": recall_threshold,
            "bm25_count": len(bm25),
            "dense_count": len(dense),
            "rrf_count": len(candidate_indexes),
            "candidate_count": len(candidate_indexes),
            "selected_count": len(hits),
            "hits": hits,
            "table_candidates": self._table_candidates(hits),
            "low_confidence_candidates": low_confidence,
        }

    def include_workspace(
        self,
        retrieval: dict[str, Any],
        workspace: dict[str, Any],
        access_scope: AccessScope | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """将用户确认的字段加入检索结果。"""
        scope = (
            access_scope
            if isinstance(access_scope, AccessScope)
            else AccessScope.from_dict(access_scope)
            if access_scope is not None
            else None
        )
        confirmed_tables = set(workspace.get("confirmed_schema_tables") or [])
        confirmed_fields = {
            (item.get("tableId"), item.get("name"))
            for item in workspace.get("schema_fields", [])
            if item.get("name")
        }
        hits = list(retrieval.get("hits", []))
        known = {hit["doc_id"] for hit in hits}
        for document in self.documents:
            explicit_field = (
                document.table_id,
                document.field_name,
            ) in confirmed_fields
            legacy_field = (None, document.field_name) in confirmed_fields
            if (
                document.table_id not in confirmed_tables
                and not explicit_field
                and not legacy_field
            ):
                continue
            if scope and not scope.allows_field(document.table_id, document.field_name):
                if document.table_id in confirmed_tables or explicit_field:
                    raise PipelineStageError(
                        "schema_access_control",
                        "用户确认内容包含无权访问的Schema字段",
                    )
                continue
            if document.doc_id in known:
                hit = next(hit for hit in hits if hit["doc_id"] == document.doc_id)
                hit.update(
                    {
                        "source": "user_confirmed",
                        "selection_reason": "user_confirmed",
                        "score": 1.0,
                        "rerank_score": 1.0,
                    }
                )
                continue
            hits.append(
                {
                    **document.public(),
                    "score": 1.0,
                    "bm25_rank": None,
                    "dense_rank": None,
                    "rrf_score": 0.0,
                    "rerank_score": 1.0,
                    "keyword_score": 0.0,
                    "vector_score": 0.0,
                    "selection_reason": "user_confirmed",
                    "source": "user_confirmed",
                }
            )
            known.add(document.doc_id)
        retrieval["hits"] = hits
        retrieval["selected_count"] = len(hits)
        retrieval["table_candidates"] = self._table_candidates(hits)
        return retrieval

    def _empty_retrieval(
        self,
        query: str,
        terms: list[str],
        threshold: float,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "retrieval_terms": terms,
            "embedding_source": self.embedding_source,
            "rerank_source": self.config.rerank_model,
            "threshold": threshold,
            "bm25_count": 0,
            "dense_count": 0,
            "rrf_count": 0,
            "candidate_count": 0,
            "selected_count": 0,
            "hits": [],
            "table_candidates": [],
            "low_confidence_candidates": [],
        }

    def status(self) -> dict[str, Any]:
        return {
            "built": bool(self.documents),
            "document_count": len(self.documents),
            "embedding_source": self.embedding_source,
            "last_build_error": self.last_build_error,
            "threshold": self.config.schema_recall_threshold,
            "store": self.store.status(),
        }

    def _build_raw_documents(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        # 共用索引只保存元数据，避免区域角色通过样例/统计分布看到其他机构数据。
        for table in SCHEMA:
            self._append_table_documents(output, table)
        return output

    def _append_table_documents(
        self, output: list[dict[str, Any]], table: dict[str, Any]
    ) -> None:
        """仅根据受控金融 Schema 元数据构建索引，不抽取客户或业务行。"""
        sql_table_name = physical_table_name(table)
        for field in table["fields"]:
            # Metadata only. Sensitive field descriptions may be retrieved by admins;
            # every recall/rerank candidate is filtered by AccessScope before use.
            samples = []
            profile = f"声明类型 {field['source_type']}；表粒度 {table['grain']}"
            aliases = list(
                dict.fromkeys(
                    [*field.get("aliases", []), *SYNONYMS.get(field["name"], [])]
                )
            )
            samples_text = "、".join(str(item) for item in samples)
            relations = [
                f"{item['left_table']}.{item['left_field']}={item['right_table']}.{item['right_field']}"
                for item in RELATIONS
                if table["id"] in {item["left_table"], item["right_table"]}
                and field["name"] in {item["left_field"], item["right_field"]}
            ]
            default_keyword_text = " ".join(
                [
                    table.get("database", "credit_ops"),
                    table.get("domain", ""),
                    table["id"],
                    sql_table_name,
                    table["label"],
                    *table.get("business_terms", []),
                    field["name"],
                    field["label"],
                    field.get("description", ""),
                    *aliases,
                    samples_text,
                ]
            )
            default_semantic_text = (
                f"{field['label']}（{field['name']}）属于{table['label']}。"
                f"表用途：{table['description']}。字段含义：{field.get('description', '')}。"
                f"业务表达：{'、'.join(aliases)}。数据特征：{profile}。样例：{samples_text}。"
            )
            default_rerank_text = (
                f"数据库字段 {sql_table_name}.{field['name']}；中文名：{field['label']}；"
                f"字段含义：{field.get('description', '')}；角色：{field.get('role', '')}；"
                f"所属表：{table['label']}；表业务：{table['description']}；"
                f"同义词：{'、'.join(aliases)}；样例：{samples_text}；分布：{profile}；"
                f"关联：{'、'.join(relations) or '无'}。"
            )
            index_content = field.get("index_content") or {}
            keyword_text = str(
                index_content.get("keyword_text") or default_keyword_text
            )
            semantic_text = str(
                index_content.get("vector_text") or default_semantic_text
            )
            rerank_text = str(index_content.get("rerank_text") or default_rerank_text)
            output.append(
                {
                    "doc_id": f"{table['id']}.{field['name']}",
                    "database_id": table.get("database", "credit_ops"),
                    "table_id": table["id"],
                    "table_label": table["label"],
                    "field_name": field["name"],
                    "field_label": field["label"],
                    "field_type": field["type"],
                    "field_description": field.get("description", ""),
                    "field_role": field.get("role", ""),
                    "aliases": aliases,
                    "samples": samples,
                    "profile": profile,
                    "keyword_text": keyword_text,
                    "semantic_text": semantic_text,
                    "rerank_text": rerank_text,
                    "relation_ids": relations,
                }
            )

    def _query_vector(self, text: str) -> list[float]:
        try:
            return self.model_client.embed([text])[0]
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise PipelineStageError("query_embedding", str(exc)) from exc

    def _hit(
        self,
        index: int,
        score: float,
        bm25_rank: dict[int, int],
        dense_rank: dict[int, int],
        bm25_scores: dict[int, float],
        dense_scores: dict[int, float],
        fused: dict[int, float],
    ) -> dict[str, Any]:
        return {
            **self.documents[index].public(),
            "score": round(float(score), 6),
            "rerank_score": round(float(score), 6),
            "bm25_rank": bm25_rank.get(index),
            "dense_rank": dense_rank.get(index),
            "rrf_score": round(fused.get(index, 0.0), 8),
            "keyword_score": round(bm25_scores.get(index, 0.0), 6),
            "vector_score": round(dense_scores.get(index, 0.0), 6),
            "selection_reason": "rerank_threshold",
            "source": "retrieval",
        }

    @staticmethod
    def _table_candidates(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for hit in hits:
            item = grouped.setdefault(
                hit["table_id"],
                {
                    "table_id": hit["table_id"],
                    "table_label": hit["table_label"],
                    "score": 0.0,
                    "field_count": 0,
                },
            )
            item["score"] = max(item["score"], float(hit.get("score", 0)))
            item["field_count"] += 1
        return sorted(grouped.values(), key=lambda item: item["score"], reverse=True)

    @staticmethod
    def _schema_signature() -> str:
        payload = json.dumps(
            {
                "store_version": "credit_open_metadata_only_v3",
                "data_version": DuckDbEngine().manifest()["data_version"],
                "schema": SCHEMA,
                "relations": RELATIONS,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
