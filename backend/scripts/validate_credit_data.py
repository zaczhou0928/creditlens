"""独立核对金融数据：主键、关系、余额守恒及按日还款重算。"""

from __future__ import annotations
import csv, json, sys, hashlib
from pathlib import Path
from collections import defaultdict
from datetime import date
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.credit.schema import SCHEMA, RELATIONS
from app.config import BASE_DIR


def load(folder):
    result = {}
    for t in SCHEMA:
        with (folder / f"{t['name']}.csv").open(encoding="utf-8") as f:
            result[t["name"]] = list(csv.DictReader(f))
    return result


def validate(folder=None):
    folder = folder or BASE_DIR / "data/databases/credit_ops"
    data = load(folder)
    checks = []

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    for table in SCHEMA:
        rows = data[table["name"]]
        pk = table["primary_key"]
        check(
            len({tuple(r[k] for k in pk) for r in rows}) == len(rows),
            table["name"] + "主键唯一",
        )
    for relation in RELATIONS:
        left, right = (
            relation["left_table"].split(".")[-1],
            relation["right_table"].split(".")[-1],
        )
        valid = {r[relation["right_field"]] for r in data[right]}
        check(
            all(r[relation["left_field"]] in valid for r in data[left]),
            left + "→" + right + "外键完整",
        )
    txs = {r["transaction_id"]: r for r in data["repayment_transactions"]}
    schedule = {r["schedule_id"]: r for r in data["repayment_schedules"]}
    allocation_totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
    allocated = defaultdict(list)
    for r in data["repayment_allocations"]:
        tx = txs[r["transaction_id"]]
        s = schedule[r["schedule_id"]]
        check(tx["loan_id"] == s["loan_id"], "还款与计划同借据")
        pp, ip = Decimal(r["principal_paid"]), Decimal(r["interest_paid"])
        allocation_totals[r["transaction_id"]][0] += pp
        allocation_totals[r["transaction_id"]][1] += ip
        allocated[r["schedule_id"]].append((tx["paid_on"], pp, ip))
    check(
        all(
            allocation_totals[key]
            == [Decimal(t["principal_paid"]), Decimal(t["interest_paid"])]
            for key, t in txs.items()
        ),
        "分配与实还本金利息守恒",
    )
    loans = {r["loan_id"]: r for r in data["loans"]}
    plans = defaultdict(list)
    for r in schedule.values():
        plans[r["loan_id"]].append(r)
    check(
        all(
            sum(Decimal(s["principal_due"]) for s in plans[lid])
            == Decimal(l["principal"])
            for lid, l in loans.items()
        ),
        "每笔应还本金合计等于放款本金",
    )
    applications = {r["application_id"]: r for r in data["applications"]}
    final_approvals = [r for r in data["approval_events"] if r["is_final"] == "1"]
    check(
        len({r["application_id"] for r in final_approvals}) == len(final_approvals),
        "最终审批事件唯一",
    )
    check(
        all(
            applications[l["application_id"]]["applied_on"] <= l["originated_on"]
            for l in loans.values()
        ),
        "申请先于放款",
    )
    check(
        all(t["paid_on"] >= loans[t["loan_id"]]["originated_on"] for t in txs.values()),
        "实还不早于放款",
    )
    manifest = json.loads((folder / "_database_manifest.json").read_text())
    check(
        all(t["paid_on"] <= manifest["data_as_of"] for t in txs.values()),
        "实际事件不晚于数据截止日",
    )
    check(
        all(
            hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
            for name, digest in manifest["file_hashes"].items()
        ),
        "所有CSV内容哈希一致",
    )
    # Across the full dataset, snapshots reconcile with event flows using independent Python arithmetic.
    by_loan = defaultdict(list)
    for row in data["loan_daily_snapshots"]:
        by_loan[row["loan_id"]].append(row)
    rebuilt = 0
    for lid, snapshots in by_loan.items():
        loan = loans[lid]
        check(
            len(snapshots)
            == (
                date.fromisoformat(manifest["data_as_of"])
                - date.fromisoformat(loan["originated_on"])
            ).days
            + 1,
            "快照覆盖每个自然日",
        )
        # First/last, monthly ends and repayment boundaries catch historical look-ahead errors.
        event_dates = {x[0] for s in plans[lid] for x in allocated[s["schedule_id"]]}
        selected = [
            r
            for j, r in enumerate(snapshots)
            if j == 0
            or j == len(snapshots) - 1
            or r["snapshot_on"] in event_dates
            or r["snapshot_on"].endswith(("-28", "-30", "-31"))
        ]
        for row in selected:
            at = row["snapshot_on"]
            outstanding = Decimal(loan["principal"])
            overdue = Decimal(0)
            dpd = 0
            for s in plans[lid]:
                paid_p = sum(
                    (p for d, p, i in allocated[s["schedule_id"]] if d <= at),
                    Decimal(0),
                )
                paid_i = sum(
                    (i for d, p, i in allocated[s["schedule_id"]] if d <= at),
                    Decimal(0),
                )
                outstanding -= paid_p
                if s["due_on"] < at and (
                    Decimal(s["principal_due"]) > paid_p
                    or Decimal(s["interest_due"]) > paid_i
                ):
                    overdue += max(Decimal(0), Decimal(s["principal_due"]) - paid_p)
                    dpd = max(
                        dpd,
                        (date.fromisoformat(at) - date.fromisoformat(s["due_on"])).days,
                    )
            if (outstanding, overdue, dpd) != (
                Decimal(row["outstanding_principal"]),
                Decimal(row["overdue_principal"]),
                int(row["dpd"]),
            ):
                raise AssertionError(f"历史快照不一致 {lid} {at}")
            rebuilt += 1
    check(
        all(
            Decimal(r["outstanding_principal"]) >= Decimal(r["overdue_principal"]) >= 0
            for r in data["loan_daily_snapshots"]
        ),
        "全部快照：逾期本金不大于未偿本金且非负",
    )
    return {
        "status": "passed",
        "tables": len(SCHEMA),
        "checks": len(checks),
        "independently_rebuilt_snapshots": rebuilt,
        "snapshot_count": len(data["loan_daily_snapshots"]),
        "data_version": manifest["data_version"],
    }


if __name__ == "__main__":
    print(json.dumps(validate(), ensure_ascii=False, indent=2))
