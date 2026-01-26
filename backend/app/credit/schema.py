"""金融 Schema 的唯一声明入口；粒度与关联基数属于业务契约。"""

DATABASE = "credit_ops"
SCHEMA_VERSION = "credit-1.0.0"


def f(
    name, label, sql_type="VARCHAR", role="dimension", sensitive=False, description=""
):
    kind = (
        "日期"
        if sql_type == "DATE"
        else "数值"
        if sql_type.startswith("DECIMAL")
        else "整数"
        if sql_type == "INTEGER"
        else "文本"
    )
    return dict(
        name=name,
        label=label,
        type=kind,
        source_type=sql_type,
        description=description or label,
        aliases=[label],
        role=role,
        aggregation="sum" if role == "metric" else "none",
        sensitive=sensitive,
    )


def key(name, label):
    return f(name, label, role="identifier")


def money(name, label):
    return f(
        name, label, "DECIMAL(18,2)", "metric", description=label + "，人民币元，定点数"
    )


def day(name, label):
    return f(name, label, "DATE", "time")


def attrs():
    return [
        key("customer_id", "客户编号"),
        key("product_id", "贷款产品编号"),
        key("channel_id", "申请来源渠道编号"),
        key("org_id", "业务归属机构编号"),
    ]


def t(name, label, grain, pk, fields):
    return dict(
        id=f"{DATABASE}.{name}",
        name=name,
        label=label,
        database=DATABASE,
        domain="零售信贷",
        description=f"{label}；每行{grain}",
        grain=grain,
        primary_key=pk.split(","),
        business_terms=[label, "信贷"],
        fields=fields,
    )


SCHEMA = [
    t(
        "customers",
        "客户",
        "一名客户",
        "customer_id",
        [
            key("customer_id", "客户编号"),
            f("customer_name", "客户姓名", sensitive=True),
            f("phone", "联系电话", sensitive=True),
            f("customer_segment", "客户群体"),
            key("org_id", "客户归属机构编号"),
            day("registered_on", "登记日期"),
        ],
    ),
    t(
        "loan_products",
        "贷款产品",
        "一个固定期限贷款产品",
        "product_id",
        [
            key("product_id", "贷款产品编号"),
            f("product_name", "贷款产品名称"),
            f("term_months", "期限月数", "INTEGER"),
            f("annual_rate", "年利率", "DECIMAL(8,6)"),
        ],
    ),
    t(
        "channels",
        "申请渠道",
        "一个来源渠道",
        "channel_id",
        [key("channel_id", "渠道编号"), f("channel_name", "渠道名称")],
    ),
    t(
        "org_units",
        "机构",
        "一个机构",
        "org_id",
        [key("org_id", "机构编号"), f("org_name", "机构名称"), f("region", "地区")],
    ),
    t(
        "applications",
        "贷款申请",
        "一次申请",
        "application_id",
        [
            key("application_id", "申请编号"),
            *attrs(),
            day("applied_on", "申请日期"),
            money("requested_amount", "申请金额"),
            f("application_status", "申请状态"),
        ],
    ),
    t(
        "approval_events",
        "审批事件",
        "一次审批事件，最终事件 is_final=1",
        "event_id",
        [
            key("event_id", "审批事件编号"),
            key("application_id", "申请编号"),
            day("decided_on", "审批完成日期"),
            f("decision", "审批结果"),
            f("is_final", "最终审批标记", "INTEGER"),
        ],
    ),
    t(
        "loans",
        "贷款借据",
        "一笔贷款借据",
        "loan_id",
        [
            key("loan_id", "借据编号"),
            key("application_id", "申请编号"),
            *attrs(),
            day("originated_on", "放款日期"),
            money("principal", "初始贷款本金"),
            f("term_months", "期限月数", "INTEGER"),
            f("annual_rate", "年利率", "DECIMAL(8,6)"),
        ],
    ),
    t(
        "disbursements",
        "放款流水",
        "一次放款事件，冲正为负",
        "disbursement_id",
        [
            key("disbursement_id", "放款流水编号"),
            key("loan_id", "借据编号"),
            day("disbursed_on", "放款入账日期"),
            money("amount", "放款金额"),
            f("status", "放款状态"),
        ],
    ),
    t(
        "repayment_schedules",
        "应还计划",
        "一笔贷款的一期计划",
        "schedule_id",
        [
            key("schedule_id", "计划编号"),
            key("loan_id", "借据编号"),
            f("installment_no", "期次", "INTEGER"),
            day("due_on", "到期日期"),
            money("principal_due", "应还本金"),
            money("interest_due", "应还利息"),
        ],
    ),
    t(
        "repayment_transactions",
        "实还流水",
        "一次还款事件，冲正为负",
        "transaction_id",
        [
            key("transaction_id", "还款流水编号"),
            key("loan_id", "借据编号"),
            day("paid_on", "还款入账日期"),
            money("principal_paid", "实还本金"),
            money("interest_paid", "实还利息"),
            f("status", "还款状态"),
        ],
    ),
    t(
        "repayment_allocations",
        "还款分配明细",
        "一笔还款分配至一期计划",
        "allocation_id",
        [
            key("allocation_id", "分配编号"),
            key("transaction_id", "还款流水编号"),
            key("schedule_id", "计划编号"),
            money("principal_paid", "分配本金"),
            money("interest_paid", "分配利息"),
        ],
    ),
    t(
        "loan_daily_snapshots",
        "贷款日快照",
        "一笔借据一个自然日的日终状态",
        "loan_id,snapshot_on",
        [
            key("loan_id", "借据编号"),
            day("snapshot_on", "日终观察日期"),
            money("outstanding_principal", "未偿贷款本金余额"),
            money("overdue_principal", "已到期未偿本金"),
            f(
                "dpd",
                "逾期天数",
                "INTEGER",
                description="观察日减最早未还清到期日；无宽限期；到期当日为0",
            ),
            f("loan_status", "日终贷款状态"),
        ],
    ),
]
RELATIONS = []


def relation(left, col, right, target=None):
    RELATIONS.append(
        dict(
            left_table=f"{DATABASE}.{left}",
            left_field=col,
            right_table=f"{DATABASE}.{right}",
            right_field=target or col,
            cardinality="many_to_one",
            relation_type="foreign_key",
            description="多对一；度量按源表粒度聚合，禁止关联后重复累计",
        )
    )


relation("customers", "org_id", "org_units")
for source in ("applications", "loans"):
    for column, target in [
        ("customer_id", "customers"),
        ("product_id", "loan_products"),
        ("channel_id", "channels"),
        ("org_id", "org_units"),
    ]:
        relation(source, column, target)
relation("approval_events", "application_id", "applications")
relation("loans", "application_id", "applications")
for source in (
    "disbursements",
    "repayment_schedules",
    "repayment_transactions",
    "loan_daily_snapshots",
):
    relation(source, "loan_id", "loans")
relation("repayment_allocations", "transaction_id", "repayment_transactions")
relation("repayment_allocations", "schedule_id", "repayment_schedules")
TABLE_MAP = {table["name"]: table for table in SCHEMA}
