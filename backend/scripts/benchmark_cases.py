"""Project-authored query candidates; never imported by the Agent.

Gold SQL is evaluation-only. Freeze results before live runs; no gold feedback is
sent to the model. These are authored project cases, not an unseen benchmark.
"""
from pathlib import Path
import json

from app.config import BASE_DIR
from app.credit.knowledge import historical_state_sql


def query_cases():
    items = json.loads((BASE_DIR / 'evaluation/credit_v1/questions.json').read_text())
    questions = {int(case_id.rsplit('-', 1)[1]): question for case_id, question in items.items()}
    specs = []

    def add(n, sql, ordered=False, scales=None, note=''):
        specs.append(dict(id=f'CREDIT-Q-{n:03}', kind='query', question=questions[n],
                          difficulty='简单' if n <= 10 else '中等' if n <= 26 else '复杂',
                          user='credit_analyst', gold_sql=sql, ordered=ordered,
                          scales=scales or {}, note=note, seen_status='authored_project_cases_not_blind_holdout'))

    jul = lambda col: f"{col} >= DATE '2026-07-01' AND {col} < DATE '2026-08-01'"
    valid = lambda col: f"{col} IN ('success','reversal')"
    add(1, f"SELECT COUNT(DISTINCT application_id) AS 申请笔数 FROM applications WHERE {jul('applied_on')}")
    add(2, f"SELECT COUNT(DISTINCT customer_id) AS 申请客户数 FROM applications WHERE {jul('applied_on')}")
    add(3, f"SELECT application_status AS 申请状态,COUNT(DISTINCT application_id) AS 申请笔数 FROM applications WHERE {jul('applied_on')} GROUP BY application_status")
    add(4, f"SELECT MIN(requested_amount) AS 最小申请金额,MAX(requested_amount) AS 最大申请金额,ROUND(AVG(requested_amount),2) AS 平均申请金额 FROM applications WHERE {jul('applied_on')}")
    add(5, f"SELECT COUNT(DISTINCT loan_id) AS 借据笔数,ROUND(AVG(term_months),2) AS 平均期限月数 FROM loans WHERE {jul('originated_on')}")
    add(6, f"SELECT applied_on AS 申请日期,COUNT(DISTINCT application_id) AS 申请笔数 FROM applications WHERE {jul('applied_on')} GROUP BY applied_on ORDER BY applied_on", True)
    add(7, f"SELECT COALESCE(SUM(principal_paid),0) AS 实还本金 FROM repayment_transactions WHERE {jul('paid_on')} AND {valid('status')}")
    add(8, f"SELECT COALESCE(SUM(interest_paid),0) AS 实还利息 FROM repayment_transactions WHERE {jul('paid_on')} AND {valid('status')}")
    add(9, f"SELECT COALESCE(SUM(amount),0) AS 净放款金额 FROM disbursements WHERE {jul('disbursed_on')} AND {valid('status')}")
    add(10, "SELECT customer_segment AS 客户群体,COUNT(DISTINCT customer_id) AS 登记客户数 FROM customers WHERE registered_on<=DATE '2026-07-31' GROUP BY customer_segment")
    add(11, f"SELECT c.channel_name AS 渠道,COUNT(DISTINCT a.application_id) AS 申请笔数 FROM applications a JOIN channels c ON a.channel_id=c.channel_id WHERE {jul('a.applied_on')} GROUP BY c.channel_name")
    approvals = "FROM approval_events e JOIN applications a ON e.application_id=a.application_id JOIN org_units o ON a.org_id=o.org_id"
    final = "e.is_final=1 AND e.decision IN ('approved','rejected')"
    add(12, f"SELECT o.org_name AS 机构,COUNT(DISTINCT CASE WHEN e.decision='approved' THEN e.application_id END) AS 通过数,COUNT(DISTINCT e.application_id) AS 最终审批总数,COUNT(DISTINCT CASE WHEN e.decision='approved' THEN e.application_id END)*1.0/NULLIF(COUNT(DISTINCT e.application_id),0) AS 审批通过率 {approvals} WHERE {jul('e.decided_on')} AND {final} GROUP BY o.org_name", scales={'审批通过率':6})
    disb_join = "FROM disbursements d JOIN loans l ON d.loan_id=l.loan_id"
    product = "JOIN loan_products p ON l.product_id=p.product_id"
    add(13, f"SELECT p.product_name AS 贷款产品,SUM(d.amount) AS 净放款金额 {disb_join} {product} WHERE d.disbursed_on BETWEEN DATE '2026-07-01' AND DATE '2026-09-30' AND {valid('d.status')} GROUP BY p.product_name")
    add(14, f"SELECT p.product_name AS 贷款产品,COUNT(DISTINCT l.loan_id) AS 借据笔数,SUM(l.principal) AS 初始本金合计,MAX(l.term_months) AS 最长期限月数 FROM loans l {product} WHERE {jul('l.originated_on')} GROUP BY p.product_name")
    add(15, f"SELECT c.customer_segment AS 客户群体,ROUND(AVG(a.requested_amount),2) AS 平均申请金额 FROM applications a JOIN customers c ON a.customer_id=c.customer_id WHERE {jul('a.applied_on')} GROUP BY c.customer_segment")
    add(16, f"WITH n AS (SELECT l.channel_id,l.customer_id,SUM(d.amount) AS amount {disb_join} WHERE {jul('d.disbursed_on')} AND {valid('d.status')} GROUP BY l.channel_id,l.customer_id) SELECT c.channel_name AS 渠道,COUNT(DISTINCT n.customer_id) AS 放款客户数 FROM n JOIN channels c ON n.channel_id=c.channel_id WHERE n.amount>0 GROUP BY c.channel_name")
    first = f"net AS (SELECT l.customer_id,l.channel_id,l.product_id,l.loan_id,d.disbursed_on,SUM(d.amount) AS amount {disb_join} WHERE {valid('d.status')} GROUP BY l.customer_id,l.channel_id,l.product_id,l.loan_id,d.disbursed_on HAVING SUM(d.amount)>0), firsts AS (SELECT customer_id,channel_id,product_id,disbursed_on,ROW_NUMBER() OVER(PARTITION BY customer_id ORDER BY disbursed_on,loan_id) AS rn FROM net)"
    add(17, f"WITH {first} SELECT c.channel_name AS 渠道,COUNT(DISTINCT f.customer_id) AS 首次放款客户数 FROM firsts f JOIN channels c ON f.channel_id=c.channel_id WHERE f.rn=1 AND {jul('f.disbursed_on')} GROUP BY c.channel_name")
    snap = "FROM loan_daily_snapshots s JOIN loans l ON s.loan_id=l.loan_id"
    org = "JOIN org_units o ON l.org_id=o.org_id"
    at = "s.snapshot_on=DATE '2026-07-31'"
    add(18, f"SELECT p.product_name AS 贷款产品,COUNT(DISTINCT CASE WHEN s.dpd>=1 AND s.outstanding_principal>0 THEN s.loan_id END) AS 逾期贷款笔数 {snap} {product} WHERE {at} GROUP BY p.product_name")
    add(19, f"SELECT o.org_name AS 机构,SUM(s.outstanding_principal) AS 贷款本金余额 {snap} {org} WHERE {at} GROUP BY o.org_name", note='snapshot/event alternatives; retrieval gold path is snapshot only')
    add(20, f"SELECT o.org_name AS 机构,SUM(CASE WHEN s.dpd>=1 THEN s.overdue_principal ELSE 0 END) AS 逾期应还本金 {snap} {org} WHERE {at} GROUP BY o.org_name")
    for n,col,label in [(21,'outstanding_principal','逾期贷款余额占比'),(22,'overdue_principal','逾期应还本金占比')]:
        add(n, f"SELECT o.org_name AS 机构,SUM(CASE WHEN s.dpd>=1 THEN s.{col} ELSE 0 END) AS 分子,SUM(s.outstanding_principal) AS 分母,SUM(CASE WHEN s.dpd>=1 THEN s.{col} ELSE 0 END)/NULLIF(SUM(s.outstanding_principal),0) AS {label} {snap} {org} WHERE {at} GROUP BY o.org_name",scales={label:6})
    add(23, f"SELECT p.product_name AS 贷款产品,COUNT(DISTINCT CASE WHEN s.dpd>=30 AND s.outstanding_principal>0 THEN s.loan_id END) AS 逾期贷款笔数 {snap} {product} WHERE {at} GROUP BY p.product_name")
    add(24, f"SELECT o.org_name AS 机构,ROUND(AVG(DATE_DIFF('day',a.applied_on,e.decided_on)),2) AS 平均审批天数 {approvals} WHERE {jul('e.decided_on')} AND {final} GROUP BY o.org_name")
    add(25, f"SELECT p.product_name AS 贷款产品,COUNT(l.loan_id) AS 借据笔数 FROM loan_products p LEFT JOIN loans l ON p.product_id=l.product_id AND {jul('l.originated_on')} GROUP BY p.product_name")
    add(26, f"SELECT c.channel_name AS 渠道,SUM(d.amount) AS 净放款金额 {disb_join} {org} JOIN channels c ON l.channel_id=c.channel_id WHERE {jul('d.disbursed_on')} AND {valid('d.status')} AND o.region='华东' GROUP BY c.channel_name")
    add(27, f"WITH f AS (SELECT l.product_id,SUM(d.amount) AS amount {disb_join} WHERE {jul('d.disbursed_on')} AND {valid('d.status')} GROUP BY l.product_id), r AS (SELECT l.product_id,SUM(r.principal_paid) AS amount FROM repayment_transactions r JOIN loans l ON r.loan_id=l.loan_id WHERE {jul('r.paid_on')} AND {valid('r.status')} GROUP BY l.product_id) SELECT p.product_name AS 贷款产品,COALESCE(f.amount,0) AS 净放款本金,COALESCE(r.amount,0) AS 净实还本金,COALESCE(f.amount,0)-COALESCE(r.amount,0) AS 差额 FROM loan_products p LEFT JOIN f ON p.product_id=f.product_id LEFT JOIN r ON p.product_id=r.product_id")
    add(28, "SELECT customer_id AS 客户编号,COUNT(DISTINCT loan_id) AS 借据笔数,SUM(principal) AS 累计初始本金 FROM loans WHERE originated_on<=DATE '2026-07-31' GROUP BY customer_id HAVING COUNT(DISTINCT loan_id)>=2 ORDER BY 借据笔数 DESC,客户编号 LIMIT 5",True)
    add(29, "WITH state AS ("+historical_state_sql('2026-07-15')+") SELECT o.org_name AS 机构,SUM(s.outstanding_principal) AS 本金余额,SUM(s.overdue_principal) AS 逾期应还本金,MAX(s.dpd) AS 最大DPD FROM state s JOIN loans l ON s.loan_id=l.loan_id JOIN org_units o ON l.org_id=o.org_id GROUP BY o.org_name",note='must not query loan_daily_snapshots')
    months = ','.join(f"('2026-{i:02}')" for i in range(1,9))
    add(30, f"WITH months AS (SELECT m AS month FROM (VALUES {months}) AS v(m)), counts AS (SELECT STRFTIME(applied_on,'%Y-%m') AS month,COUNT(DISTINCT application_id) AS n FROM applications WHERE applied_on BETWEEN DATE '2026-01-01' AND DATE '2026-08-31' GROUP BY STRFTIME(applied_on,'%Y-%m')) SELECT m.month AS 月份,COALESCE(c.n,0) AS 申请笔数,SUM(COALESCE(c.n,0)) OVER(ORDER BY m.month) AS 累计申请笔数 FROM months m LEFT JOIN counts c ON m.month=c.month ORDER BY m.month",True)
    add(31, f"WITH x AS (SELECT c.channel_name AS 渠道,COALESCE(SUM(CASE WHEN d.disbursed_on BETWEEN DATE '2026-08-01' AND DATE '2026-08-31' THEN d.amount ELSE 0 END),0) AS 八月金额,COALESCE(SUM(CASE WHEN d.disbursed_on BETWEEN DATE '2026-09-01' AND DATE '2026-09-30' THEN d.amount ELSE 0 END),0) AS 九月金额 FROM channels c LEFT JOIN loans l ON c.channel_id=l.channel_id LEFT JOIN disbursements d ON l.loan_id=d.loan_id AND {valid('d.status')} GROUP BY c.channel_name) SELECT 渠道,八月金额,九月金额,九月金额-八月金额 AS 差额,(九月金额-八月金额)/NULLIF(八月金额,0) AS 环比 FROM x",scales={'环比':6})
    add(32, f"WITH x AS (SELECT o.org_name AS 机构,SUM(CASE WHEN e.decided_on<DATE '2026-09-01' AND e.decision='approved' THEN 1 ELSE 0 END)*1.0/NULLIF(SUM(CASE WHEN e.decided_on<DATE '2026-09-01' THEN 1 ELSE 0 END),0) AS 八月通过率,SUM(CASE WHEN e.decided_on>=DATE '2026-09-01' AND e.decision='approved' THEN 1 ELSE 0 END)*1.0/NULLIF(SUM(CASE WHEN e.decided_on>=DATE '2026-09-01' THEN 1 ELSE 0 END),0) AS 九月通过率 {approvals} WHERE e.decided_on BETWEEN DATE '2026-08-01' AND DATE '2026-09-30' AND {final} GROUP BY o.org_name) SELECT 机构,八月通过率,九月通过率,(九月通过率-八月通过率)*100 AS 变化百分点,(九月通过率-八月通过率)/NULLIF(八月通过率,0) AS 相对变化率 FROM x",scales={'八月通过率':6,'九月通过率':6,'变化百分点':6,'相对变化率':6})
    add(33, f"SELECT COALESCE(o.region,'全部机构') AS 地区,SUM(CASE WHEN s.dpd>=30 THEN s.outstanding_principal ELSE 0 END)/NULLIF(SUM(s.outstanding_principal),0) AS 逾期贷款余额占比 {snap} {org} WHERE s.snapshot_on=DATE '2026-09-30' GROUP BY GROUPING SETS((o.region),())",scales={'逾期贷款余额占比':6})
    add(34, "SELECT STRFTIME(snapshot_on,'%Y-%m') AS 月份,SUM(outstanding_principal) AS 贷款本金余额 FROM loan_daily_snapshots WHERE snapshot_on BETWEEN DATE '2026-01-01' AND DATE '2026-09-30' AND snapshot_on=LAST_DAY(snapshot_on) GROUP BY STRFTIME(snapshot_on,'%Y-%m') ORDER BY 月份",True)
    add(35, "WITH buckets AS (SELECT b AS band FROM (VALUES ('0'),('1～29'),('30～59'),('60及以上')) AS v(b)), x AS (SELECT CASE WHEN dpd=0 THEN '0' WHEN dpd<30 THEN '1～29' WHEN dpd<60 THEN '30～59' ELSE '60及以上' END AS band,loan_id,outstanding_principal FROM loan_daily_snapshots WHERE snapshot_on=DATE '2026-09-30' AND outstanding_principal>0) SELECT b.band AS DPD分组,COUNT(x.loan_id) AS 借据笔数,COALESCE(SUM(x.outstanding_principal),0) AS 本金余额 FROM buckets b LEFT JOIN x ON b.band=x.band GROUP BY b.band")
    add(36, f"WITH f AS (SELECT l.customer_id,SUM(d.amount) AS amount {disb_join} WHERE d.disbursed_on<=DATE '2026-08-31' AND {valid('d.status')} GROUP BY l.customer_id), l AS (SELECT customer_id,COUNT(DISTINCT loan_id) AS n FROM loans WHERE originated_on<=DATE '2026-08-31' GROUP BY customer_id) SELECT l.customer_id AS 客户编号,l.n AS 借据笔数,f.amount AS 净放款金额 FROM l JOIN f ON l.customer_id=f.customer_id WHERE l.n>=2 AND f.amount>100000 ORDER BY f.amount DESC,l.customer_id LIMIT 10",True)
    add(37, f"WITH x AS (SELECT o.org_name AS 机构,SUM(r.principal_paid+r.interest_paid) AS 本息合计 FROM repayment_transactions r JOIN loans l ON r.loan_id=l.loan_id {org} WHERE r.paid_on BETWEEN DATE '2026-09-01' AND DATE '2026-09-30' AND {valid('r.status')} GROUP BY o.org_name) SELECT 机构,本息合计,本息合计/NULLIF(SUM(本息合计) OVER(),0) AS 占比 FROM x",scales={'占比':6})
    add(38, f"SELECT l.loan_id AS 借据编号,l.principal AS 初始借据本金,s.outstanding_principal AS 当前本金余额,l.principal-s.outstanding_principal AS 差额 {snap} WHERE s.snapshot_on=DATE '2026-09-30' ORDER BY 差额 DESC,l.loan_id LIMIT 10",True)
    add(39, f"WITH {first} SELECT p.product_name AS 首次放款产品,SUM(s.outstanding_principal) AS 本金余额 FROM firsts f JOIN loans l ON f.customer_id=l.customer_id JOIN loan_daily_snapshots s ON l.loan_id=s.loan_id JOIN loan_products p ON f.product_id=p.product_id WHERE f.rn=1 AND f.disbursed_on BETWEEN DATE '2026-08-01' AND DATE '2026-08-31' AND s.snapshot_on=DATE '2026-09-30' GROUP BY p.product_name")
    add(40, f"SELECT c.channel_name AS 渠道,COUNT(DISTINCT CASE WHEN e.decision='approved' THEN e.application_id END) AS 通过数,COUNT(DISTINCT e.application_id) AS 最终审批总数,COUNT(DISTINCT CASE WHEN e.decision='approved' THEN e.application_id END)*1.0/NULLIF(COUNT(DISTINCT e.application_id),0) AS 审批通过率 FROM applications a JOIN channels c ON a.channel_id=c.channel_id JOIN approval_events e ON a.application_id=e.application_id WHERE {jul('a.applied_on')} AND e.decided_on<=DATE '2026-09-30' AND {final} GROUP BY c.channel_name",scales={'审批通过率':6})
    return specs
