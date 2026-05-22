"""Local counterexample for saved Q017 SQL; no new model generation."""
import argparse
import csv
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_live import ROOT,save,compare
from app.config import BASE_DIR
from app.querying.duckdb_engine import DuckDbEngine
from app.security import AccessController


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True);parser.add_argument('--case-id',choices=['CREDIT-Q-017','CREDIT-Q-039'],default='CREDIT-Q-017');args=parser.parse_args()
    run=ROOT/args.run;case=next(c for c in json.loads((ROOT/'cases.json').read_text()) if c['id']==args.case_id)
    record=json.loads((run/f'cases/{args.case_id}.json').read_text())
    output='first_borrower_counterexample.json' if args.case_id.endswith('017') else 'cohort_counterexample.json'
    sql=record.get('result',{}).get('sql')
    if not sql:
        save(run/output,{'tested':False,'reason':'no_generated_sql'});return
    source=BASE_DIR/'data/databases/credit_ops'
    with tempfile.TemporaryDirectory(prefix='credit-first-borrower-') as tmp:
        db=Path(tmp)/'credit_ops';db.mkdir()
        for p in source.iterdir():
            if p.is_file():(db/p.name).symlink_to(p)
        def read(name):
            with (source/(name+'.csv')).open() as f:return list(csv.DictReader(f))
        def write(name,rows):
            p=db/(name+'.csv');p.unlink()
            with p.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        base=read('loans')[0]
        loans=[{**base,'loan_id':f'L_FIX_{i}','customer_id':customer,'channel_id':channel,'originated_on':date,'principal':'100.00'}
               for i,customer,channel,date in [(1,'C00001','CH1','2026-06-01'),(2,'C00001','CH2','2026-07-01'),(3,'C00002','CH3','2026-07-02')]]
        if args.case_id.endswith('039'):
            for i,loan in enumerate(loans):
                loan['originated_on']=['2026-07-01','2026-08-01','2026-08-02'][i]
                loan['product_id']=f'P{i+1}'
        write('loans',loans)
        events=[]
        for loan,values in zip(loans,[(100,-100),(100,),(100,-100)]):
            for amount in values:
                events.append({'disbursement_id':f'D_FIX_{len(events)+1}','loan_id':loan['loan_id'],'disbursed_on':loan['originated_on'],'amount':str(amount),'status':'success' if amount>0 else 'reversal'})
        write('disbursements',events)
        if args.case_id.endswith('039'):
            template=read('loan_daily_snapshots')[0]
            write('loan_daily_snapshots',[{**template,'loan_id':loan['loan_id'],'snapshot_on':'2026-09-30','outstanding_principal':'70.00' if i==1 else '0.00','overdue_principal':'0.00','dpd':'0'} for i,loan in enumerate(loans)])
        engine=DuckDbEngine(Path(tmp));scope=AccessController().resolve('credit_analyst')
        gold=engine.execute('credit_ops',case['gold_sql'],scope);actual=engine.execute('credit_ops',sql,scope)
        assert gold.success,gold.error
        if args.case_id.endswith('017'):assert gold.rows==[{'渠道':'线下网点','首次放款客户数':1}],gold.rows
        else:assert len(gold.rows)==1 and str(gold.rows[0]['本金余额'])=='70.00',gold.rows
        score=compare({**case,'expected_rows':gold.rows,'expected_columns':gold.columns},actual.rows) if actual.success else {'values_match':False,'reason':actual.error}
        fixture='Customer A: prior-month first loan +100/-100 same-day reversal, target-month second loan +100; customer B: target-month +100/-100 only. Only A is a first net-success borrower in target month. Q017 targets July and CH2; Q039 targets August and P2, with September 30 balance 70.00. Local metric counterexample, not a full accounting-ledger validation. Originals unchanged; no model calls.'
        result={'tested':True,'case_id':case['id'],'fixture':fixture,'expected_rows':gold.rows,'actual_rows':actual.rows,'comparison':score}
        save(run/output,result)
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
