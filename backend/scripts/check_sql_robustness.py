"""Replay already-generated SQL on an isolated failed-transaction fixture.

No model calls. This is an additional diagnostic, not new independent NL cases.
"""
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
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True);args=parser.parse_args()
    run=ROOT/args.run;cases=json.loads((ROOT/'cases.json').read_text());records=[]
    source=BASE_DIR/'data/databases/credit_ops'
    with tempfile.TemporaryDirectory(prefix='credit-sql-counterfactual-') as tmp:
        db=Path(tmp)/'credit_ops';db.mkdir()
        for p in source.iterdir():
            if p.is_file(): (db/p.name).symlink_to(p)
        for name,identifier,date_col,amounts in [
            ('disbursements','disbursement_id','disbursed_on',{'amount':'900000.00'}),
            ('repayment_transactions','transaction_id','paid_on',{'principal_paid':'700000.00','interest_paid':'30000.00'})]:
            p=db/(name+'.csv');rows=list(csv.DictReader(p.open()));fields=list(rows[0])
            for month in ('07','08','09'):
                rows.append({**rows[0],identifier:f'FIXTURE_FAILED_{name}_{month}',date_col:f'2026-{month}-10','status':'failed',**amounts})
            p.unlink()
            with p.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
        engine=DuckDbEngine(Path(tmp));scope=AccessController().resolve('credit_analyst')
        for case in cases:
            path=run/'cases'/f"{case['id']}.json"
            if not path.exists():continue
            result=json.loads(path.read_text());sql=result.get('result',{}).get('sql')
            if not sql:continue
            gold=engine.execute('credit_ops',case['gold_sql'],scope)
            actual=engine.execute('credit_ops',sql,scope)
            assert gold.success,(case['id'],gold.error)
            contract={**case,'expected_rows':gold.rows,'expected_columns':gold.columns}
            score=compare(contract,actual.rows) if actual.success else {'values_match':False,'reason':actual.error}
            records.append({'id':case['id'],'main_values_match':result['comparison']['values_match'],'fixture_values_match':score['values_match'],'comparison':score,'expected_rows':gold.rows,'actual_rows':actual.rows})
    save(run/'sql_counterfactual.json',{'fixture':'Add three positive-amount failed disbursements and three failed repayments in July/August/September; originals untouched. Failed events must not change net metrics. No model regeneration.','executed_sql_count':len(records),'passed':sum(r['fixture_values_match'] for r in records),'results':records})
    print(json.dumps({'tested':len(records),'passed':sum(r['fixture_values_match'] for r in records),'newly_exposed':[r['id'] for r in records if r['main_values_match'] and not r['fixture_values_match']]},ensure_ascii=False))


if __name__=='__main__':main()
