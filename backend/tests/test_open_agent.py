"""离线Agent契约测试：脚本化模型响应 + 真实检索/MCP/DuckDB/权限。不是模型准确率。"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.credit.knowledge import historical_state_sql
from app.credit.schema import SCHEMA
from app.model_client import ModelClient
from app.querying.duckdb_engine import DuckDbEngine
from app.retrieval import SchemaIndex
from app.security import AccessController
from app.services.askdata_service import AskDataService
from scripts.open_query_cases import cases, compare_rows, load_rows, reference


class ScriptedModel(ModelClient):
    """手写响应检验编排契约，词法/哈希检索完全本地且明确标注。"""

    def __init__(self, config, replies):
        super().__init__(config)
        self.replies = list(replies)
        self.calls = []
        self.local = ModelClient(replace(config, credit_demo_mode=True))

    @property
    def enabled(self):
        return True

    def embed(self, texts):
        return self.local.embed(texts)

    def rerank(self, query, documents, top_n):
        return self.local.rerank(query, documents, top_n)

    def chat_json(self, system, user):
        self.calls.append((system, user))
        if not self.replies:
            raise AssertionError("脚本化模型响应耗尽")
        reply = self.replies.pop(0)
        return reply(system, user) if callable(reply) else reply


def prepared(query, terms=None, action="database_query"):
    return {
        "action": action,
        "confidence": 1,
        "reason": "脚本化测试",
        "standalone_query": query,
        "retrieval": {"retrieval_terms": terms or [query]},
    }


def sql_call(sql, purpose="final"):
    return {
        "action": "call_tool",
        "tool_name": "query_credit_ops",
        "arguments": {"sql": sql},
        "purpose": purpose,
        "interpretation": {"title": "开放查询测试", "definition": "按验收题给定口径"},
        "reason": "本地契约用例",
    }


ANSWER = {"action": "answer", "answer": "返回值为 {{S1.r0.c0}}，范围以来源SQL为准。"}
CLARIFICATION = {
    "action": "clarify",
    "clarification": {
        "parameter": "observation_date",
        "question": "需要哪个观察日？",
        "reason": "日终状态随日期变化",
        "options": [
            {"id": "june", "label": "六月末", "description": "2026-06-30日终"},
            {"id": "september", "label": "九月末", "description": "2026-09-30日终"},
        ],
    },
}


class OpenAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cfg = replace(
            settings,
            credit_demo_mode=False,
            external_model_enabled=False,
            api_key="",
            embedding_model="local_test_hash_not_live",
            rerank_model="local_test_lexical_not_live",
            credit_reference_date="2026-10-01",
            session_archive_path=str(cls.root / "tasks.db"),
        )
        cls.data = load_rows()
        cls.counter = 0
        cls.network = patch(
            "urllib.request.urlopen", side_effect=AssertionError("离线测试禁止网络请求")
        )
        cls.network.start()

    @classmethod
    def tearDownClass(cls):
        cls.network.stop()
        cls.temp.cleanup()

    def make_service(self, replies):
        model = ScriptedModel(self.cfg, replies)
        index = SchemaIndex(model, self.cfg, index_path=self.root / "index.json")
        return AskDataService(model, index, self.cfg), model

    def submit(self, service, question, user="credit_analyst", workspace=None):
        type(self).counter += 1
        return service.submit(question, f"open-{self.counter}", workspace, user)

    def test_unlisted_queries_execute_against_independent_csv_reference(self):
        for user in ("credit_analyst", "credit_east"):
            for case in cases():
                with self.subTest(user=user, case=case["id"]):
                    service, model = self.make_service(
                        [
                            prepared(case["question"], case["terms"]),
                            sql_call(case["sql"]),
                            ANSWER,
                        ]
                    )
                    with patch(
                        "app.workflows.query_graph.compile_plan",
                        side_effect=AssertionError("模型路径不得编译指标"),
                    ):
                        result = self.submit(service, case["question"], user)
                    self.assertEqual(result.status, "completed", result.analysis)
                    self.assertTrue(
                        compare_rows(
                            result.rows,
                            reference(
                                case["id"],
                                self.data,
                                AccessController().resolve(user).allowed_orgs,
                            ),
                        ),
                        result.rows,
                    )
                    self.assertEqual(result.query_plan["mode"], "model_sql")
                    self.assertNotIn("metric_id", result.query_plan)
                    self.assertEqual(
                        result.validation["model_analysis"],
                        "grounded_references_checked",
                    )
                    self.assertTrue(result.result_fingerprint)
                    hits = {h["doc_id"] for h in result.retrieval["hits"]}
                    self.assertTrue(
                        hits.intersection(case["relevant"]), (case["id"], hits)
                    )
                    payload = json.loads(model.calls[1][1])
                    self.assertEqual(
                        payload["retrieval"]["hits"], result.retrieval["hits"]
                    )
                    self.assertTrue(payload["schema_graph"]["fields"])
                    self.assertFalse(
                        any(
                            h.get("source") == "metric_dependency"
                            for h in result.retrieval["hits"]
                        )
                    )
                    self.assertTrue(
                        all(
                            "phone" not in h["doc_id"] for h in result.retrieval["hits"]
                        )
                    )

    def test_schema_mcp_inspection_and_sql_error_repair(self):
        question = "按贷款产品统计平均本金"

        def repair(system, user):
            p = json.loads(user)
            self.assertIn("schema_graph", p["tool_results"][0]["result"])
            self.assertTrue(p["tool_results"][1]["result"]["rows"])
            self.assertFalse(p["tool_results"][2]["result"]["success"])
            return sql_call("SELECT COUNT(*) AS n FROM loans")

        service, model = self.make_service(
            [
                prepared(question),
                {
                    "action": "call_tool",
                    "tool_name": "search_credit_schema",
                    "arguments": {"query": "贷款产品名称 初始贷款本金"},
                },
                sql_call("SELECT DISTINCT product_name FROM loan_products", "inspect"),
                sql_call("SELECT missing_column FROM loans"),
                repair,
                ANSWER,
            ]
        )
        result = self.submit(service, question)
        self.assertEqual(result.status, "completed", result.analysis)
        self.assertEqual(
            [t["tool"] for t in result.tool_calls],
            [
                "search_credit_schema",
                "query_credit_ops",
                "query_credit_ops",
                "query_credit_ops",
            ],
        )
        self.assertFalse(result.tool_calls[2]["success"])

    def test_model_clarification_restart_and_owner(self):
        service, _ = self.make_service([prepared("历史余额"), CLARIFICATION])
        waiting = self.submit(service, "历史余额")
        self.assertEqual(waiting.status, "waiting_clarification")

        def resumed(system, user):
            self.assertIn(
                "2026-06-30",
                json.loads(user)["confirmed_parameters"]["observation_date"],
            )
            return sql_call("SELECT COUNT(*) AS n FROM loans")

        restarted, _ = self.make_service([resumed, ANSWER])
        with self.assertRaises(KeyError):
            restarted.clarify(waiting.task_id, "june", user_id="credit_east")
        result = restarted.clarify(waiting.task_id, "june", user_id="credit_analyst")
        self.assertEqual(result.status, "completed", result.analysis)
        self.assertEqual(result.task_id, waiting.task_id)
        self.assertIn(
            "2026-06-30", result.query_plan["confirmed_parameters"]["observation_date"]
        )

    def test_model_clarification_same_process(self):
        service, _ = self.make_service(
            [
                prepared("观察日"),
                CLARIFICATION,
                sql_call("SELECT COUNT(*) AS n FROM loans"),
                ANSWER,
            ]
        )
        waiting = self.submit(service, "观察日")
        done = service.clarify(waiting.task_id, "september", user_id="credit_analyst")
        self.assertEqual(done.status, "completed", done.analysis)
        self.assertIn(
            "2026-09-30", done.query_plan["confirmed_parameters"]["observation_date"]
        )

    def test_followup_context_and_arbitrary_selected_field(self):
        case = cases()[0]
        service, model = self.make_service(
            [
                prepared(case["question"], case["terms"]),
                sql_call(case["sql"]),
                ANSWER,
                prepared("同样期间改按机构统计"),
                sql_call("SELECT COUNT(*) AS n FROM applications"),
                ANSWER,
            ]
        )
        a = service.submit(case["question"], "follow-up-open", user_id="credit_analyst")
        b = service.submit(
            "改按机构统计",
            "follow-up-open",
            {
                "schema_fields": [
                    {"tableId": "credit_ops.applications", "name": "requested_amount"}
                ]
            },
            "credit_analyst",
        )
        self.assertEqual(b.status, "completed", b.analysis)
        self.assertIn(a.task_id, model.calls[3][1])
        self.assertTrue(
            any(
                h["source"] == "user_confirmed"
                and h["field_name"] == "requested_amount"
                for h in b.retrieval["hits"]
            )
        )

    def test_permission_changed_history_not_sent_to_model(self):
        service, model = self.make_service(
            [prepared("统计贷款"), sql_call("SELECT COUNT(*) AS n FROM loans"), ANSWER]
        )
        service.submit("统计贷款", "changed-scope", user_id="credit_analyst")
        altered = replace(
            AccessController().resolve("credit_analyst"), allowed_orgs=frozenset({"O1"})
        )
        self.assertEqual(
            service.context.recent_context("credit_analyst", "changed-scope", altered),
            "[]",
        )

    def test_free_report_calculation_and_backend_sources(self):
        service, model = self.make_service(
            [prepared("贷款笔数"), sql_call("SELECT COUNT(*) AS n FROM loans"), ANSWER]
        )
        result = self.submit(service, "贷款笔数")
        service.save_memory(result.task_id, "credit_analyst")
        model.replies.extend(
            [
                prepared("生成报告", action="data_qa"),
                {
                    "action": "calculate",
                    "operation": "divide",
                    "operands": ["S1.r0.c0", "S1.r0.c0"],
                },
                {
                    "action": "report",
                    "answer": "样本借据数 {{S1.r0.c0}}。",
                    "title": "资产观察札记",
                    "markdown": "## 样本与证据\n\n借据数 {{S1.r0.c0}}，自身比值 {{C1}}。\n\n## 可继续核对的问题\n\n尚需审批策略证据才能解释原因。",
                    "tool_calls": [],
                },
            ]
        )
        report = self.submit(
            service,
            "生成报告",
            workspace={
                "analysis_table_ids": [result.task_id],
                "analysis_tables": [
                    {"task_id": result.task_id, "rows": [{"n": 999999}]}
                ],
            },
        )
        self.assertEqual(report.status, "completed", report.analysis)
        self.assertEqual(report.report.title, "资产观察札记")
        self.assertNotIn("999999", report.report.markdown)
        self.assertIn(result.result_fingerprint, report.report.markdown)
        self.assertEqual(report.report.facts[-1]["value"], "1")
        self.assertEqual(
            report.report_tool_calls[0]["tool"], "calculate_verified_facts"
        )
        self.assertIn(
            result.task_id,
            [m["task_id"] for m in service.list_memories("credit_analyst")],
        )

    def test_invented_number_rejected_and_repaired(self):
        service, _ = self.make_service(
            [
                prepared("贷款笔数"),
                sql_call("SELECT COUNT(*) AS n FROM loans"),
                {"action": "answer", "answer": "贷款为999999笔"},
                ANSWER,
            ]
        )
        result = self.submit(service, "贷款笔数")
        self.assertEqual(
            result.validation["model_analysis"], "grounded_references_checked"
        )
        self.assertNotIn("999999", result.analysis)

    def test_invented_fact_and_chart_source_rejected(self):
        from app.querying.data_qa_agent import DataQaAgent

        for invalid in ("{{S9.r0.c0}}", "不存在来源 [S9]", "增加了30%"):
            with self.assertRaises(ValueError):
                DataQaAgent._render_grounded(invalid, {}, [{"id": "S1"}])
        service, model = self.make_service(
            [prepared("查询借据"), sql_call("SELECT COUNT(*) AS n FROM loans"), ANSWER]
        )
        result = self.submit(service, "查询借据")
        invalid = {
            "action": "report",
            "answer": "核对结果 [S1]",
            "title": "报告",
            "markdown": "范围 [S1]",
            "tool_calls": [
                {
                    "name": "build_bar_chart",
                    "arguments": {
                        "title": "图",
                        "source_task_id": "fake",
                        "category_field": "n",
                        "value_field": "n",
                    },
                }
            ],
        }
        model.replies.extend([prepared("报告", action="data_qa"), *[invalid] * 6])
        report = self.submit(
            service, "报告", workspace={"analysis_table_ids": [result.task_id]}
        )
        self.assertEqual(report.status, "failed")

    def test_model_failure_keeps_verified_query_result(self):
        service, _ = self.make_service(
            [
                prepared("查询借据"),
                sql_call("SELECT COUNT(*) AS n FROM loans"),
                *[{"action": "answer", "answer": "伪造99999"}] * 6,
            ]
        )
        result = self.submit(service, "查询借据")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.validation["model_analysis"], "unavailable")
        self.assertTrue(result.rows)

    def test_truncated_result_no_calculation(self):
        service, model = self.make_service(
            [
                prepared("所有借据"),
                sql_call("SELECT loan_id,principal FROM loans ORDER BY loan_id"),
                {
                    "action": "calculate",
                    "operation": "sum",
                    "operands": ["S1.r0.c1", "S1.r1.c1"],
                },
                {"action": "answer", "answer": "结果已截断，仅能描述预览 [S1]。"},
            ]
        )
        r = self.submit(service, "所有借据")
        self.assertTrue(r.truncated)
        self.assertEqual(r.returned_rows, 200)
        self.assertIn("截断", r.analysis)
        self.assertEqual(r.report_tool_calls, [])
        self.assertIn("拒绝", json.loads(model.calls[-1][1])["feedback"][0])

    def test_unknown_domain_data_can_be_explained_without_metric_id(self):
        service, _ = self.make_service(
            [
                prepared("违约预测及征信评分"),
                {
                    "action": "answer",
                    "answer": "当前没有征信特征或已训练预测模型，无法给出评分。",
                },
            ]
        )
        result = self.submit(service, "违约预测及征信评分")
        self.assertEqual(result.route, "direct_response")
        self.assertIsNone(result.sql)

    def test_sensitive_metadata_only_retrievable_by_admin(self):
        service, _ = self.make_service([])
        for user, allowed in [("credit_admin", True), ("credit_analyst", False)]:
            result = service.search_schema("客户姓名 联系电话", 0.4, user)
            fields = {h["field_name"] for h in result["hits"]}
            self.assertEqual("customer_name" in fields, allowed)
            self.assertTrue(all(not h["samples"] for h in result["hits"]))

    def test_no_retrieved_schema_blocks_blind_sql(self):
        service, _ = self.make_service(
            [
                prepared("贷款统计"),
                sql_call("SELECT COUNT(*) AS n FROM loans"),
                CLARIFICATION,
            ]
        )
        with (
            patch.object(
                service.schema_index,
                "retrieve",
                return_value={"hits": [], "selected_count": 0},
            ),
            patch.object(
                service.workflow.database_engine,
                "execute",
                side_effect=AssertionError("无Schema时不能执行SQL"),
            ),
        ):
            result = self.submit(service, "贷款统计")
        self.assertEqual(result.status, "waiting_clarification")

    def test_open_query_and_model_chart_report_over_http(self):
        from fastapi.testclient import TestClient
        from app.main import app

        service, model = self.make_service(
            [
                prepared("按机构统计借据"),
                sql_call(
                    "SELECT org_id,COUNT(*) AS n FROM loans GROUP BY org_id ORDER BY org_id"
                ),
                ANSWER,
            ]
        )
        with patch("app.api.routes.service", service), TestClient(app) as client:
            token = client.post(
                "/api/auth/login", json={"username": "east", "password": "east123"}
            ).json()["access_token"]
            headers = {"Authorization": "Bearer " + token}
            result = client.post(
                "/api/query", headers=headers, json={"query": "按机构统计借据"}
            ).json()
            self.assertEqual(result["status"], "completed", result["analysis"])
            self.assertEqual([r["org_id"] for r in result["rows"]], ["O1", "O2"])
            task = result["task_id"]
            model.replies.extend(
                [
                    prepared("生成报告", action="data_qa"),
                    {
                        "action": "report",
                        "answer": "机构分布见图 [S1]。",
                        "title": "机构借据分布",
                        "markdown": "## 观察\n\n第一组机构 {{S1.r0.c0}}，借据数 {{S1.r0.c1}}。",
                        "tool_calls": [
                            {
                                "name": "build_bar_chart",
                                "arguments": {
                                    "title": "机构借据",
                                    "source_task_id": task,
                                    "category_field": "org_id",
                                    "value_field": "n",
                                    "max_items": 3,
                                },
                            }
                        ],
                    },
                ]
            )
            report = client.post(
                "/api/query",
                headers=headers,
                json={"query": "生成报告", "workspace": {"analysis_table_ids": [task]}},
            ).json()
            self.assertEqual(report["status"], "completed", report["analysis"])
            self.assertEqual(
                report["report"]["visualizations"][0]["source_task_id"], task
            )
            self.assertEqual(report["report_tool_calls"][0]["tool"], "build_bar_chart")

    def test_external_request_requires_explicit_enable(self):
        with self.assertRaisesRegex(RuntimeError, "未启用"):
            ModelClient(self.cfg).chat("test", "test")


class OpenSqlBoundaryTest(unittest.TestCase):
    def test_permission_and_readonly_including_cte_shadowing(self):
        engine = DuckDbEngine()
        scope = AccessController().resolve("credit_east")
        attacks = [
            "WITH __raw_loans AS (SELECT loan_id FROM loans) SELECT loan_id FROM __raw_loans",
            "SELECT customer_name FROM customers",
            "SELECT phone FROM customers",
            "SELECT a.phone FROM (SELECT phone FROM customers) a",
            "SELECT COUNT(*) FROM __raw_loans",
            "SELECT COUNT(*) FROM read_csv('/tmp/secret.csv')",
            "COPY loans TO '/tmp/test.csv'",
            "SELECT COUNT(*) FROM loans; DROP TABLE loans",
            "SELECT * FROM loans",
            "SELECT 1 AS same,2 AS same",
        ]
        for sql in attacks:
            with self.subTest(sql=sql):
                self.assertFalse(engine.execute("credit_ops", sql, scope).success)
        r = engine.execute(
            "credit_ops", "SELECT DISTINCT org_id FROM loans ORDER BY org_id", scope
        )
        self.assertEqual(r.rows, [{"org_id": "O1"}, {"org_id": "O2"}])
        # Even direct trusted connection has no full-data backing tables.
        with engine.connect(access_scope=scope) as con:
            self.assertFalse(
                any(
                    r[0].startswith("__raw")
                    for r in con.execute("SHOW TABLES").fetchall()
                )
            )

    def test_event_state_without_snapshot_interest_reversal_and_future_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "credit_ops"
            folder.mkdir()
            data = {
                "org_units": [{"org_id": "O1", "org_name": "上海", "region": "华东"}],
                "loans": [
                    {
                        "loan_id": "L1",
                        "org_id": "O1",
                        "originated_on": "2026-01-01",
                        "principal": "100.00",
                    }
                ],
                "disbursements": [
                    {
                        "disbursement_id": "D1",
                        "loan_id": "L1",
                        "disbursed_on": "2026-01-01",
                        "amount": "100.00",
                        "status": "success",
                    }
                ],
                "repayment_schedules": [
                    {
                        "schedule_id": "S1",
                        "loan_id": "L1",
                        "installment_no": 1,
                        "due_on": "2026-02-01",
                        "principal_due": "100.00",
                        "interest_due": "10.00",
                    }
                ],
                "repayment_transactions": [],
                "repayment_allocations": [],
            }
            for tid, day, principal, interest, status in [
                ("T1", "2026-02-01", "30", "5", "success"),
                ("T2", "2026-02-05", "70", "0", "success"),
                ("T3", "2026-03-01", "0", "5", "success"),
                ("T4", "2026-03-02", "-70", "0", "reversal"),
            ]:
                data["repayment_transactions"].append(
                    {
                        "transaction_id": tid,
                        "loan_id": "L1",
                        "paid_on": day,
                        "principal_paid": principal,
                        "interest_paid": interest,
                        "status": status,
                    }
                )
                data["repayment_allocations"].append(
                    {
                        "allocation_id": tid + "A",
                        "transaction_id": tid,
                        "schedule_id": "S1",
                        "principal_paid": principal,
                        "interest_paid": interest,
                    }
                )
            for table in SCHEMA:
                if table["name"] == "loan_daily_snapshots":
                    continue
                with (folder / (table["name"] + ".csv")).open("w") as file:
                    writer = csv.DictWriter(
                        file, fieldnames=[f["name"] for f in table["fields"]]
                    )
                    writer.writeheader()
                    writer.writerows(data.get(table["name"], []))
            (folder / "_database_manifest.json").write_text(
                json.dumps({"data_version": "test-events", "data_as_of": "2026-03-02"})
            )
            engine = DuckDbEngine(root)
            for at, balance, overdue, dpd in [
                ("2026-02-01", "70.00", "0.00", 0),
                ("2026-02-02", "70.00", "70.00", 1),
                ("2026-02-05", "0.00", "0.00", 4),
                ("2026-03-01", "0.00", "0.00", 0),
                ("2026-03-02", "70.00", "70.00", 29),
            ]:
                with self.subTest(date=at):
                    r = engine.execute("credit_ops", historical_state_sql(at))
                    self.assertTrue(r.success, r.error)
                    self.assertEqual(
                        r.rows,
                        [
                            {
                                "loan_id": "L1",
                                "outstanding_principal": balance,
                                "overdue_principal": overdue,
                                "dpd": dpd,
                            }
                        ],
                    )


if __name__ == "__main__":
    unittest.main()
