"""可复现的合成信贷数据；历史快照只使用当日及以前的实还事件。"""

from __future__ import annotations
import csv
import hashlib
import json
import random
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from .config import BASE_DIR
from .credit.schema import SCHEMA, RELATIONS, SCHEMA_VERSION

START, END = date(2025, 1, 1), date(2026, 9, 30)


def month_after(d, months):
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def amount(cents):
    return f"{Decimal(cents) / 100:.2f}"


def seed_demo_data(seed=20261006, database_dir: Path | None = None, applications=1200):
    rng = random.Random(seed)
    folder = database_dir or BASE_DIR / "data/databases/credit_ops"
    folder.mkdir(parents=True, exist_ok=True)
    rows = {t["name"]: [] for t in SCHEMA}
    rows["org_units"] = [
        ("O1", "上海分行", "华东"),
        ("O2", "杭州分行", "华东"),
        ("O3", "广州分行", "华南"),
        ("O4", "北京分行", "华北"),
    ]
    rows["channels"] = [("CH1", "手机银行"), ("CH2", "线下网点"), ("CH3", "合作渠道")]
    rows["loan_products"] = [
        ("P1", "薪享贷", 12, "0.080000"),
        ("P2", "消费分期贷", 6, "0.120000"),
        ("P3", "优享贷", 12, "0.100000"),
    ]
    customer_count = max(12, applications // 3)
    rows["customers"] = [
        (
            f"C{i:05}",
            f"模拟客户{i:05}",
            f"139{i:08}",
            ["工薪", "自雇", "其他"][i % 3],
            f"O{i % 4 + 1}",
            START,
        )
        for i in range(1, customer_count + 1)
    ]
    snapshot_count = 0
    with (folder / "loan_daily_snapshots.csv").open(
        "w", newline="", encoding="utf-8"
    ) as sf:
        sw = csv.writer(sf)
        sw.writerow(
            [
                f["name"]
                for f in next(t for t in SCHEMA if t["name"] == "loan_daily_snapshots")[
                    "fields"
                ]
            ]
        )
        for i in range(1, applications + 1):
            app_id, loan_id = f"A{i:06}", f"L{i:06}"
            customer_index = min(customer_count - 1, (i - 1) // 3)
            applied = START + timedelta(
                days=min(
                    (END - START).days - 3,
                    customer_index * ((END - START).days - 3) // customer_count
                    + ((i - 1) % 3) * 27,
                )
            )
            customer = rows["customers"][customer_index]
            product = rows["loan_products"][(i // 4) % 3]
            attrs = (customer[0], product[0], f"CH{(i // 3) % 3 + 1}", customer[4])
            principal = rng.randrange(20, 801) * 10000
            status = (
                "withdrawn" if i % 13 == 0 else "rejected" if i % 5 == 0 else "approved"
            )
            rows["applications"].append(
                (app_id, *attrs, applied, amount(principal), status)
            )
            if status == "withdrawn":
                continue
            rows["approval_events"].extend(
                [
                    (f"E{i:06}a", app_id, applied, "review", 0),
                    (f"E{i:06}b", app_id, applied + timedelta(days=1), status, 1),
                ]
            )
            if status != "approved":
                continue
            originated = applied + timedelta(days=2)
            term, rate = product[2], Decimal(product[3])
            rows["loans"].append(
                (
                    loan_id,
                    app_id,
                    *attrs,
                    originated,
                    amount(principal),
                    term,
                    str(rate),
                )
            )
            rows["disbursements"].append(
                (f"D{i:06}", loan_id, originated, amount(principal), "success")
            )
            if i % 29 == 0:
                rows["disbursements"].extend(
                    [
                        (
                            f"D{i:06}r",
                            loan_id,
                            originated,
                            amount(-principal),
                            "reversal",
                        ),
                        (
                            f"D{i:06}b",
                            loan_id,
                            originated,
                            amount(principal),
                            "success",
                        ),
                    ]
                )
            schedules, payments = [], []
            for k in range(1, term + 1):
                sid = f"{loan_id}S{k:02}"
                due = month_after(originated, k)
                p = (
                    principal // term
                    if k < term
                    else principal - (principal // term) * (term - 1)
                )
                interest = int(
                    (
                        Decimal(principal - principal // term * (k - 1)) * rate / 12
                    ).quantize(Decimal("1"))
                )
                schedules.append((sid, due, p, interest))
                rows["repayment_schedules"].append(
                    (sid, loan_id, k, due, amount(p), amount(interest))
                )
                behavior = i % 11
                paid_on = (
                    month_after(originated, 1)
                    if behavior == 7
                    else due
                    + timedelta(days=45 if behavior == 2 else 7 if behavior == 3 else 0)
                )
                if behavior == 1:
                    continue
                chunks = (
                    [(paid_on, p, interest)]
                    if behavior != 4
                    else [
                        (paid_on, p // 2, interest),
                        (paid_on + timedelta(days=20), p - p // 2, 0),
                    ]
                )
                for j, (pd, pp, ip) in enumerate(chunks):
                    if pd > END:
                        continue
                    tx = f"{sid}T{j}"
                    rows["repayment_transactions"].append(
                        (tx, loan_id, pd, amount(pp), amount(ip), "success")
                    )
                    rows["repayment_allocations"].append(
                        (tx + "X", tx, sid, amount(pp), amount(ip))
                    )
                    payments.append((pd, sid, pp, ip))
                    if i % 31 == 0 and k == 1 and j == 0:
                        for suffix, sign, st in [
                            ("r", -1, "reversal"),
                            ("b", 1, "success"),
                        ]:
                            tid = tx + suffix
                            rows["repayment_transactions"].append(
                                (
                                    tid,
                                    loan_id,
                                    pd,
                                    amount(pp * sign),
                                    amount(ip * sign),
                                    st,
                                )
                            )
                            rows["repayment_allocations"].append(
                                (
                                    tid + "X",
                                    tid,
                                    sid,
                                    amount(pp * sign),
                                    amount(ip * sign),
                                )
                            )
                            payments.append((pd, sid, pp * sign, ip * sign))
            payments.sort(key=lambda x: x[0])
            paid = {s[0]: [0, 0] for s in schedules}
            pointer = 0
            for offset in range((END - originated).days + 1):
                at = originated + timedelta(days=offset)
                while pointer < len(payments) and payments[pointer][0] <= at:
                    _, sid, pp, ip = payments[pointer]
                    paid[sid][0] += pp
                    paid[sid][1] += ip
                    pointer += 1
                outstanding = principal - sum(v[0] for v in paid.values())
                overdue_items = [
                    s
                    for s in schedules
                    if s[1] < at and (s[2] > paid[s[0]][0] or s[3] > paid[s[0]][1])
                ]
                overdue = sum(max(0, s[2] - paid[s[0]][0]) for s in overdue_items)
                dpd = max(((at - s[1]).days for s in overdue_items), default=0)
                state = (
                    "closed"
                    if outstanding == 0 and not overdue_items
                    else "overdue"
                    if dpd
                    else "current"
                )
                sw.writerow(
                    (loan_id, at, amount(outstanding), amount(overdue), dpd, state)
                )
                snapshot_count += 1
    for table in SCHEMA:
        if table["name"] == "loan_daily_snapshots":
            continue
        with (folder / (table["name"] + ".csv")).open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            w = csv.writer(handle)
            w.writerow([f["name"] for f in table["fields"]])
            w.writerows(rows[table["name"]])
    counts = {name: len(data) for name, data in rows.items()}
    counts["loan_daily_snapshots"] = snapshot_count
    hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.glob("*.csv"))
    }
    version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[
        :16
    ]
    manifest = dict(
        database="credit_ops",
        scenario="零售信贷经营分析与贷后风险指标查询",
        synthetic=True,
        schema_version=SCHEMA_VERSION,
        data_version=version,
        seed=seed,
        data_range={"start": str(START), "end": str(END)},
        data_as_of=str(END),
        demo_reference_date="2026-10-01",
        timezone="Asia/Shanghai",
        currency="CNY",
        table_counts=counts,
        file_hashes=hashes,
    )
    (folder / "_database_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "_schema.json").write_text(
        json.dumps(
            dict(schema_version=SCHEMA_VERSION, tables=SCHEMA, relations=RELATIONS),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return counts
