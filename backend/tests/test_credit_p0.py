from __future__ import annotations
import tempfile, unittest
from dataclasses import replace
from pathlib import Path
from decimal import Decimal
from app.config import settings
from app.credit.metrics import METRIC_VERSION
from app.credit.plan import QueryPlan
from app.credit.compiler import compile_plan
from app.credit.planner import CreditPlanner
from app.model_client import ModelClient
from app.services.askdata_service import AskDataService
from app.retrieval import SchemaIndex
from app.security import AccessController
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.data_qa_agent import DataQaAgent


class CreditP0Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cfg = replace(
            settings,
            credit_demo_mode=True,
            credit_reference_date="2026-10-01",
            session_archive_path=str(cls.root / "tasks.db"),
        )
        cls.model = ModelClient(cls.cfg)
        cls.index = SchemaIndex(cls.model, cls.cfg, index_path=cls.root / "index.json")
        cls.service = AskDataService(cls.model, cls.index, cls.cfg)
        cls.engine = DuckDbEngine()
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def session(self):
        type(self).counter += 1
        return f"case-{self.counter}"

    def query(self, q, session=None, workspace=None, user="credit_analyst"):
        return self.service.submit(q, session or self.session(), workspace, user)

    def success(self, r):
        self.assertEqual(r.status, "completed", r.analysis)

    def test_single_financial_schema(self):
        tables = self.service.visible_schema("credit_analyst")
        self.assertEqual(len(tables), 12)
        self.assertTrue(all(t["database"] == "credit_ops" for t in tables))
        self.assertNotIn("phone", [f["name"] for t in tables for f in t["fields"]])

    def test_metric_query_money_and_provenance(self):
        r = self.query("查询2026年9月各渠道放款金额")
        self.success(r)
        self.assertEqual(len(r.rows), 3)
        self.assertIsInstance(r.rows[0]["放款金额"], str)
        self.assertTrue(r.query_plan and r.result_fingerprint and r.data_version)
        self.assertEqual(r.interpretation.metric_version, METRIC_VERSION)
        self.assertTrue(r.validation["metric_contract"] == "passed")

    def test_follow_up_changes_only_metric_then_dimension(self):
        sid = self.session()
        a = self.query("查询2026年9月华东各渠道放款金额", sid)
        self.success(a)
        b = self.query("首次放款客户数呢", sid)
        self.success(b)
        c = self.query("改按产品统计", sid)
        self.success(c)
        self.assertEqual(c.query_plan["start_date"], "2026-09-01")
        self.assertEqual(c.query_plan["metric_id"], "new_borrower_count")
        self.assertEqual(c.query_plan["dimensions"], ["product"])
        self.assertEqual(c.query_plan["filters"], {"region": ["华东"]})
        self.assertEqual(c.query_plan["parameter_sources"]["time"], "inherited")

    def test_ambiguity_resumes_same_task(self):
        sid = self.session()
        r = self.query("查询2026年9月末逾期率", sid)
        self.assertEqual(r.status, "waiting_clarification")
        self.assertEqual(len(r.clarification.options), 2)
        done = self.service.clarify(
            r.task_id, "overdue_principal_ratio", user_id="credit_analyst"
        )
        self.success(done)
        self.assertEqual(done.task_id, r.task_id)
        self.assertEqual(done.query_plan["as_of"], "2026-09-30")
        self.assertEqual(
            done.query_plan["parameter_sources"]["metric_id"], "user_confirmed"
        )

    def test_customer_ambiguity(self):
        r = self.query("查询2026年9月客户数")
        self.assertEqual(r.status, "waiting_clarification")
        self.assertEqual(len(r.clarification.options), 3)

    def test_multiple_metrics_not_silently_dropped(self):
        result = self.query("查询2026年9月放款金额和放款客户数")
        self.success(result)
        self.assertIsNone(result.query_plan)
        self.assertIn("每次查询一个指标", result.analysis)

    def test_restart_recovers_pending_clarification_and_owner(self):
        r = self.query("查询2026年9月末逾期率")
        restarted = AskDataService(self.model, self.index, self.cfg)
        done = restarted.clarify(
            r.task_id, "overdue_balance_ratio", user_id="credit_analyst"
        )
        self.success(done)
        self.assertEqual(
            restarted.get_task(r.task_id, "credit_analyst").task_id, r.task_id
        )
        with self.assertRaises(KeyError):
            restarted.get_task(r.task_id, "credit_east")

    def test_invalid_clarification_does_not_resume(self):
        r = self.query("查询2026年9月客户数")
        with self.assertRaises(ValueError):
            self.service.clarify(r.task_id, "unknown", user_id="credit_analyst")

    def test_natural_language_clarification(self):
        sid = self.session()
        r = self.query("查询2026年9月末逾期率", sid)
        done = self.query("逾期贷款余额占比", sid)
        self.success(done)
        self.assertEqual(done.task_id, r.task_id)

    def test_no_future_data_substitution(self):
        r = self.query("查询2026年10月放款金额")
        self.assertEqual(r.status, "failed")
        self.assertIn("超出范围", r.analysis)

    def test_relative_time_and_default_are_explicit(self):
        r = self.query("查询上月各渠道放款金额")
        self.success(r)
        self.assertEqual(r.query_plan["end_date"], "2026-09-30")
        r = self.query("查询贷款余额")
        self.success(r)
        self.assertEqual(
            r.query_plan["parameter_sources"]["time"], "default_data_cutoff"
        )

    def test_loan_balance_uses_one_date(self):
        r = self.query("查询2026年9月末贷款余额")
        self.success(r)
        self.assertIn("b.snapshot_on=DATE '2026-09-30'", r.sql)

    def test_dpd_threshold_boundaries(self):
        r = self.query("查询2026年9月末逾期超过30天的逾期贷款余额占比")
        self.success(r)
        self.assertEqual(r.query_plan["dpd_threshold"], 31)
        r2 = self.query("查询2026年9月末逾期30天及以上的逾期贷款余额占比")
        self.success(r2)
        self.assertEqual(r2.query_plan["dpd_threshold"], 30)

    def test_distinct_overdue_numerators(self):
        a = self.query("查询2026年9月末逾期贷款余额占比")
        b = self.query("查询2026年9月末逾期应还本金占比")
        self.success(a)
        self.success(b)
        self.assertGreaterEqual(
            float(a.rows[0]["逾期贷款余额占比"]), float(b.rows[0]["逾期应还本金占比"])
        )
        self.assertEqual(a.rows[0]["分母"], b.rows[0]["分母"])

    def test_zero_denominator_is_null(self):
        p = QueryPlan(
            metric_id="approval_rate",
            start_date="2026-09-01",
            end_date="2026-09-30",
            filters={"region": ["不存在地区"]},
        )
        r = self.engine.execute(
            "credit_ops", compile_plan(p, self.engine.manifest()).sql
        )
        self.assertTrue(r.success, r.error)
        self.assertIsNone(r.rows[0]["审批通过率"])

    def test_comparison_and_percentage_points(self):
        r = self.query("查询2026年9月审批通过率环比")
        self.success(r)
        self.assertEqual(len(r.comparison_stats), 1)
        self.assertIsNotNone(r.comparison_stats[0]["percentage_point_change"])

    def test_results_truncation_metadata(self):
        r = self.engine.execute(
            "credit_ops",
            "SELECT application_id FROM applications ORDER BY application_id",
        )
        self.assertTrue(r.success, r.error)
        self.assertTrue(r.truncated)
        self.assertEqual(len(r.rows), 200)
        self.assertIsNone(r.total_rows)

    def test_region_is_enforced_in_cte(self):
        scope = AccessController().resolve("credit_east")
        sql = "WITH a AS (SELECT org_id,principal FROM loans) SELECT org_id,SUM(principal) AS amount FROM a GROUP BY org_id"
        r = self.engine.execute("credit_ops", sql, scope)
        self.assertTrue(r.success, r.error)
        self.assertEqual({row["org_id"] for row in r.rows}, {"O1", "O2"})

    def test_region_cannot_be_overridden_by_prompt(self):
        r = self.query("查询2026年9月华南放款金额", user="credit_east")
        self.success(r)
        self.assertEqual(Decimal(r.rows[0]["放款金额"]), 0)

    def test_forbidden_field_is_absent_even_in_subquery(self):
        r = self.engine.execute(
            "credit_ops",
            "SELECT customer_name FROM customers",
            AccessController().resolve("credit_east"),
        )
        self.assertFalse(r.success)
        r = self.engine.execute(
            "credit_ops",
            "SELECT customer_name FROM customers",
            AccessController().resolve("credit_admin"),
        )
        self.assertTrue(r.success)

    def test_sql_safety_boundaries(self):
        for sql in [
            "DELETE FROM loans",
            "SELECT * FROM loans",
            "SELECT loan_id FROM loans; SELECT loan_id FROM loans",
            "SELECT loan_id FROM __raw_loans",
            "SELECT x FROM read_csv('/tmp/not-allowed.csv')",
            "SELECT table_name FROM information_schema.tables",
            "SELECT query('SELECT loan_id FROM __raw_loans')",
        ]:
            with self.subTest(sql=sql):
                self.assertFalse(self.engine.execute("credit_ops", sql).success)

    def test_user_cannot_read_or_save_other_result(self):
        r = self.query("查询2026年9月放款金额")
        self.success(r)
        with self.assertRaises(KeyError):
            self.service.get_task(r.task_id, "credit_east")
        with self.assertRaises(KeyError):
            self.service.save_memory(r.task_id, "credit_east")

    def test_permissions_changed_result_rejected(self):
        r = self.query("查询2026年9月放款金额")
        self.success(r)
        old = self.service.access_controller.resolve
        self.service.access_controller.resolve = lambda uid=None: replace(
            old(uid), allowed_orgs=frozenset({"O1"})
        )
        try:
            with self.assertRaises(PermissionError):
                self.service.get_task(r.task_id, "credit_analyst")
        finally:
            self.service.access_controller.resolve = old

    def test_report_ignores_frontend_row_forgery(self):
        r = self.query("查询2026年9月各渠道放款金额")
        self.success(r)
        qa = self.query(
            "生成分析报告",
            workspace={
                "analysis_table_ids": [r.task_id],
                "analysis_tables": [
                    {"task_id": r.task_id, "rows": [{"放款金额": 999999999}]}
                ],
            },
        )
        self.success(qa)
        self.assertTrue(qa.report)
        self.assertNotIn("999999999", qa.report.markdown)
        self.assertEqual(
            qa.report.sources[0]["result_fingerprint"], r.result_fingerprint
        )
        self.assertTrue(qa.report.facts)

    def test_report_cannot_import_other_user_task(self):
        r = self.query("查询2026年9月放款金额")
        qa = self.query(
            "生成分析报告",
            workspace={"analysis_table_ids": [r.task_id]},
            user="credit_east",
        )
        self.assertEqual(qa.status, "failed")

    def test_report_without_data_does_not_fabricate(self):
        r = self.query("生成分析报告")
        self.success(r)
        self.assertIsNone(r.report)
        self.assertIn("没有", r.analysis)

    def test_multi_source_report_and_chart_reference(self):
        a = self.query("查询2026年9月各渠道放款金额")
        b = self.query("查询2026年9月末各产品贷款余额")
        r = self.query(
            "生成分析报告", workspace={"analysis_table_ids": [a.task_id, b.task_id]}
        )
        self.success(r)
        self.assertEqual(len(r.report.sources), 2)
        self.assertTrue(
            all(
                v.source_task_id in {a.task_id, b.task_id}
                for v in r.report.visualizations
            )
        )

    def test_monthly_line_chart(self):
        a = self.query("查询2026年1月至2026年9月每月放款金额")
        self.success(a)
        self.assertEqual(len(a.rows), 9)
        r = self.query("生成分析报告", workspace={"analysis_table_ids": [a.task_id]})
        self.success(r)
        self.assertEqual(r.report.visualizations[0].type, "line")

    def test_different_data_versions_not_combined(self):
        r = self.query("查询2026年9月放款金额")
        self.success(r)
        other = r.model_copy(deep=True)
        other.data_version = "different"
        with self.assertRaises(ValueError):
            DataQaAgent().run_verified("生成报告", [r, other], True)

    def test_partial_report_has_no_overall_maximum(self):
        r = self.query("查询2026年9月各渠道放款金额")
        r.truncated = True
        report = DataQaAgent().run_verified("报告", [r], True).report
        self.assertEqual(report.completeness, "partial")
        self.assertIn("截断", report.markdown)
        self.assertNotIn("最高", report.markdown)

    def test_saved_result_survives_restart(self):
        r = self.query("查询2026年9月放款金额")
        self.service.save_memory(r.task_id, "credit_analyst")
        restarted = AskDataService(self.model, self.index, self.cfg)
        saved = restarted.list_memories("credit_analyst")
        self.assertIn(r.task_id, [m.get("task_id") for m in saved])
        report = restarted.submit(
            "生成分析报告",
            self.session(),
            {"analysis_table_ids": [r.task_id]},
            "credit_analyst",
        )
        self.success(report)
        self.assertTrue(report.report)

    def test_sensitive_data_not_in_retrieval_documents(self):
        self.index.ensure_built()
        # Shared store is metadata only; field-level permission must apply before recall/rerank.
        hits = self.index.retrieve(
            "客户姓名 联系电话",
            retrieval_terms=["客户姓名", "联系电话"],
            access_scope=AccessController().resolve("credit_analyst"),
        )["hits"]
        self.assertFalse(
            any(h["field_name"] in {"phone", "customer_name"} for h in hits)
        )
        self.assertTrue(all(not d.samples for d in self.index.documents))
        self.assertTrue(all("声明类型" in d.profile for d in self.index.documents))

    def test_cross_day_reversal_excludes_net_zero_borrower(self):
        import duckdb

        plan = QueryPlan(
            metric_id="borrower_count", start_date="2026-09-01", end_date="2026-09-30"
        )
        sql = compile_plan(plan, self.engine.manifest()).sql
        with duckdb.connect() as conn:
            conn.execute(
                "CREATE TABLE loans(loan_id VARCHAR, customer_id VARCHAR, product_id VARCHAR, channel_id VARCHAR, org_id VARCHAR)"
            )
            conn.execute(
                "INSERT INTO loans VALUES ('L1','C1','P1','CH1','O1'), ('L2','C2','P1','CH1','O1')"
            )
            conn.execute(
                "CREATE TABLE disbursements(loan_id VARCHAR, disbursed_on DATE, amount DECIMAL(18,2), status VARCHAR)"
            )
            conn.execute(
                "INSERT INTO disbursements VALUES ('L1','2026-09-01',100,'success'), ('L1','2026-09-02',-100,'reversal'), ('L2','2026-09-03',200,'success')"
            )
            self.assertEqual(conn.execute(sql).fetchone()[0], 1)

    def test_snapshot_trend_reversed_dates_rejected(self):
        with self.assertRaises(ValueError):
            QueryPlan(
                metric_id="loan_balance",
                dimensions=["month"],
                start_date="2026-09-01",
                end_date="2026-08-31",
                as_of="2026-09-30",
            )

    def test_two_dimension_report_does_not_collapse_chart_labels(self):
        result = self.query("查询2026年9月各地区各渠道放款金额")
        self.success(result)
        self.assertEqual(len(result.query_plan["dimensions"]), 2)
        report = DataQaAgent().run_verified("报告", [result], True).report
        self.assertEqual(report.visualizations, [])

    def test_report_fact_index_includes_rows_beyond_preview(self):
        result = self.query("查询2026年9月各渠道放款金额")
        result.rows = [{"渠道": str(i), "放款金额": str(i * 100)} for i in range(25)]
        report = DataQaAgent().run_verified("报告", [result], True).report
        self.assertTrue(any(f.get("row") == 24 for f in report.facts))

    def test_metric_dependencies_in_rag(self):
        r = self.query("查询2026年9月末各产品逾期贷款余额占比")
        self.success(r)
        docs = {h["doc_id"] for h in r.retrieval["hits"]}
        for name in ("snapshot_on", "outstanding_principal", "dpd"):
            self.assertIn("credit_ops.loan_daily_snapshots." + name, docs)

    def test_unknown_metric_and_bad_plan_rejected(self):
        for payload in [
            {"metric_id": "nonexistent"},
            {
                "metric_id": "loan_balance",
                "as_of": "2026-09-30",
                "metric_version": "outdated",
            },
            {
                "metric_id": "loan_balance",
                "as_of": "2026-09-30",
                "filters": {"sql": ["1=1"]},
            },
        ]:
            with self.assertRaises(ValueError):
                QueryPlan.model_validate(payload)

    def test_month_end_snapshot_trend_not_sum_of_daily_balances(self):
        r = self.query("查询2026年1月至2026年9月每月贷款余额")
        self.success(r)
        self.assertEqual(len(r.rows), 9)
        last = self.query("查询2026年9月末贷款余额")
        self.success(last)
        self.assertEqual(
            Decimal(r.rows[-1]["贷款本金余额"]), Decimal(last.rows[0]["贷款本金余额"])
        )
        self.assertIn("LAST_DAY", r.sql)

    def test_mixed_month_trend_and_comparison_rejected(self):
        r = self.query("查询2026年1月至2026年9月每月贷款余额环比")
        self.assertEqual(r.status, "failed")

    def test_legacy_planner_cannot_be_used_as_model_text2sql(self):
        planner = CreditPlanner(
            self.model,
            replace(self.cfg, credit_demo_mode=False),
            self.engine.manifest(),
        )
        with self.assertRaisesRegex(ValueError, "开放Agent"):
            planner.prepare("查询2026年9月各渠道放款金额")

    def test_model_unrecognized_injected_parameter_rejected(self):
        planner = CreditPlanner(self.model, self.cfg, self.engine.manifest())
        with self.assertRaises(ValueError):
            planner.resolve(
                "查询放款金额",
                {
                    "action": "database_query",
                    "patch": {
                        "metric_id": "disbursement_amount",
                        "sql": "SELECT 1",
                        "allowed_orgs": ["O3"],
                    },
                },
            )


if __name__ == "__main__":
    unittest.main()
