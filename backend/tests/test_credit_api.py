"""HTTP 边界：登录、行权限、任务归属和可信报告来源。全程离线。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.config import settings
from app.main import app
from app.model_client import ModelClient
from app.retrieval import SchemaIndex
from app.services.askdata_service import AskDataService


class CreditApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cfg = replace(
            settings,
            credit_demo_mode=True,
            credit_reference_date="2026-10-01",
            session_archive_path=str(root / "api.db"),
        )
        model = ModelClient(cfg)
        service = AskDataService(
            model, SchemaIndex(model, cfg, index_path=root / "index.json"), cfg
        )
        cls.override = patch("app.api.routes.service", service)
        cls.override.start()
        cls.client = TestClient(app)
        cls.headers = {}
        for name in ("analyst", "east"):
            reply = cls.client.post(
                "/api/auth/login", json={"username": name, "password": name + "123"}
            )
            cls.headers[name] = {
                "Authorization": "Bearer " + reply.json()["access_token"]
            }

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.override.stop()
        cls.temp.cleanup()

    def test_anonymous_cannot_query_and_health_is_minimal(self):
        self.assertEqual(self.client.get("/api/schema").status_code, 401)
        self.assertEqual(
            self.client.post("/api/query", json={"query": "查询贷款余额"}).status_code,
            401,
        )
        self.assertEqual(
            set(self.client.get("/api/health").json()), {"status", "scenario"}
        )

    def test_api_applies_region_and_field_permissions(self):
        h = self.headers["east"]
        schema = self.client.get("/api/schema", headers=h).json()
        self.assertFalse(any(f["name"] == "phone" for t in schema for f in t["fields"]))
        reply = self.client.post(
            "/api/query",
            headers=h,
            json={"query": "查询2026年9月各地区放款金额", "session_id": "api-region"},
        )
        self.assertEqual(reply.status_code, 200)
        body = reply.json()
        self.assertEqual(body["status"], "completed", body["analysis"])
        self.assertEqual([r["地区"] for r in body["rows"]], ["华东"])
        self.assertEqual(
            self.client.post("/api/schema/index/rebuild", headers=h).status_code, 403
        )

    def test_api_result_owner_save_and_report(self):
        h = self.headers["analyst"]
        source = self.client.post(
            "/api/query",
            headers=h,
            json={"query": "查询2026年9月各渠道放款金额", "session_id": "api-source"},
        ).json()
        task_id = source["task_id"]
        self.assertEqual(
            self.client.get(
                "/api/tasks/" + task_id, headers=self.headers["east"]
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/api/memories", headers=h, json={"task_id": task_id}
            ).status_code,
            200,
        )
        self.assertTrue(self.client.get("/api/memories", headers=h).json())
        report = self.client.post(
            "/api/query",
            headers=h,
            json={
                "query": "生成分析报告",
                "session_id": "api-report",
                "workspace": {"analysis_table_ids": [task_id]},
            },
        ).json()
        self.assertEqual(report["status"], "completed", report["analysis"])
        self.assertEqual(
            report["report"]["sources"][0]["result_fingerprint"],
            source["result_fingerprint"],
        )

    def test_api_clarification_owner_and_resume(self):
        h = self.headers["analyst"]
        source = self.client.post(
            "/api/query",
            headers=h,
            json={"query": "查询2026年9月末逾期率", "session_id": "api-clarify"},
        ).json()
        endpoint = "/api/tasks/" + source["task_id"] + "/clarify"
        choice = {"option_id": "overdue_balance_ratio"}
        self.assertEqual(source["status"], "waiting_clarification")
        self.assertEqual(
            self.client.post(
                endpoint, headers=self.headers["east"], json=choice
            ).status_code,
            404,
        )
        result = self.client.post(endpoint, headers=h, json=choice).json()
        self.assertEqual(result["status"], "completed", result["analysis"])
        self.assertEqual(result["task_id"], source["task_id"])


if __name__ == "__main__":
    unittest.main()
