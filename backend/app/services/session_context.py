from __future__ import annotations
import json
from ..models import QueryResult


class SessionContext:
    """只从后端取回原始结果，不信任客户端上传的历史表格内容。"""

    def __init__(self, archive):
        self.archive = archive

    @staticmethod
    def normalize_workspace(workspace):
        raw = workspace or {}
        return {
            "schema_fields": list(raw.get("schema_fields") or []),
            "analysis_table_ids": list(
                dict.fromkeys(raw.get("analysis_table_ids") or [])
            )[:5],
        }

    def previous_plan(self, user_id, session_id, scope):
        for task in reversed(self.archive.list_session(user_id, session_id)):
            r = task["result"]
            if (
                r["status"] == "completed"
                and r.get("query_plan")
                and r.get("scope_fingerprint") == scope.fingerprint()
            ):
                return r["query_plan"]
        return None

    def recent_context(self, user_id, session_id, scope):
        turns = []
        for task in self.archive.list_session(user_id, session_id)[-6:]:
            if task["payload"]["access_scope"] != scope.public():
                continue
            r = task["result"]
            turns.append(
                {
                    "query": task["query"],
                    "status": r["status"],
                    "standalone_query": r.get("standalone_query"),
                    "interpretation": r.get("interpretation"),
                    "sql": r.get("sql"),
                    "clarification": r.get("clarification"),
                    "confirmed": task["payload"].get("confirmed", {}),
                    "result_task_id": task["task_id"],
                    "columns": r.get("columns", []),
                }
            )
        return json.dumps(turns, ensure_ascii=False)

    def sources(self, user_id, session_id, workspace, scope):
        ids = workspace.get("analysis_table_ids", [])
        if not ids:
            tasks = self.archive.list_session(user_id, session_id)
            ids = [
                t["task_id"]
                for t in reversed(tasks)
                if t["result"]["status"] == "completed"
                and t["result"].get("query_plan")
            ][:1]
        results = []
        for task_id in ids:
            task = self.archive.get_task(task_id, user_id)
            result = QueryResult.model_validate(task["result"])
            if result.status != "completed" or not result.query_plan:
                raise ValueError("所选来源不是已完成的数据库查询")
            if result.scope_fingerprint != scope.fingerprint():
                raise PermissionError("查询来源权限已变化，请重新查询")
            results.append(result.model_dump(mode="json"))
        return results

    @staticmethod
    def match_clarification(text, result):
        c = result.get("clarification") or {}
        normalized = text.strip()
        for option in c.get("options", []):
            if normalized in {option["id"], option["label"]}:
                return option["id"]
        return None
