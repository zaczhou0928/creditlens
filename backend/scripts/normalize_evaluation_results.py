"""Supplemental, explicitly post-run dimension-display equivalence review.

Never changes saved raw grades. The same transformations apply to both arms;
monetary/ratio precision, row cardinality, ordering, and SQL remain unchanged.
"""
import argparse
import csv
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import BASE_DIR
from scripts.evaluate_live import ROOT,save,compare,sha


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);args=p.parse_args()
    run=ROOT/args.run;catalog={};hashes={}
    for table,key,value in [('org_units','org_id','org_name'),('channels','channel_id','channel_name'),('loan_products','product_id','product_name')]:
        path=BASE_DIR/'data/databases/credit_ops'/f'{table}.csv'
        hashes[table]=sha(path)
        with path.open() as f:catalog.update({r[key]:r[value] for r in csv.DictReader(f)})
    intervals={'DPD=0':'0','0天':'0','1~29':'1～29','1-29':'1～29','30~59':'30～59','30-59':'30～59','60+':'60及以上','>=60':'60及以上','≥60':'60及以上'}
    cases={c['id']:c for c in json.loads((ROOT/'cases.json').read_text())};records=[]
    for path in sorted((run/'cases').glob('*.json')):
        raw=json.loads(path.read_text());c=cases[raw['id']];result=raw.get('result',{});changes=[];rows=[]
        for row in result.get('rows',[]):
            normalized={}
            for key,value in row.items():
                changed=catalog.get(value,value) if isinstance(value,str) else value
                if 'DPD分组' in c['expected_columns'] and isinstance(changed,str):
                    label=changed.removeprefix('DPD').removeprefix('=')
                    changed=intervals.get(label,label)
                if c['id']=='CREDIT-Q-033' and changed=='ALL_ORG':changed='全部机构'
                if changed!=value:changes.append({'column':key,'from':value,'to':changed})
                normalized[key]=changed
            rows.append(normalized)
        score=compare(c,rows) if result.get('sql') else raw.get('comparison',{'values_match':False})
        if c['id']=='CREDIT-Q-029' and 'loan_daily_snapshots' in (result.get('sql') or '').lower():score.update(values_match=False,reason='forbidden_snapshot_path')
        records.append({'id':c['id'],'raw_values_match':raw['comparison']['values_match'],'normalized_comparison':score,'changes':changes,'normalized_rows':rows})
    out={'note':'Supplemental post-run display equivalence only; frozen raw comparison is retained separately. All records in both arms use the same catalog and DPD interval rules. Column meanings still require SQL review. No query regeneration, score feedback, numeric rounding relaxation, or dropped failures.','catalog_sha256':hashes,'attempted':len(records),'raw_matched':sum(r['raw_values_match'] for r in records),'normalized_matched':sum(r['normalized_comparison']['values_match'] for r in records),'changed_outcomes':[r['id'] for r in records if r['raw_values_match']!=r['normalized_comparison']['values_match']],'cases':records}
    save(run/'display_equivalence.json',out)
    print(json.dumps({k:out[k] for k in ('attempted','raw_matched','normalized_matched','changed_outcomes')},ensure_ascii=False))


if __name__=='__main__':main()
