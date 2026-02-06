"""业务审核口径，独立于模型与个人会话记忆。"""

from dataclasses import dataclass, asdict

METRIC_VERSION = "credit-metrics-1.0.0"
DIMENSIONS = {
    "region": "地区",
    "channel": "渠道",
    "product": "贷款产品",
    "org": "机构",
    "segment": "客户群体",
    "month": "月份",
}
DIMENSION_FIELDS = {
    "region": "org_units.region",
    "channel": "channels.channel_name",
    "product": "loan_products.product_name",
    "org": "org_units.org_name",
    "segment": "customers.customer_segment",
}


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    definition: str
    expression: str
    source: str
    time_kind: str
    time_field: str
    unit: str
    grain: str
    required_fields: tuple[str, ...]
    aliases: tuple[str, ...]
    numerator: str = ""
    denominator: str = ""

    def public(self):
        return {
            **asdict(self),
            "version": METRIC_VERSION,
            "effective_from": "2025-01-01",
            "currency": "CNY" if self.unit == "元" else None,
            "zero_denominator": "NULL（无可比存量/无已完成审批），不记为0",
            "allowed_dimensions": list(DIMENSIONS),
            "attribution": "申请或放款时记录的渠道与机构；客户群体使用固定合成属性",
            "rounding": "金额两位小数；比例内部六位小数；日快照不得跨日期求和",
        }


