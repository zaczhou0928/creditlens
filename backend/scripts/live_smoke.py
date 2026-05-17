"""开放查询结果验收。默认拒绝外部发送；--local-contract 用脚本化模型验证链路。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import BASE_DIR, settings
from app.model_client import ModelClient
from app.retrieval import SchemaIndex
from app.security import AccessController
from app.services.askdata_service import AskDataService
from scripts.open_query_cases import cases, compare_rows, load_rows, reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--local-contract",
        action="store_true",
        help="完全本地：手写模型响应、真实MCP/SQL与独立参考值；不代表模型效果",
    )
    group.add_argument(
        "--allow-external-model",
        action="store_true",
        help="仅在明确获准发送问题、Schema、上下文及授权查询结果到配置服务后使用",
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    mode = (
        "scripted_model_local_contract"
        if args.local_contract
        else "live_external_model"
    )
    destination = (
        Path(args.output)
        if args.output
        else BASE_DIR
        / "data"
        / (
            "credit_open_contract.json"
            if args.local_contract
            else "credit_live_smoke.json"
        )
    )
    records = []
    data = load_rows()
    network_guard = (
        patch(
            "urllib.request.urlopen", side_effect=AssertionError("本地契约验收禁止网络")
        )
        if args.local_contract
        else nullcontext()
    )
    with tempfile.TemporaryDirectory(prefix="credit-open-") as temporary, network_guard:
        root = Path(temporary)
        cfg = replace(
            settings,
            credit_demo_mode=False,
            external_model_enabled=args.allow_external_model,
            credit_reference_date="2026-10-01",
            session_archive_path=str(root / "tasks.db"),
        )
        if args.local_contract:
            from tests.test_open_agent import ScriptedModel, prepared, sql_call, ANSWER

            cfg = replace(
                cfg,
                api_key="",
                embedding_model="local_test_hash_not_live",
                rerank_model="local_test_lexical_not_live",
            )
            model = ScriptedModel(cfg, [])
        else:
            model = ModelClient(cfg)
        service = AskDataService(
            model, SchemaIndex(model, cfg, index_path=root / "schema.json"), cfg
        )
        for user in ("credit_analyst", "credit_east"):
            for case in cases():
                if args.local_contract:
                    model.replies.extend(
                        [
                            prepared(case["question"], case["terms"]),
                            sql_call(case["sql"]),
                            ANSWER,
                        ]
                    )
                result = service.submit(
                    case["question"], f"{user}-{case['id']}", user_id=user
                )
                expected = reference(
                    case["id"], data, AccessController().resolve(user).allowed_orgs
                )
                retrieved = set(
                    h["doc_id"] for h in (result.retrieval or {}).get("hits", [])
                )
                for call in result.tool_calls:
                    if call.get("tool") == "search_credit_schema":
                        retrieved.update(
                            h["doc_id"]
                            for h in call.get("result", {})
                            .get("retrieval", {})
                            .get("hits", [])
                        )
                relevant = set(case["relevant"])
                values_match = result.status == "completed" and compare_rows(
                    result.rows, expected
                )
                records.append(
                    {
                        "id": case["id"],
                        "user": user,
                        "question": case["question"],
                        "result_matches_independent_reference": values_match,
                        "relevant_field_recall": len(relevant & retrieved)
                        / len(relevant),
                        "relevant_fields": sorted(relevant),
                        "retrieved_relevant_fields": sorted(relevant & retrieved),
                        "model_analysis_status": result.validation.get(
                            "model_analysis"
                        ),
                        "result": result.model_dump(mode="json"),
                    }
                )
        output = {
            "mode": mode,
            "cases": len(records),
            "reference_matches": sum(
                r["result_matches_independent_reference"] for r in records
            ),
            "cases_with_relevant_retrieval": sum(
                r["relevant_field_recall"] > 0 for r in records
            ),
            "note": "本地模式是脚本化响应与确定性检索，不验证真实LLM/Embedding/Rerank质量。字段召回只检查标注的部分关键字段，不代表全部SQL依赖。",
            "results": records,
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {k: v for k, v in output.items() if k != "results"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if output["reference_matches"] == output["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
