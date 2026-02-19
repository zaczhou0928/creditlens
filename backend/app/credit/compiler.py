"""将经校验的指标计划编译为只读 SQL，固定粒度、时间列、分母与关联。"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from calendar import monthrange
import sqlglot
from sqlglot import exp
from .metrics import METRICS, DIMENSIONS
from .plan import QueryPlan
from .schema import TABLE_MAP

ATTRS = "l.customer_id, l.product_id, l.channel_id, l.org_id"
BASES = {
    "applications": "SELECT a.application_id, a.customer_id, a.product_id, a.channel_id, a.org_id, a.applied_on FROM applications a",
    "approvals": "SELECT a.application_id, a.customer_id, a.product_id, a.channel_id, a.org_id, e.decided_on, e.decision FROM applications a JOIN approval_events e ON a.application_id=e.application_id WHERE e.is_final=1 AND e.decision IN ('approved','rejected')",
    "disbursements": f"SELECT {ATTRS}, d.loan_id, d.disbursed_on, d.amount FROM disbursements d JOIN loans l ON d.loan_id=l.loan_id WHERE d.status IN ('success','reversal')",
    "borrowers": f"SELECT {ATTRS}, d.loan_id, d.disbursed_on, d.amount FROM disbursements d JOIN loans l ON d.loan_id=l.loan_id WHERE d.status IN ('success','reversal')",
    "first_borrowers": f"SELECT customer_id,product_id,channel_id,org_id,disbursed_on FROM (SELECT {ATTRS}, d.loan_id, d.disbursed_on, ROW_NUMBER() OVER (PARTITION BY l.customer_id ORDER BY d.disbursed_on,d.loan_id) AS rn FROM disbursements d JOIN loans l ON d.loan_id=l.loan_id WHERE d.status IN ('success','reversal') GROUP BY l.customer_id,l.product_id,l.channel_id,l.org_id,d.loan_id,d.disbursed_on HAVING SUM(d.amount)>0) first_loans WHERE rn=1",
    "repayments": f"SELECT {ATTRS}, r.loan_id, r.paid_on, r.principal_paid, r.interest_paid FROM repayment_transactions r JOIN loans l ON r.loan_id=l.loan_id WHERE r.status IN ('success','reversal')",
    "snapshots": f"SELECT {ATTRS}, s.loan_id, s.snapshot_on, s.outstanding_principal, s.overdue_principal, s.dpd FROM loan_daily_snapshots s JOIN loans l ON s.loan_id=l.loan_id",
}
JOINS = {
    "region": ("o.region", "JOIN org_units o ON b.org_id=o.org_id"),
    "org": ("o.org_name", "JOIN org_units o ON b.org_id=o.org_id"),
    "product": ("p.product_name", "JOIN loan_products p ON b.product_id=p.product_id"),
    "channel": ("c.channel_name", "JOIN channels c ON b.channel_id=c.channel_id"),
    "segment": (
        "u.customer_segment",
        "JOIN customers u ON b.customer_id=u.customer_id",
    ),
}


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def previous_dates(plan):
    if plan.as_of and METRICS[plan.metric_id].time_kind == "snapshot":
        first = plan.as_of.replace(day=1)
        return {"as_of": first - timedelta(days=1)}
    start, end = plan.start_date, plan.end_date
    if (
        start.day == 1
        and end.day == monthrange(end.year, end.month)[1]
        and start.month == end.month
        and start.year == end.year
    ):
        prev_end = start - timedelta(days=1)
        return {"start_date": prev_end.replace(day=1), "end_date": prev_end}
    return {
        "start_date": start - (end - start) - timedelta(days=1),
        "end_date": start - timedelta(days=1),
    }


@dataclass
class CompiledQuery:
    sql: str
    required_fields: list[str]
    column_metadata: dict[str, dict]
    metric: dict


def compile_plan(plan: QueryPlan, manifest: dict) -> CompiledQuery:
    metric = METRICS[plan.metric_id]
    begin = date.fromisoformat(manifest["data_range"]["start"])
    cutoff = date.fromisoformat(manifest["data_as_of"])
    dates = [d for d in (plan.start_date, plan.end_date, plan.as_of) if d]
    if any(d < begin or d > cutoff for d in dates):
        raise ValueError(
            f"数据覆盖 {begin} 至 {cutoff}，请求日期超出范围；未自动改用其他日期"
        )
    dims = []
    joins = []
    conditions = []
    for dimension in dict.fromkeys([*plan.dimensions, *plan.filters]):
        if dimension == "month":
            expression = f"STRFTIME(b.{metric.time_field}, '%Y-%m')"
            join = ""
        else:
            expression, join = JOINS[dimension]
        if join and join not in joins:
            joins.append(join)
        if dimension in plan.dimensions:
            dims.append((dimension, expression))
        if dimension in plan.filters:
            conditions.append(
                expression
                + " IN ("
                + ",".join(quote(v) for v in plan.filters[dimension])
                + ")"
            )
    metric_expr = metric.expression.format(dpd=plan.dpd_threshold)
    metadata = {
        DIMENSIONS[d]: {"kind": "dimension", "unit": "", "precision": 0}
        for d, _ in dims
    }
    metadata[metric.label] = {
        "kind": "ratio"
        if metric.unit == "比例"
        else "money"
        if metric.unit == "元"
        else "count",
        "unit": metric.unit,
        "precision": 6 if metric.unit == "比例" else 2 if metric.unit == "元" else 0,
        "metric_id": metric.id,
        "metric_version": plan.metric_version,
    }
    projections = [f'{expression} AS "{DIMENSIONS[d]}"' for d, expression in dims]
    projections.append(
        f'ROUND({metric_expr}, {6 if metric.unit == "比例" else 2}) AS "{metric.label}"'
    )
    if metric.unit == "比例":
        if metric.id == "approval_rate":
            num = "SUM(CASE WHEN b.decision='approved' THEN 1 ELSE 0 END)"
            den = "COUNT(*)"
        else:
            numerator = (
                "outstanding_principal"
                if metric.id == "overdue_balance_ratio"
                else "overdue_principal"
            )
            num = f"SUM(CASE WHEN b.dpd>={plan.dpd_threshold} THEN b.{numerator} ELSE 0 END)"
            den = "SUM(b.outstanding_principal)"
        projections += [f'{num} AS "分子"', f'{den} AS "分母"']
        for n in ("分子", "分母"):
            metadata[n] = {
                "kind": "count" if metric.id == "approval_rate" else "money",
                "unit": "笔" if metric.id == "approval_rate" else "元",
                "precision": 0 if metric.id == "approval_rate" else 2,
            }

    def select_for(p, label=""):
        if metric.time_kind == "snapshot":
            predicate = (
                f"b.snapshot_on>=DATE {quote(p.start_date)} AND b.snapshot_on<=DATE {quote(p.end_date)} AND b.snapshot_on=LAST_DAY(b.snapshot_on)"
                if "month" in p.dimensions
                else f"b.{metric.time_field}=DATE {quote(p.as_of)}"
            )
        else:
            predicate = f"b.{metric.time_field}>=DATE {quote(p.start_date)} AND b.{metric.time_field}<DATE {quote(p.end_date + timedelta(days=1))}"
        if metric.source == "borrowers":
            # 在查询期间、筛选范围和分组内先求每位客户净额，跨日冲正也会抵销。
            dim_labels = [f'"{DIMENSIONS[d]}"' for d, _ in dims]
            inner_cols = [
                "b.customer_id",
                *[f'{expr} AS "{DIMENSIONS[d]}"' for d, expr in dims],
                "SUM(b.amount) AS net_amount",
            ]
            inner = (
                "SELECT " + ", ".join(inner_cols) + " FROM base b " + " ".join(joins)
            )
            inner += " WHERE " + " AND ".join([predicate, *conditions])
            inner += " GROUP BY " + ", ".join(
                ["b.customer_id", *[expr for _, expr in dims]]
            )
            outer_cols = (
                [f'{quote(label)} AS "比较期间"'] if label else []
            ) + dim_labels
            outer_cols.append(
                f'COUNT(DISTINCT CASE WHEN net_amount>0 THEN customer_id END) AS "{metric.label}"'
            )
            sql = (
                "SELECT " + ", ".join(outer_cols) + " FROM (" + inner + ") customer_net"
            )
            return sql + (" GROUP BY " + ", ".join(dim_labels) if dims else "")
        cols = ([f'{quote(label)} AS "比较期间"'] if label else []) + projections
        sql = (
            "SELECT "
            + ", ".join(cols)
            + " FROM base b "
            + " ".join(joins)
            + " WHERE "
            + " AND ".join([predicate, *conditions])
        )
        if dims:
            sql += " GROUP BY " + ", ".join(expression for _, expression in dims)
        return sql

    body = select_for(plan, "本期" if plan.comparison else "")
    if plan.comparison:
        other = plan.model_copy(update=previous_dates(plan) | {"comparison": False})
        other_dates = (
            [d for d in (other.as_of,) if d]
            if metric.time_kind == "snapshot"
            else [other.start_date, other.end_date]
        )
        if any(d < begin or d > cutoff for d in other_dates):
            raise ValueError("比较期超出数据覆盖范围")
        body += " UNION ALL " + select_for(other, "上期")
        metadata = {
            "比较期间": {"kind": "dimension", "unit": "", "precision": 0},
            **metadata,
        }
    body += " ORDER BY " + (
        ", ".join('"' + DIMENSIONS[d] + '"' for d, _ in dims) if dims else "1"
    )
    sql = f"WITH base AS ({BASES[metric.source]}) {body}"
    tree = sqlglot.parse_one(sql, read="duckdb")
    table_names = {t.name for t in tree.find_all(exp.Table)} & set(TABLE_MAP)
    column_names = {c.name for c in tree.find_all(exp.Column)}
    required = [
        f"credit_ops.{t}.{f['name']}"
        for t in sorted(table_names)
        for f in TABLE_MAP[t]["fields"]
        if f["name"] in column_names
    ]
    return CompiledQuery(sql, required, metadata, metric.public())
