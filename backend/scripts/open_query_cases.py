"""开放查询验收题及独立CSV参考值。SQL仅供本地契约测试，不被运行期Agent导入。"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from app.config import BASE_DIR
from app.credit.knowledge import historical_state_sql

ROOT = BASE_DIR / "data/databases/credit_ops"


def load_rows(root=ROOT):
    return {
        p.stem: list(csv.DictReader(p.open(encoding="utf-8", newline="")))
        for p in Path(root).glob("*.csv")
        if p.stem != "loan_daily_snapshots"
    }


def historical_reference(data, at, orgs):
    """逐借据、逐计划用Decimal重算；不读快照，不执行参考SQL。"""
    loans = {
        r["loan_id"]: r
        for r in data["loans"]
        if r["org_id"] in orgs and r["originated_on"] <= at
    }
    funded, paid = defaultdict(Decimal), defaultdict(Decimal)
    for r in data["disbursements"]:
        if r["disbursed_on"] <= at and r["status"] in ("success", "reversal"):
            funded[r["loan_id"]] += Decimal(r["amount"])
    transactions = {
        r["transaction_id"]: r
        for r in data["repayment_transactions"]
        if r["paid_on"] <= at and r["status"] in ("success", "reversal")
    }
    for r in transactions.values():
        paid[r["loan_id"]] += Decimal(r["principal_paid"])
    allocation = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for r in data["repayment_allocations"]:
        if r["transaction_id"] in transactions:
            allocation[r["schedule_id"]][0] += Decimal(r["principal_paid"])
            allocation[r["schedule_id"]][1] += Decimal(r["interest_paid"])
    schedules = defaultdict(list)
    for r in data["repayment_schedules"]:
        schedules[r["loan_id"]].append(r)
    result = []
    for loan_id in loans:
        overdue, dpd = Decimal(0), 0
        for s in schedules[loan_id]:
            if s["due_on"] >= at:
                continue
            principal = Decimal(s["principal_due"]) - allocation[s["schedule_id"]][0]
            interest = Decimal(s["interest_due"]) - allocation[s["schedule_id"]][1]
            overdue += max(Decimal(0), principal)
            if principal > 0 or interest > 0:
                dpd = max(
                    dpd, (date.fromisoformat(at) - date.fromisoformat(s["due_on"])).days
                )
        result.append(
            {
                "loan_id": loan_id,
                "outstanding_principal": max(
                    Decimal(0), funded[loan_id] - paid[loan_id]
                ),
                "overdue_principal": overdue,
                "dpd": dpd,
            }
        )
    return result


def cases():
    return [
        {
            "id": "application_detail",
            "question": "列出2026年9月申请金额最高的五笔申请及客户群体；同额按申请编号升序，列为申请编号、客户群体、申请金额。",
            "terms": ["申请金额", "申请日期", "客户群体", "申请编号"],
            "relevant": [
                "credit_ops.applications.requested_amount",
                "credit_ops.customers.customer_segment",
            ],
            "sql": """SELECT a.application_id AS 申请编号,c.customer_segment AS 客户群体,a.requested_amount AS 申请金额
          FROM applications a JOIN customers c ON a.customer_id=c.customer_id
          WHERE a.applied_on BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'
          ORDER BY a.requested_amount DESC,a.application_id LIMIT 5""",
        },
        {
            "id": "loan_statistics",
            "question": "统计2026年9月放款借据，按产品显示贷款产品、借据笔数、平均初始本金（两位小数）、最长期限月数。",
            "terms": ["贷款产品名称", "初始贷款本金", "放款日期", "期限月数"],
            "relevant": ["credit_ops.loans.principal", "credit_ops.loans.term_months"],
            "sql": """SELECT p.product_name AS 贷款产品,COUNT(*) AS 借据笔数,ROUND(AVG(l.principal),2) AS 平均初始本金,MAX(l.term_months) AS 最长期限月数
          FROM loans l JOIN loan_products p ON l.product_id=p.product_id WHERE l.originated_on BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'
          GROUP BY p.product_name ORDER BY p.product_name""",
        },
        {
            "id": "separate_fact_join",
            "question": "对比2026年9月各产品净放款与净实还本金，包含冲正且本息分开；列为贷款产品、净放款本金、净实还本金，列出全部产品，无流水为零。",
            "terms": [
                "放款金额",
                "放款入账日期",
                "实还本金",
                "还款入账日期",
                "贷款产品名称",
            ],
            "relevant": [
                "credit_ops.disbursements.amount",
                "credit_ops.repayment_transactions.principal_paid",
            ],
            "sql": """WITH d AS (SELECT l.product_id,SUM(d.amount) AS amount FROM disbursements d JOIN loans l ON d.loan_id=l.loan_id
          WHERE d.disbursed_on BETWEEN DATE '2026-09-01' AND DATE '2026-09-30' AND d.status IN ('success','reversal') GROUP BY l.product_id),
          r AS (SELECT l.product_id,SUM(r.principal_paid) AS amount FROM repayment_transactions r JOIN loans l ON r.loan_id=l.loan_id
          WHERE r.paid_on BETWEEN DATE '2026-09-01' AND DATE '2026-09-30' AND r.status IN ('success','reversal') GROUP BY l.product_id)
          SELECT p.product_name AS 贷款产品,COALESCE(d.amount,0) AS 净放款本金,COALESCE(r.amount,0) AS 净实还本金 FROM loan_products p
          LEFT JOIN d ON p.product_id=d.product_id LEFT JOIN r ON p.product_id=r.product_id ORDER BY p.product_name""",
        },
        {
            "id": "repeat_borrower_ranking",
            "question": "截至2026年9月30日，借据至少两笔的客户按笔数降序取前十名，同笔数按客户编号升序，输出客户编号、借据笔数、累计初始本金。",
            "terms": ["借据编号", "客户编号", "初始贷款本金", "放款日期"],
            "relevant": ["credit_ops.loans.customer_id", "credit_ops.loans.principal"],
            "sql": """SELECT customer_id AS 客户编号,COUNT(*) AS 借据笔数,SUM(principal) AS 累计初始本金 FROM loans
          WHERE originated_on <= DATE '2026-09-30' GROUP BY customer_id HAVING COUNT(*)>=2 ORDER BY 借据笔数 DESC,客户编号 LIMIT 10""",
        },
        {
            "id": "approval_duration",
            "question": "对2026年9月完成最终通过或拒绝审批的申请，按机构统计从申请到最终审批的平均天数（两位小数），输出机构、平均审批天数。",
            "terms": [
                "审批完成日期",
                "申请日期",
                "最终审批标记",
                "审批结果",
                "机构名称",
            ],
            "relevant": [
                "credit_ops.approval_events.decided_on",
                "credit_ops.applications.applied_on",
            ],
            "sql": """SELECT o.org_name AS 机构,ROUND(AVG(DATE_DIFF('day',a.applied_on,e.decided_on)),2) AS 平均审批天数 FROM approval_events e
          JOIN applications a ON e.application_id=a.application_id JOIN org_units o ON a.org_id=o.org_id
          WHERE e.is_final=1 AND e.decision IN ('approved','rejected') AND e.decided_on BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'
          GROUP BY o.org_name ORDER BY o.org_name""",
        },
        {
            "id": "historical_events",
            "question": "请从流水、还款计划和分配关系重算2026年6月30日日终状态，不使用快照；按机构输出机构、本金余额、逾期应还本金、最大逾期天数。逾期按本息未还清及到期日早于观察日计算。",
            "terms": [
                "实还本金",
                "分配本金",
                "分配利息",
                "到期日期",
                "应还本金",
                "应还利息",
                "放款金额",
            ],
            "relevant": [
                "credit_ops.repayment_schedules.due_on",
                "credit_ops.repayment_allocations.principal_paid",
            ],
            "sql": "WITH state AS ("
            + historical_state_sql("2026-06-30")
            + """ ) SELECT o.org_name AS 机构,SUM(s.outstanding_principal) AS 本金余额,
          SUM(s.overdue_principal) AS 逾期应还本金,MAX(s.dpd) AS 最大逾期天数 FROM state s JOIN loans l ON s.loan_id=l.loan_id
          JOIN org_units o ON l.org_id=o.org_id GROUP BY o.org_name ORDER BY o.org_name""",
        },
        {
            "id": "window_cumulative",
            "question": "显示2026年1至9月每月申请笔数和年内累计申请笔数，按月份排序，列为月份、申请笔数、累计申请笔数。",
            "terms": ["申请日期", "申请编号"],
            "relevant": ["credit_ops.applications.applied_on"],
            "sql": """SELECT STRFTIME(applied_on,'%Y-%m') AS 月份,COUNT(*) AS 申请笔数,
          SUM(COUNT(*)) OVER (ORDER BY STRFTIME(applied_on,'%Y-%m')) AS 累计申请笔数 FROM applications
          WHERE applied_on BETWEEN DATE '2026-01-01' AND DATE '2026-09-30' GROUP BY STRFTIME(applied_on,'%Y-%m') ORDER BY 月份""",
        },
    ]


def reference(case_id, data, orgs):
    apps = {r["application_id"]: r for r in data["applications"] if r["org_id"] in orgs}
    loans = {r["loan_id"]: r for r in data["loans"] if r["org_id"] in orgs}
    products = {r["product_id"]: r["product_name"] for r in data["loan_products"]}
    org_names = {r["org_id"]: r["org_name"] for r in data["org_units"]}
    in_month = lambda d: "2026-09-01" <= d <= "2026-09-30"
    rounded = lambda v: v.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    if case_id == "application_detail":
        customers = {r["customer_id"]: r["customer_segment"] for r in data["customers"]}
        rows = sorted(
            (r for r in apps.values() if in_month(r["applied_on"])),
            key=lambda r: (-Decimal(r["requested_amount"]), r["application_id"]),
        )[:5]
        return [
            {
                "申请编号": r["application_id"],
                "客户群体": customers[r["customer_id"]],
                "申请金额": Decimal(r["requested_amount"]),
            }
            for r in rows
        ]
    if case_id == "loan_statistics":
        groups = defaultdict(list)
        for r in loans.values():
            if in_month(r["originated_on"]):
                groups[products[r["product_id"]]].append(r)
        return [
            {
                "贷款产品": k,
                "借据笔数": len(v),
                "平均初始本金": rounded(
                    sum(Decimal(r["principal"]) for r in v) / len(v)
                ),
                "最长期限月数": max(int(r["term_months"]) for r in v),
            }
            for k, v in sorted(groups.items())
        ]
    if case_id == "separate_fact_join":
        amounts = {p: [Decimal(0), Decimal(0)] for p in products}
        for table, date_col, amount_col, index in [
            ("disbursements", "disbursed_on", "amount", 0),
            ("repayment_transactions", "paid_on", "principal_paid", 1),
        ]:
            for r in data[table]:
                if (
                    r["loan_id"] in loans
                    and in_month(r[date_col])
                    and r["status"] in ("success", "reversal")
                ):
                    amounts[loans[r["loan_id"]]["product_id"]][index] += Decimal(
                        r[amount_col]
                    )
        return [
            {"贷款产品": products[p], "净放款本金": v[0], "净实还本金": v[1]}
            for p, v in sorted(amounts.items(), key=lambda x: products[x[0]])
        ]
    if case_id == "repeat_borrower_ranking":
        groups = defaultdict(list)
        for r in loans.values():
            if r["originated_on"] <= "2026-09-30":
                groups[r["customer_id"]].append(r)
        ranked = sorted(
            ((k, v) for k, v in groups.items() if len(v) >= 2),
            key=lambda x: (-len(x[1]), x[0]),
        )[:10]
        return [
            {
                "客户编号": k,
                "借据笔数": len(v),
                "累计初始本金": sum(Decimal(r["principal"]) for r in v),
            }
            for k, v in ranked
        ]
    if case_id == "approval_duration":
        groups = defaultdict(list)
        for r in data["approval_events"]:
            a = apps.get(r["application_id"])
            if (
                a
                and r["is_final"] == "1"
                and r["decision"] in ("approved", "rejected")
                and in_month(r["decided_on"])
            ):
                groups[org_names[a["org_id"]]].append(
                    (
                        date.fromisoformat(r["decided_on"])
                        - date.fromisoformat(a["applied_on"])
                    ).days
                )
        return [
            {"机构": k, "平均审批天数": rounded(Decimal(sum(v)) / len(v))}
            for k, v in sorted(groups.items())
        ]
    if case_id == "historical_events":
        groups = defaultdict(lambda: [Decimal(0), Decimal(0), 0])
        for r in historical_reference(data, "2026-06-30", orgs):
            group = groups[org_names[loans[r["loan_id"]]["org_id"]]]
            group[0] += r["outstanding_principal"]
            group[1] += r["overdue_principal"]
            group[2] = max(group[2], r["dpd"])
        return [
            {"机构": k, "本金余额": v[0], "逾期应还本金": v[1], "最大逾期天数": v[2]}
            for k, v in sorted(groups.items())
        ]
    if case_id == "window_cumulative":
        counts = defaultdict(int)
        for r in apps.values():
            if "2026-01-01" <= r["applied_on"] <= "2026-09-30":
                counts[r["applied_on"][:7]] += 1
        total = 0
        output = []
        for k, v in sorted(counts.items()):
            total += v
            output.append({"月份": k, "申请笔数": v, "累计申请笔数": total})
        return output
    raise KeyError(case_id)


def compare_rows(actual, expected):
    if len(actual) != len(expected):
        return False
    for row, ref in zip(actual, expected):
        if set(row) != set(ref):
            return False
        for col, value in ref.items():
            if isinstance(value, (Decimal, int)):
                try:
                    if row[col] is None or abs(
                        Decimal(str(row[col])) - Decimal(value)
                    ) > Decimal(".000001"):
                        return False
                except Exception:
                    return False
            elif row[col] != value:
                return False
    return True