METRICS = {
    m.id: m
    for m in [
        Metric(
            "application_count",
            "申请笔数",
            "期间内提交的申请，含撤回与拒绝，按申请编号去重",
            "COUNT(DISTINCT b.application_id)",
            "applications",
            "period",
            "applied_on",
            "笔",
            "申请",
            ("applications.application_id", "applications.applied_on"),
            ("申请量", "申请笔数", "申请数量"),
        ),
        Metric(
            "applicant_count",
            "申请客户数",
            "期间内提交申请的客户去重数",
            "COUNT(DISTINCT b.customer_id)",
            "applications",
            "period",
            "applied_on",
            "人",
            "客户",
            ("applications.customer_id", "applications.applied_on"),
            ("申请客户数", "申请人数"),
        ),
        Metric(
            "approval_rate",
            "审批通过率",
            "期间内完成最终审批的申请中，通过的比例；仅 approved/rejected，撤回与待审不入分母",
            "SUM(CASE WHEN b.decision='approved' THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*),0)",
            "approvals",
            "period",
            "decided_on",
            "比例",
            "最终审批申请",
            (
                "approval_events.application_id",
                "approval_events.decided_on",
                "approval_events.decision",
                "approval_events.is_final",
                "applications.application_id",
            ),
            ("审批通过率", "通过率"),
            "通过的最终审批申请数",
            "通过或拒绝的最终审批申请数",
        ),
        Metric(
            "disbursement_amount",
            "放款金额",
            "期间内成功放款及冲正的净额；冲正为负",
            "COALESCE(SUM(b.amount),0)",
            "disbursements",
            "period",
            "disbursed_on",
            "元",
            "放款事件",
            (
                "disbursements.loan_id",
                "disbursements.disbursed_on",
                "disbursements.amount",
                "disbursements.status",
            ),
            ("放款金额", "放款额", "贷款投放金额"),
        ),
        Metric(
            "borrower_count",
            "放款客户数",
            "期间内净放款本金大于0的客户去重数",
            "COUNT(DISTINCT b.customer_id)",
            "borrowers",
            "period",
            "disbursed_on",
            "人",
            "客户",
            (
                "disbursements.loan_id",
                "disbursements.disbursed_on",
                "disbursements.amount",
                "loans.customer_id",
            ),
            ("放款客户数", "放款人数"),
        ),
        Metric(
            "new_borrower_count",
            "首次放款客户数",
            "客户历史首次净成功放款日落在期间内；同日多笔按日期和借据编号确定归属",
            "COUNT(DISTINCT b.customer_id)",
            "first_borrowers",
            "period",
            "disbursed_on",
            "人",
            "客户首次放款",
            (
                "loans.customer_id",
                "loans.loan_id",
                "disbursements.loan_id",
                "disbursements.disbursed_on",
                "disbursements.amount",
            ),
            ("首次放款客户数", "首贷客户数", "新增放款客户数"),
        ),
        Metric(
            "loan_balance",
            "贷款本金余额",
            "指定日终未偿本金；按借据去重，不跨快照日期求和",
            "COALESCE(SUM(b.outstanding_principal),0)",
            "snapshots",
            "snapshot",
            "snapshot_on",
            "元",
            "借据日快照",
            (
                "loan_daily_snapshots.loan_id",
                "loan_daily_snapshots.snapshot_on",
                "loan_daily_snapshots.outstanding_principal",
            ),
            ("贷款本金余额", "贷款余额", "本金余额"),
        ),
        Metric(
            "repaid_principal",
            "实还本金",
            "期间内成功还款及冲正的本金净额",
            "COALESCE(SUM(b.principal_paid),0)",
            "repayments",
            "period",
            "paid_on",
            "元",
            "还款事件",
            (
                "repayment_transactions.loan_id",
                "repayment_transactions.paid_on",
                "repayment_transactions.principal_paid",
                "repayment_transactions.status",
            ),
            ("实还本金", "归还本金", "还款本金"),
        ),
        Metric(
            "repaid_interest",
            "实还利息",
            "期间内成功还款及冲正的利息净额",
            "COALESCE(SUM(b.interest_paid),0)",
            "repayments",
            "period",
            "paid_on",
            "元",
            "还款事件",
            (
                "repayment_transactions.loan_id",
                "repayment_transactions.paid_on",
                "repayment_transactions.interest_paid",
                "repayment_transactions.status",
            ),
            ("实还利息", "还款利息"),
        ),
        Metric(
            "overdue_loan_count",
            "逾期贷款笔数",
            "观察日逾期天数达到阈值且未偿本金大于0的借据数",
            "COUNT(DISTINCT CASE WHEN b.dpd >= {dpd} AND b.outstanding_principal > 0 THEN b.loan_id END)",
            "snapshots",
            "snapshot",
            "snapshot_on",
            "笔",
            "借据日快照",
            (
                "loan_daily_snapshots.loan_id",
                "loan_daily_snapshots.snapshot_on",
                "loan_daily_snapshots.dpd",
                "loan_daily_snapshots.outstanding_principal",
            ),
            ("逾期贷款笔数", "逾期笔数"),
        ),
        Metric(
            "overdue_balance_ratio",
            "逾期贷款余额占比",
            "分子为达到逾期阈值的整笔贷款未偿本金，分母为同日全部未偿本金",
            "SUM(CASE WHEN b.dpd >= {dpd} THEN b.outstanding_principal ELSE 0 END) / NULLIF(SUM(b.outstanding_principal),0)",
            "snapshots",
            "snapshot",
            "snapshot_on",
            "比例",
            "借据日快照",
            (
                "loan_daily_snapshots.loan_id",
                "loan_daily_snapshots.snapshot_on",
                "loan_daily_snapshots.dpd",
                "loan_daily_snapshots.outstanding_principal",
            ),
            ("逾期贷款余额占比", "逾期余额率"),
            "达到逾期阈值的借据全部未偿本金",
            "同日所有借据未偿本金",
        ),
        Metric(
            "overdue_principal_ratio",
            "逾期应还本金占比",
            "分子只含达到逾期阈值贷款的已到期未偿本金，分母为同日全部未偿本金",
            "SUM(CASE WHEN b.dpd >= {dpd} THEN b.overdue_principal ELSE 0 END) / NULLIF(SUM(b.outstanding_principal),0)",
            "snapshots",
            "snapshot",
            "snapshot_on",
            "比例",
            "借据日快照",
            (
                "loan_daily_snapshots.loan_id",
                "loan_daily_snapshots.snapshot_on",
                "loan_daily_snapshots.dpd",
                "loan_daily_snapshots.overdue_principal",
                "loan_daily_snapshots.outstanding_principal",
            ),
            ("逾期应还本金占比", "逾期本金率"),
            "达到逾期阈值借据的已到期未偿本金",
            "同日所有借据未偿本金",
        ),
        Metric(
            "overdue_principal",
            "逾期应还本金",
            "达到逾期阈值借据中已到期未偿本金合计",
            "COALESCE(SUM(CASE WHEN b.dpd >= {dpd} THEN b.overdue_principal ELSE 0 END),0)",
            "snapshots",
            "snapshot",
            "snapshot_on",
            "元",
            "借据日快照",
            (
                "loan_daily_snapshots.loan_id",
                "loan_daily_snapshots.snapshot_on",
                "loan_daily_snapshots.dpd",
                "loan_daily_snapshots.overdue_principal",
            ),
            ("逾期应还本金", "逾期本金金额"),
        ),
    ]
}


def metric_catalog():
    return [m.public() for m in METRICS.values()]


def knowledge_search(query):
    terms = query.lower()
    candidates = []
    for m in METRICS.values():
        score = sum(len(a) for a in (*m.aliases, m.label) if a in terms)
        if score:
            candidates.append((score, m.public()))
    return [item for _, item in sorted(candidates, key=lambda x: x[0], reverse=True)]
