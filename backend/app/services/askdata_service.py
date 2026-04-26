from __future__ import annotations
import uuid
from datetime import datetime, timezone
from langgraph.types import Command
from ..config import settings
from ..model_client import ModelClient
from ..models import QueryResult
from ..retrieval import SchemaIndex
from ..security import AccessController
from ..workflows.query_graph import QueryWorkflow
from ..skills import SkillRegistry
from ..credit.metrics import metric_catalog
from .session_archive import SessionArchive
from .session_context import SessionContext


class AskDataService:
    def __init__(self, model_client=None, schema_index=None, config=None):
        self.config = config or (schema_index.config if schema_index else settings)
        self.model_client = model_client or ModelClient(self.config)
        self.schema_index = schema_index or SchemaIndex(self.model_client, self.config)
        self.access_controller = AccessController()
        self.archive = SessionArchive(self.config.session_archive_file)
        self.context = SessionContext(self.archive)
        self.workflow = QueryWorkflow(self.model_client, self.schema_index, self.config)
        self.skills = SkillRegistry()

    def submit(self, query, session_id, workspace=None, user_id=None):
        scope = self.access_controller.resolve(user_id)
        if not scope.roles:
            raise PermissionError("未授权用户")
        history = self.archive.list_session(scope.user_id, session_id)
        if history and history[-1]["result"]["status"] == "waiting_clarification":
            option = self.context.match_clarification(query, history[-1]["result"])
            if option:
                return self.clarify(
                    history[-1]["task_id"], option, user_id=scope.user_id
                )
        task_id = uuid.uuid4().hex[:16]
        workspace = self.context.normalize_workspace(workspace)
        payload = {
            "task_id": task_id,
            "query": query.strip(),
            "session_id": session_id,
            "access_scope": scope.public(),
            "previous_plan": self.context.previous_plan(
                scope.user_id, session_id, scope
            ),
            "recent_context": self.context.recent_context(
                scope.user_id, session_id, scope
            ),
            "workspace": workspace,
            "confirmed": {},
            "result": {},
        }
        try:
            # Sources are loaded only for QA after intent resolution; a stale selection must not silently leak data.
            decision = self.workflow.prepare_request(payload)
            payload["decision"] = decision
            if decision["action"] == "data_qa":
                payload["sources"] = self.context.sources(
                    scope.user_id, session_id, workspace, scope
                )
            result = self.workflow.invoke(payload, task_id)
        except Exception as exc:
            result = self._failed(task_id, exc)
        self._save(payload, result)
        return result

    @staticmethod
    def _failed(task_id, exc):
        return QueryResult(
            task_id=task_id,
            status="failed",
            route="database_query",
            message="查询未完成",
            analysis=str(exc),
            validation={"passed": False},
            execution_log=[
                {"stage": "credit_workflow", "success": False, "error": str(exc)}
            ],
        )

    def _save(self, payload, result):
        task = {
            "task_id": payload["task_id"],
            "user_id": payload["access_scope"]["user_id"],
            "session_id": payload["session_id"],
            "query": payload["query"],
            "payload": payload,
            "result": result.model_dump(mode="json"),
        }
        self.archive.save_task(task)

    def clarify(self, task_id, option_id, user_message=None, user_id=None):
        scope = self.access_controller.resolve(user_id)
        task = self.archive.get_task(task_id, scope.user_id)
        current = task["result"]
        c = current.get("clarification")
        if (
            current["status"] != "waiting_clarification"
            or not c
            or option_id not in {o["id"] for o in c["options"]}
        ):
            raise ValueError("无效或已过期的澄清选项")
        payload = task["payload"]
        payload["access_scope"] = scope.public()
        payload["confirmed"] = {
            **payload.get("confirmed", {}),
            c["parameter"]: self.workflow.confirmed_value(c, option_id),
        }
        payload["result"] = {}
        try:
            snapshot = self.workflow.graph.get_state(self.workflow.run_config(task_id))
            if snapshot.values:
                self.workflow.graph.update_state(
                    self.workflow.run_config(task_id), {"access_scope": scope.public()}
                )
                result = self.workflow.invoke(
                    Command(resume={"option_id": option_id}), task_id
                )
            else:
                # Rebuild pending work from persisted intent after process restart; no lost ownership/default user.
                result = self.workflow.invoke(payload, task_id)
        except Exception as exc:
            result = self._failed(task_id, exc)
        self._save(payload, result)
        return result

    def get_task(self, task_id, user_id=None):
        scope = self.access_controller.resolve(user_id)
        task = self.archive.get_task(task_id, scope.user_id)
        if task["payload"]["access_scope"] != scope.public():
            raise PermissionError("权限已变化，请重新查询")
        return QueryResult.model_validate(task["result"])

    def save_memory(self, task_id, user_id=None):
        scope = self.access_controller.resolve(user_id)
        r = self.get_task(task_id, scope.user_id)
        if r.status != "completed" or not r.query_plan:
            raise ValueError("只能保存已完成的数据库查询")
        self.archive.save_result(task_id, scope.user_id)

    def list_memories(self, user_id=None):
        scope = self.access_controller.resolve(user_id)
        items = self.archive.list_saved(scope.user_id)
        out = []
        for item in items:
            if item["kind"] == "result_table":
                try:
                    self.get_task(item["task_id"], scope.user_id)
                except (PermissionError, KeyError):
                    continue
            elif not scope.allows_field(item["table_id"], item["name"]):
                continue
            out.append(item)
        return out

    def delete_memory(self, memory_id, user_id=None):
        self.archive.delete_memory(
            memory_id, self.access_controller.resolve(user_id).user_id
        )

    def save_field_memory(self, table_id, name, label, field_type, user_id=None):
        scope = self.access_controller.resolve(user_id)
        if not scope.allows_field(table_id, name):
            raise PermissionError("无权访问字段")
        tables = self.visible_schema(scope.user_id)
        if not any(
            t["id"] == table_id and any(f["name"] == name for f in t["fields"])
            for t in tables
        ):
            raise ValueError("字段不存在")
        self.archive.save_field(
            {
                "id": f"field:{table_id}.{name}",
                "user_id": scope.user_id,
                "kind": "schema_field",
                "table_id": table_id,
                "name": name,
                "label": label,
                "field_type": field_type,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def visible_schema(self, user_id=None):
        return self.access_controller.filter_schema(
            self.access_controller.resolve(user_id)
        )

    def search_schema(self, query, threshold=None, user_id=None):
        return self.schema_index.retrieve(
            query,
            retrieval_terms=[query],
            threshold=threshold,
            access_scope=self.access_controller.resolve(user_id),
        )

    def rebuild_schema_index(self):
        self.schema_index.ensure_built(force=True)
        return self.schema_index.status()

    def list_mcp_tools(self, user_id=None):
        return self.workflow.mcp_client(
            self.access_controller.resolve(user_id).public()
        ).list_tools()

    def list_skills(self):
        return self.skills.list()

    def status(self):
        return {
            **self.config.public_status(),
            "scenario": "零售信贷经营分析与贷后风险指标查询 Agent",
            "dataset": self.workflow.database_engine.manifest(),
            "metrics": metric_catalog(),
            "workflow_engine": "langgraph",
            "schema_index": self.schema_index.status(),
        }
