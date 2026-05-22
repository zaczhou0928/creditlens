"""Derive auditable metrics from persisted runs, without calling models."""
from collections import Counter
import csv
from decimal import Decimal
import json
import math
import re
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from app.credit.schema import SCHEMA
from scripts.evaluate_live import ROOT,save,summarize


FIELDS={t['name']:{f['name'] for f in t['fields']} for t in SCHEMA}


def dependencies(sql):
    result=set()
    for scope in traverse_scope(sqlglot.parse_one(sql,read='duckdb')):
        tables={alias:source.name for alias,source in scope.sources.items() if isinstance(source,exp.Table) and source.name in FIELDS}
        for col in scope.columns:
            if col.table and col.table in tables and col.name in FIELDS[tables[col.table]]:
                result.add('credit_ops.'+tables[col.table]+'.'+col.name)
            elif not col.table:
                matches={table for table in tables.values() if col.name in FIELDS[table]}
                if len(matches)==1:result.add('credit_ops.'+matches.pop()+'.'+col.name)
    return result


def wilson(x,n):
    if not n:return None
    z=1.959963984540054;p=x/n;den=1+z*z/n
    mid=(p+z*z/(2*n))/den;half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [mid-half,mid+half]


def analyze(run_name):
    run=ROOT/run_name
    if not (run/'cases').exists():return None
    cases={c['id']:c for c in json.loads((ROOT/'cases.json').read_text())}
    records=[json.loads(p.read_text()) for p in sorted((run/'cases').glob('*.json'))]
    if not records:return None
    review_path=run/'query_review.json'
    review_document=json.loads(review_path.read_text()) if review_path.exists() else {}
    reviews=review_document.get('cases',{})
    raw_sql_count=sum(bool(r.get('sql_success')) for r in records)
    for r in records:
        if reviews.get(r['id'],{}).get('final_sql_is_probe'):
            r['sql_success']=False
    summary=summarize(records,40)
    summary['raw_returned_sql_executed']=raw_sql_count
    summary['final_probe_exclusions']=[id for id,v in reviews.items() if v.get('final_sql_is_probe')]
    summary['review_status']=review_document.get('status','pending')
    summary['reviewed_counts']=review_document.get('counts')
    summary['difficulty']={}
    for level in ('简单','中等','复杂'):
        subset=[r for r in records if r.get('difficulty')==level]
        summary['difficulty'][level]={'n':len(subset),'executed':sum(bool(r.get('sql_success')) for r in subset),'matched':sum(bool(r.get('comparison',{}).get('values_match')) for r in subset)}
    metrics=[]
    for r in records:
        c=cases[r['id']]
        gold=dependencies(c['gold_sql'])
        # Snapshot/event queries have alternate legal paths. Exclude them from
        # aggregate retrieval claims until every alternative has a full label.
        eligible='loan_daily_snapshots' not in c['gold_sql']
        first=set(r.get('retrieval_calls',[{}])[0].get('fields',[])) if r.get('retrieval_calls') else set()
        final=set(first)
        for call in r.get('retrieval_calls',[]):final.update(call['fields'])
        final.update(f['id'] for f in (r.get('result',{}).get('schema_graph') or {}).get('fields',[]))
        for call in r.get('result',{}).get('tool_calls',[]):
            result=call.get('result') or {}
            final.update(f['id'] for f in result.get('schema_graph',{}).get('fields',[]))
        metrics.append({'id':r['id'],'eligible':eligible,'gold_fields':sorted(gold),'initial_fields':sorted(first),'final_schema_fields':sorted(final),'initial_recall':len(first&gold)/len(gold) if gold else None,'initial_precision':len(first&gold)/len(first) if first else 0,'initial_complete':bool(gold and gold<=first),'final_recall':len(final&gold)/len(gold) if gold else None,'final_complete':bool(gold and gold<=final)})
    subset=[m for m in metrics if m['eligible'] and m['gold_fields']]
    summary['retrieval_diagnostic']={'n':len(subset),'note':'Gold-SQL dependency diagnostic, excluding snapshot/event alternative queries; initial and graph/MCP-completed Schema only, knowledge SQL fields excluded. Derived annotation, not a separately human-labeled retrieval benchmark.',**{key:sum(float(m[key]) for m in subset)/len(subset) if subset else None for key in ('initial_recall','initial_precision','initial_complete','final_recall','final_complete')}}
    requests=[q for r in records for q in r.get('model_requests',[])]
    summary['requests_by_service']=dict(Counter(q['service'] for q in requests))
    summary['chat_reasoning_tokens']=sum((q.get('usage') or {}).get('completion_tokens_details',{}).get('reasoning_tokens',0) for q in requests if q['service']=='chat')
    summary['missing_usage_requests']=sum(q.get('usage') is None for q in requests)
    summary['model_errors']=sum(not q.get('ok') for q in requests)
    success_times=[r['seconds'] for r in records if r.get('sql_success')]
    summary['sql_success_mean_e2e_seconds']=sum(success_times)/len(success_times) if success_times else None
    execution_times=[r['sql_calls'][-1]['seconds'] for r in records if r.get('sql_success') and r.get('sql_calls')]
    summary['final_success_sql_mean_seconds']=sum(execution_times)/len(execution_times) if execution_times else None
    summary['final_success_sql_p95_seconds']=sorted(execution_times)[math.ceil(len(execution_times)*.95)-1] if execution_times else None
    summary['failures']=[{'id':r['id'],'reason':r.get('comparison',{}).get('reason'),'analysis':r.get('result',{}).get('analysis'),'error':r.get('error')} for r in records if not r.get('comparison',{}).get('values_match')]
    # Purpose is retained in raw model responses, even though product tool logs
    # do not persist it. Explicit reviewer exclusions handle mislabeled probes.
    first=[]
    from scripts.evaluate_live import compare
    def norm(sql):return re.sub(r'\s+',' ',sql.strip().rstrip(';')).strip()
    for r in records:
        decisions=[]
        for request in r.get('model_requests',[]):
            for choice in request.get('response',[]):
                content=choice.get('message',{}).get('content') or ''
                try:d=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',content.strip()))
                except (ValueError,TypeError):continue
                if d.get('action')=='call_tool' and d.get('tool_name')=='query_credit_ops':
                    decisions.append(d)
        excluded=set(reviews.get(r['id'],{}).get('inspection_sql_call_indices',[]))
        candidates=[]
        for i,call in enumerate(r.get('sql_calls',[])):
            decision=next((d for d in decisions if norm((d.get('arguments') or {}).get('sql',''))==norm(call.get('sql',''))),{})
            if i not in excluded and decision.get('purpose')!='inspect':candidates.append((i,call))
        if candidates:
            i,call=candidates[0]
            score=compare(cases[r['id']],call.get('rows',[])) if call.get('success') else {'values_match':False,'reason':call.get('error')}
            first.append({'id':r['id'],'sql_call_index':i,'sql':call['sql'],'success':call.get('success'), 'comparison':score,'final_values_match':r.get('comparison',{}).get('values_match')})
        else:first.append({'id':r['id'],'success':False,'comparison':{'values_match':False,'reason':'no_target_sql'},'final_values_match':False})
    failed=[r for r in first if r.get('sql') and not r['success']]
    summary['first_target_sql']={'executed':sum(bool(r['success']) for r in first),'matched':sum(bool(r['comparison']['values_match']) for r in first),'total':len(first),'execution_failure_tasks':len(failed),'repaired_to_correct_result':sum(bool(r['final_values_match']) for r in failed),'review_status':'reviewed_exclusions' if reviews else 'pending_probe_review'}
    save(run/'first_sql_diagnostics.json',first)
    save(run/'metrics.json',summary);save(run/'schema_diagnostics.json',metrics)
    with (run/'case_results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['id','difficulty','question','sql_success','value_match','review_required','seconds','reason','sql','analysis'])
        w.writeheader()
        for r in records:
            s=r.get('result',{});c=r.get('comparison',{})
            w.writerow({'id':r['id'],'difficulty':r.get('difficulty'),'question':r.get('question'),'sql_success':r.get('sql_success'),'value_match':c.get('values_match'),'review_required':c.get('review_required'),'seconds':r.get('seconds'),'reason':c.get('reason'),'sql':s.get('sql'),'analysis':s.get('analysis')})
    print(run_name,json.dumps({k:summary[k] for k in ('attempted','sql_success','provisional_value_accuracy','review_status','reviewed_counts','first_target_sql','model_request_count','chat_input_tokens','chat_output_tokens')},ensure_ascii=False))
    return summary


if __name__=='__main__':
    for name in ('rag_20261007','full_schema_20261007'):analyze(name)
