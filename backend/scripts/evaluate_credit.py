"""120条固定金融问数用例；独立Python聚合参考值对照SQL结果。
默认离线模式只验证已声明规则及指标链路，不代表大模型准确率。开放模型链路请使用 live_smoke.py，不能用本脚本评价。
"""

from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.credit.metrics import METRICS, DIMENSIONS
from app.credit.plan import QueryPlan
from app.credit.planner import CreditPlanner
from app.credit.compiler import compile_plan
from app.config import settings, BASE_DIR
from app.model_client import ModelClient
from app.querying.duckdb_engine import DuckDbEngine
from app.security import AccessController
from scripts.validate_credit_data import load


class Reference:
    def __init__(self, data):
        self.d = data
        self.loans = {r["loan_id"]: r for r in data["loans"]}
        self.apps = {r["application_id"]: r for r in data["applications"]}
        self.dim = {
            "region": {r["org_id"]: r["region"] for r in data["org_units"]},
            "org": {r["org_id"]: r["org_name"] for r in data["org_units"]},
            "channel": {r["channel_id"]: r["channel_name"] for r in data["channels"]},
            "product": {
                r["product_id"]: r["product_name"] for r in data["loan_products"]
            },
            "segment": {
                r["customer_id"]: r["customer_segment"] for r in data["customers"]
            },
        }

    def value(self, attrs, dimension):
        key = {
            "region": "org_id",
            "org": "org_id",
            "channel": "channel_id",
            "product": "product_id",
            "segment": "customer_id",
        }[dimension]
        return self.dim[dimension][attrs[key]]

    def calculate(self, plan):
        metric = METRICS[plan.metric_id]
        rows = []
        if metric.source == "applications":
            rows = [(r, r) for r in self.d["applications"]]
        elif metric.source == "approvals":
            rows = [
                (r, self.apps[r["application_id"]])
                for r in self.d["approval_events"]
                if r["is_final"] == "1" and r["decision"] in ("approved", "rejected")
            ]
        elif metric.source in ("disbursements", "borrowers", "first_borrowers"):
            rows = [
                (r, self.loans[r["loan_id"]])
                for r in self.d["disbursements"]
                if r["status"] in ("success", "reversal")
            ]
            if metric.source == "first_borrowers":
                net = defaultdict(Decimal)
                attrs = {}
                for r, a in rows:
                    k = (a["customer_id"], r["disbursed_on"], r["loan_id"])
                    net[k] += Decimal(r["amount"])
                    attrs[k] = a
                first = {}
                for k, v in sorted(net.items()):
                    if v > 0 and k[0] not in first:
                        first[k[0]] = (dict(disbursed_on=k[1]), attrs[k])
                rows = list(first.values())
        elif metric.source == "repayments":
            rows = [
                (r, self.loans[r["loan_id"]])
                for r in self.d["repayment_transactions"]
                if r["status"] in ("success", "reversal")
            ]
        else:
            rows = [
                (r, self.loans[r["loan_id"]])
                for r in self.d["loan_daily_snapshots"]
                if r["snapshot_on"] == str(plan.as_of)
            ]
        groups = {}
        if not plan.dimensions:
            groups[()] = []
        for r, a in rows:
            time_value = r[metric.time_field]
            if metric.time_kind == "period" and not (
                str(plan.start_date) <= time_value <= str(plan.end_date)
            ):
                continue
            if any(
                self.value(a, dim) not in vals for dim, vals in plan.filters.items()
            ):
                continue
            k = tuple(
                time_value[:7] if dim == "month" else self.value(a, dim)
                for dim in plan.dimensions
            )
            groups.setdefault(k, []).append((r, a))
        results = {}
        for k, items in groups.items():
            identity = metric.id
            if identity == "application_count":
                value = len({r["application_id"] for r, a in items})
            elif identity in ("applicant_count", "new_borrower_count"):
                value = len({a["customer_id"] for r, a in items})
            elif identity == "borrower_count":
                paid = defaultdict(Decimal)
                for r, a in items:
                    paid[a["customer_id"]] += Decimal(r["amount"])
                value = sum(v > 0 for v in paid.values())
            elif identity == "approval_rate":
                value = (
                    Decimal(sum(r["decision"] == "approved" for r, a in items))
                    / len(items)
                    if items
                    else None
                )
            elif identity == "overdue_loan_count":
                value = len(
                    {
                        r["loan_id"]
                        for r, a in items
                        if int(r["dpd"]) >= plan.dpd_threshold
                        and Decimal(r["outstanding_principal"]) > 0
                    }
                )
            elif identity in ("overdue_balance_ratio", "overdue_principal_ratio"):
                den = sum(
                    (Decimal(r["outstanding_principal"]) for r, a in items), Decimal(0)
                )
                col = (
                    "outstanding_principal"
                    if identity == "overdue_balance_ratio"
                    else "overdue_principal"
                )
                num = sum(
                    (
                        Decimal(r[col])
                        for r, a in items
                        if int(r["dpd"]) >= plan.dpd_threshold
                    ),
                    Decimal(0),
                )
                value = num / den if den else None
            else:
                col = {
                    "disbursement_amount": "amount",
                    "loan_balance": "outstanding_principal",
                    "repaid_principal": "principal_paid",
                    "repaid_interest": "interest_paid",
                    "overdue_principal": "overdue_principal",
                }[identity]
                value = sum(
                    (
                        Decimal(r[col])
                        for r, a in items
                        if identity != "overdue_principal"
                        or int(r["dpd"]) >= plan.dpd_threshold
                    ),
                    Decimal(0),
                )
            results[k] = (
                None
                if value is None
                else Decimal(value).quantize(
                    Decimal("0.000001" if metric.unit == "比例" else "0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )
        return results


def cases():
    out = []
    for month in (8, 9):
        end = 31 if month == 8 else 30
        for metric in METRICS.values():
            for dimension in (None, "region", "channel", "product"):
                dims = [] if dimension is None else [dimension]
                p = {
                    "metric_id": metric.id,
                    "dimensions": dims,
                    **(
                        {"as_of": f"2026-{month:02}-{end}"}
                        if metric.time_kind == "snapshot"
                        else {
                            "start_date": f"2026-{month:02}-01",
                            "end_date": f"2026-{month:02}-{end}",
                        }
                    ),
                }
                question = (
                    f"查询2026年{month}月"
                    + ("末" if metric.time_kind == "snapshot" else "")
                    + ("各" + DIMENSIONS[dimension] if dimension else "")
                    + metric.label
                )
                out.append({"question": question, "plan": p, "user": "credit_analyst"})
    # Distinct institution/filter and overdue thresholds, held separately from base questions.
    for metric_id in (
        "loan_balance",
        "disbursement_amount",
        "approval_rate",
        "overdue_balance_ratio",
    ):
        m = METRICS[metric_id]
        for region in ("华东", "华南", "华北", "不存在地区"):
            p = {
                "metric_id": metric_id,
                "filters": {"region": [region]},
                **(
                    {"as_of": "2026-09-30"}
                    if m.time_kind == "snapshot"
                    else {"start_date": "2026-09-01", "end_date": "2026-09-30"}
                ),
            }
            out.append(
                {
                    "question": "查询2026年9月"
                    + ("末" if m.time_kind == "snapshot" else "")
                    + region
                    + m.label,
                    "plan": p,
                    "user": "credit_analyst",
                    "explicit_plan": region == "不存在地区",
                }
            )
    return out


def evaluate(live=False, limit=None):
    if live:
        raise ValueError("本脚本仅评估离线固定指标。开放模型验收请使用 live_smoke.py。")
    engine = DuckDbEngine()
    manifest = engine.manifest()
    reference = Reference(load(BASE_DIR / "data/databases/credit_ops"))
    cfg = replace(
        settings, credit_demo_mode=not live, credit_reference_date="2026-10-01"
    )
    planner = CreditPlanner(ModelClient(cfg), cfg, manifest)
    selected = cases()[:limit]
    out = []
    for index, case in enumerate(selected, 1):
        start = time.monotonic()
        try:
            expected_plan = QueryPlan.model_validate(case["plan"])
            if case.get("explicit_plan"):
                plan = expected_plan
            else:
                plan = planner.resolve(
                    case["question"], planner.prepare(case["question"])
                )
            for key in (
                "metric_id",
                "dimensions",
                "filters",
                "start_date",
                "end_date",
                "as_of",
            ):
                if getattr(plan, key) != getattr(expected_plan, key):
                    raise AssertionError(
                        f"解析不一致 {key}: {getattr(plan, key)} != {getattr(expected_plan, key)}"
                    )
            sql = compile_plan(plan, manifest).sql
            result = engine.execute(
                "credit_ops", sql, AccessController().resolve(case["user"])
            )
            if not result.success:
                raise AssertionError(result.error)
            expected = reference.calculate(expected_plan)
            actual = {
                tuple(str(r[DIMENSIONS[d]]) for d in plan.dimensions): None
                if r[METRICS[plan.metric_id].label] is None
                else Decimal(str(r[METRICS[plan.metric_id].label]))
                for r in result.rows
            }
            if set(actual) != set(expected) or any(
                (actual[k] is None) != (expected[k] is None)
                or actual[k] is not None
                and abs(actual[k] - expected[k]) > Decimal("0.000001")
                for k in expected
            ):
                raise AssertionError(f"结果不同：{actual} / {expected}")
            out.append(
                {
                    "case": index,
                    "question": case["question"],
                    "passed": True,
                    "seconds": round(time.monotonic() - start, 3),
                }
            )
        except Exception as exc:
            out.append(
                {
                    "case": index,
                    "question": case["question"],
                    "passed": False,
                    "error": str(exc),
                }
            )
    return {
        "mode": "live_model" if live else "explicit_offline_rules_and_sql_reference",
        "data_version": manifest["data_version"],
        "cases": len(out),
        "passed": sum(x["passed"] for x in out),
        "note": "离线通过率不等于真实大模型准确率；参考值由独立Python聚合计算。",
        "results": out,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--output", default="data/credit_evaluation.json")
    args = p.parse_args()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = evaluate(args.live, args.limit)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (BASE_DIR / "data/credit_evaluation_cases.json").write_text(
        json.dumps(cases(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )
    for r in result["results"]:
        if not r["passed"]:
            print(r)
    sys.exit(0 if result["passed"] == result["cases"] else 1)
