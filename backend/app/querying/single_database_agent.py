"""模型基于检索上下文生成SQL并使用MCP；保留显式离线口径回归入口。"""

import json
from fnmatch import fnmatch

from ..models import Clarification

from ..querying.models import SqlExecution


class SingleDatabaseAgent:
    def __init__(self, client_factory, model_client=None, skill=None):
        self.client_factory = client_factory
        self.model = model_client
        self.skill = skill

    def run(self, state, manifest, reference_date):
        client = self.client_factory(state["access_scope"])
        tools = [
            t
            for t in client.list_tools()
            if any(fnmatch(t["name"], pattern) for pattern in self.skill.allowed_tools)
        ]
        names = {t["name"] for t in tools}
        observations, trace = [], list(state.get("agent_trace", []))
        for call_index in range(1, self.skill.max_tool_calls + 1):
            decision = self.model.chat_json(
                self.skill.instructions,
                json.dumps(
                    {
                        "query": state["query"],
                        "standalone_query": state["decision"].get("standalone_query"),
                        "recent_context": state.get("recent_context", ""),
                        "confirmed_parameters": state.get("confirmed", {}),
                        "schema_graph": state["schema_graph"],
                        "retrieval": state["retrieval"],
                        "knowledge": state["knowledge"],
                        "dataset": manifest,
                        "reference_date": str(reference_date),
                        "mcp_tools": tools,
                        "tool_results": observations,
                    },
                    ensure_ascii=False,
                ),
            )
            action = decision.get("action")
            if action == "clarify":
                c = Clarification.model_validate(decision.get("clarification"))
                if not 2 <= len(c.options) <= 5 or len(
                    {o.id for o in c.options}
                ) != len(c.options):
                    raise ValueError("澄清须提供2至5个不同选项")
                if c.parameter in state.get("confirmed", {}):
                    raise ValueError("模型重复请求已确认的参数，请补充问题描述")
                return {
                    "action": "clarify",
                    "clarification": c.model_dump(),
                    "trace": trace,
                }
            if action == "answer":
                return {
                    "action": "answer",
                    "answer": str(decision.get("answer") or "现有数据不足以回答。"),
                    "trace": trace,
                }
            if action != "call_tool" or decision.get("tool_name") not in names:
                raise ValueError("模型选择了未提供的动作或工具")
            name, arguments = decision["tool_name"], decision.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是对象")
            has_schema = bool(state["schema_graph"].get("fields")) or any(
                o["tool"] == "search_credit_schema"
                and o["result"]["schema_graph"].get("fields")
                for o in observations
            )
            if name == "query_credit_ops" and not has_schema:
                result = {
                    "sql": arguments.get("sql"),
                    "success": False,
                    "error": "尚未检索到可用Schema；先补充检索或澄清，不能猜测表字段",
                }
            else:
                result = client.call_tool(name, arguments)
            trace.append(
                {
                    "call_index": len(trace) + 1,
                    "tool": name,
                    "arguments": arguments,
                    "sql": result.get("sql"),
                    "success": result.get("success", True),
                    "error": result.get("error"),
                    "row_count": result.get("row_count"),
                    "reason": str(decision.get("reason") or ""),
                    "result": result if name == "search_credit_schema" else None,
                }
            )
            if (
                name == "query_credit_ops"
                and result.get("success")
                and decision.get("purpose") != "inspect"
            ):
                return {
                    "action": "executed",
                    "execution": SqlExecution(
                        sql=result["sql"],
                        success=True,
                        columns=result["columns"],
                        rows=result["rows"],
                        returned_rows=result["row_count"],
                        total_rows=result.get("total_rows"),
                        truncated=result.get("truncated", False),
                        data_version=result["data_version"],
                        data_as_of=result["data_as_of"],
                        column_metadata=result.get("column_metadata", {}),
                    ),
                    "interpretation": decision.get("interpretation") or {},
                    "trace": trace,
                }
            observations.append(
                {"tool": name, "arguments": arguments, "result": result}
            )
        raise ValueError(
            f"Agent达到工具调用上限（{self.skill.max_tool_calls}），请缩小问题范围"
        )

    def execute(self, compiled, scope):
        client = self.client_factory(scope)
        names = {t["name"] for t in client.list_tools()}
        if "query_credit_ops" not in names:
            raise PermissionError("无权访问金融查询工具")
        result = client.call_tool("query_credit_ops", {"sql": compiled.sql})
        execution = SqlExecution(
            sql=result["sql"],
            success=result["success"],
            columns=result.get("columns", []),
            rows=result.get("rows", []),
            error=result.get("error"),
            truncated=result.get("truncated", False),
            returned_rows=result.get("row_count", 0),
            total_rows=result.get("total_rows"),
            data_version=result.get("data_version", ""),
            data_as_of=result.get("data_as_of", ""),
            column_metadata=compiled.column_metadata,
        )
        trace = {
            "call_index": 1,
            "tool": "query_credit_ops",
            "database": "credit_ops",
            "arguments": {
                "sql_source": "versioned_metric_compiler",
                "metric_id": compiled.metric["id"],
                "metric_version": compiled.metric["version"],
            },
            "sql": compiled.sql,
            "success": execution.success,
            "row_count": execution.returned_rows,
            "error": execution.error,
        }
        return execution, [trace]
