from __future__ import annotations
from dataclasses import asdict
import json
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt
from ..config import settings
from ..models import QueryResult
from ..security import AccessScope
from ..credit.planner import CreditPlanner, NeedsClarification
from ..credit.plan import QueryPlan
from ..credit.compiler import compile_plan
from ..credit.metrics import METRICS
from ..credit.knowledge import retrieve_knowledge
from ..preprocessing import RequestPreprocessor
from ..skills import SkillRegistry
from ..querying.duckdb_engine import DuckDbEngine
from ..querying.single_database_agent import SingleDatabaseAgent
from ..querying.data_qa_agent import DataQaAgent
from ..mcp_runtime import LocalMcpClient, create_local_mcp_server
from ..retrieval import SchemaGraphBuilder
from .state import QueryState
from .result_builder import ResultBuilder


class QueryWorkflow:
    def __init__(self, model_client, schema_index, config=None):
        self.config = config or settings
        self.model_client = model_client
        self.schema_index = schema_index
        self.database_engine = DuckDbEngine()
        self.graph_builder = SchemaGraphBuilder()
        skills = SkillRegistry()
        self.single_database_agent = SingleDatabaseAgent(
            self.mcp_client, model_client, skills.get("database_query")
        )
        self.data_qa_agent = DataQaAgent(
            model_client, self.mcp_client, skills.get("data_qa")
        )
        self.graph = self._compile()

    def mcp_client(self, scope):
        return LocalMcpClient(
            create_local_mcp_server(
                self.database_engine, AccessScope.from_dict(scope), self.search_schema
            )
        )

    def search_schema(self, query, scope):
        retrieved = self.schema_index.retrieve(
            query, retrieval_terms=[query], access_scope=scope
        )
        return {
            "retrieval": retrieved,
            "schema_graph": self.graph_builder.build(retrieved["hits"], scope),
            "knowledge": retrieve_knowledge(
                query, self.database_engine.manifest()["data_as_of"]
            ),
        }

    def prepare_request(self, state):
        if self.config.credit_demo_mode:
            return self.planner().prepare(state["query"], state.get("previous_plan"))
        context = json.dumps(
            {
                "recent": state.get("recent_context", ""),
                "dataset": self.database_engine.manifest(),
                "reference_date": str(self.planner().reference_date()),
                "selected_source_ids": state.get("workspace", {}).get(
                    "analysis_table_ids", []
                ),
            },
            ensure_ascii=False,
        )
        return asdict(
            RequestPreprocessor(self.model_client).prepare(state["query"], context)
        )

    def confirmed_value(self, clarification, option_id):
        if self.config.credit_demo_mode:
            return option_id
        option = next(o for o in clarification["options"] if o["id"] == option_id)
        return f"{option['label']}：{option['description']}（{option_id}）"

    def planner(self):
        return CreditPlanner(
            self.model_client, self.config, self.database_engine.manifest()
        )

    @staticmethod
    def run_config(task_id):
        return {"configurable": {"thread_id": task_id}}

    def _compile(self):
        g = StateGraph(QueryState)
        for name, fn in [
            ("prepare", self._prepare),
            ("direct", self._direct),
            ("qa", self._qa),
            ("resolve", self._resolve),
            ("clarify", self._clarify),
            ("retrieve", self._retrieve),
            ("execute", self._execute),
            ("model_execute", self._model_execute),
        ]:
            g.add_node(name, fn)
        g.add_edge(START, "prepare")
        g.add_conditional_edges(
            "prepare",
            lambda s: {
                "database_query": "resolve"
                if self.config.credit_demo_mode
                else "retrieve",
                "data_qa": "qa",
                "direct_response": "direct",
            }[s["decision"]["action"]],
        )
        g.add_conditional_edges(
            "resolve", lambda s: "clarify" if s.get("clarification") else "retrieve"
        )
        g.add_edge("clarify", "resolve" if self.config.credit_demo_mode else "retrieve")
        g.add_edge(
            "retrieve", "execute" if self.config.credit_demo_mode else "model_execute"
        )
        g.add_conditional_edges(
            "model_execute", lambda s: "clarify" if s.get("clarification") else END
        )
        for node in ("qa", "direct", "execute"):
            g.add_edge(node, END)
        return g.compile(checkpointer=InMemorySaver())

    def invoke(self, payload, task_id):
        state = self.graph.invoke(payload, config=self.run_config(task_id))
        return (
            QueryResult.model_validate(state["result"])
            if state.get("result")
            else ResultBuilder.waiting(state)
        )

    def saved_state(self, task_id):
        return dict(self.graph.get_state(self.run_config(task_id)).values)

    def _prepare(self, state):
        return {
            "decision": state.get("decision") or self.prepare_request(state),
            "result": {},
        }

    def _direct(self, state):
        return {
            "result": QueryResult(
                task_id=state["task_id"],
                status="completed",
                route="direct_response",
                message="信贷助手",
                analysis=state["decision"].get(
                    "response", "请说明需要查询的信贷指标。"
                ),
            ).model_dump(mode="json")
        }

    def _qa(self, state):
        sources = [QueryResult.model_validate(r) for r in state.get("sources", [])]
        qa = (
            self.data_qa_agent.run_verified(
                state["query"], sources, bool(state["decision"].get("report"))
            )
            if self.config.credit_demo_mode
            else self.data_qa_agent.run_model(
                state["query"], sources, state["access_scope"]
            )
        )
        result = QueryResult(
            task_id=state["task_id"],
            status="completed",
            route="data_qa",
            message="已基于后端查询结果完成分析",
            analysis=qa.answer,
            report=qa.report,
            report_tool_calls=qa.tool_calls,
            validation={
                "source": "server_verified",
                "analysis_mode": "offline_template"
                if self.config.credit_demo_mode
                else "model_grounded",
            },
            analysis_sources=[
                {
                    "task_id": r.task_id,
                    "title": r.result_title,
                    "query": "",
                    "row_count": len(r.rows),
                    "columns": r.columns,
                }
                for r in sources
            ],
            workflow_mode="credit_report" if qa.report else "credit_qa",
        )
        return {"result": result.model_dump(mode="json")}

    def _resolve(self, state):
        try:
            plan = self.planner().resolve(
                state["query"],
                state["decision"],
                state.get("previous_plan"),
                state.get("confirmed"),
            )
            compile_plan(plan, self.database_engine.manifest())
            return {"plan": plan.model_dump(mode="json"), "clarification": None}
        except NeedsClarification as exc:
            return {"clarification": exc.payload}

    def _clarify(self, state):
        selected = interrupt(state["clarification"])
        return {
            "confirmed": {
                **state.get("confirmed", {}),
                state["clarification"]["parameter"]: self.confirmed_value(
                    state["clarification"], selected["option_id"]
                ),
            },
            "clarification": None,
        }

    def _retrieve(self, state):
        if not self.config.credit_demo_mode:
            scope = AccessScope.from_dict(state["access_scope"])
            query = state["decision"].get("standalone_query") or state["query"]
            query += " " + " ".join(state.get("confirmed", {}).values())
            terms = state["decision"].get("retrieval", {}).get("retrieval_terms") or [
                query
            ]
            # Confirmed dates belong in SQL context; appending date digits to every
            # field query dilutes lexical recall. Additional business fields can be
            # retrieved by the model through search_credit_schema after clarification.
            retrieval = self.schema_index.retrieve(
                query, retrieval_terms=terms, access_scope=scope
            )
            retrieval = self.schema_index.include_workspace(
                retrieval, state.get("workspace", {}), scope
            )
            return {
                "retrieval": retrieval,
                "schema_graph": self.graph_builder.build(retrieval["hits"], scope),
                "knowledge": retrieve_knowledge(
                    query, self.database_engine.manifest()["data_as_of"]
                ),
                "clarification": None,
            }
        compiled = compile_plan(
            QueryPlan.model_validate(state["plan"]), self.database_engine.manifest()
        )
        scope = AccessScope.from_dict(state["access_scope"])
        for doc in compiled.required_fields:
            table_id, field = doc.rsplit(".", 1)
            if not scope.allows_field(table_id, field):
                raise PermissionError("所选指标依赖无权访问的字段")
        metric = METRICS[state["plan"]["metric_id"]]
        terms = [metric.label, *state["plan"]["dimensions"], *metric.aliases]
        retrieval = self.schema_index.retrieve(
            state["query"], retrieval_terms=terms, access_scope=scope
        )
        required = [
            {"tableId": doc.rsplit(".", 1)[0], "name": doc.rsplit(".", 1)[1]}
            for doc in compiled.required_fields
        ]
        retrieval = self.schema_index.include_workspace(
            retrieval, {"schema_fields": required}, scope
        )
        for hit in retrieval["hits"]:
            if hit["doc_id"] in compiled.required_fields:
                hit.update(
                    source="metric_dependency",
                    selection_reason="versioned_metric_contract",
                )
        # Only validated dependencies are supplied to the final execution graph.
        retrieval["hits"] = [
            h for h in retrieval["hits"] if h["doc_id"] in compiled.required_fields
        ]
        if set(compiled.required_fields) - {h["doc_id"] for h in retrieval["hits"]}:
            raise ValueError("指标依赖字段缺失，请重建金融Schema索引")
        retrieval["selected_count"] = len(retrieval["hits"])
        retrieval["metric_knowledge"] = metric.public()
        graph = self.graph_builder.build(retrieval["hits"], scope)
        for f in state.get("workspace", {}).get("schema_fields", []):
            tid = f.get("tableId")
            name = f.get("name")
            if not tid or not scope.allows_field(tid, name):
                raise PermissionError("选中的字段无权访问")
            if f"{tid}.{name}" not in compiled.required_fields:
                raise ValueError("选中字段与当前指标口径无关，请移除字段或更换指标")
        return {"retrieval": retrieval, "schema_graph": graph}

    def _execute(self, state):
        compiled = compile_plan(
            QueryPlan.model_validate(state["plan"]), self.database_engine.manifest()
        )
        execution, trace = self.single_database_agent.execute(
            compiled, state["access_scope"]
        )
        if not execution.success:
            raise ValueError(execution.error)
        return {
            "result": ResultBuilder.completed(state, execution, trace).model_dump(
                mode="json"
            )
        }

    def _model_execute(self, state):
        outcome = self.single_database_agent.run(
            state, self.database_engine.manifest(), self.planner().reference_date()
        )
        if outcome["action"] == "clarify":
            return {
                "clarification": outcome["clarification"],
                "agent_trace": outcome["trace"],
            }
        if outcome["action"] == "answer":
            return {
                "result": QueryResult(
                    task_id=state["task_id"],
                    status="completed",
                    route="direct_response",
                    message="需要补充数据或调整问题",
                    analysis=outcome["answer"],
                    retrieval=state["retrieval"],
                    tool_calls=outcome["trace"],
                    workflow_mode="credit_open_query",
                ).model_dump(mode="json")
            }
        result = ResultBuilder.open_completed(
            state, outcome["execution"], outcome["trace"], outcome["interpretation"]
        )
        try:
            qa = self.data_qa_agent.run_model(
                state["query"], [result], state["access_scope"], report=False
            )
            result.analysis = qa.answer
            result.validation["model_analysis"] = "grounded_references_checked"
            result.report_tool_calls = qa.tool_calls
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            # SQL success is still a valid result even if subsequent model explanation fails.
            result.analysis = (
                "数据查询成功；模型分析未通过或暂不可用，请以表格与SQL为准。"
            )
            result.validation["model_analysis"] = "unavailable"
            result.execution_log.append(
                {"stage": "model_analysis", "success": False, "error": str(exc)}
            )
        return {
            "result": result.model_dump(mode="json"),
            "agent_trace": outcome["trace"],
        }
