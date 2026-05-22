"""Resumable real-model evaluation. Gold data is used only after Agent execution."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
from itertools import chain, permutations
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import settings, BASE_DIR
from app.model_client import ModelClient
from app.retrieval import SchemaIndex
from app.security import AccessController, AccessScope
from app.services.askdata_service import AskDataService

ROOT=BASE_DIR/'evaluation/credit_v1'


def save(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def application_hash():
    h=hashlib.sha256()
    for p in sorted((BASE_DIR/'app').rglob('*')):
        if p.is_file() and p.suffix in ('.py','.md','.json'):
            h.update(str(p.relative_to(BASE_DIR)).encode());h.update(p.read_bytes())
    return h.hexdigest()


ALIASES={
 '机构': {'机构名称','org_name','org','organization'},
 '渠道': {'渠道名称','channel_name','channel'},
 '贷款产品': {'产品','产品名称','product_name','product'},
 '客户群体': {'客群','customer_segment','segment'},
 '客户编号': {'customer_id'}, '借据编号': {'loan_id'},
 '月份': {'month','统计月份','month_str'}, '申请日期': {'applied_on','日期','date'},
 '申请状态': {'application_status','status','状态'},
 '申请笔数': {'application_count','申请数','申请数量','count'},
 '申请客户数': {'applicant_count','申请人数','客户数'},
 '借据笔数': {'loan_count','借据数','贷款笔数'},
 '净放款金额': {'放款金额','net_disbursement','net_disbursement_amount'},
 '本金余额': {'outstanding_principal','贷款本金余额','贷款余额'},
 '贷款本金余额': {'outstanding_principal','本金余额','贷款余额'},
 '实还本金': {'净实还本金','principal_paid'},
 '实还利息': {'净实还利息','interest_paid'},
 '审批通过率': {'approval_rate','通过率'},
 '平均审批天数': {'avg_approval_days','平均审批耗时'},
 '首次放款产品': {'首贷产品','product_name','贷款产品'},
 'DPD分组': {'逾期分组','dpd_bucket','DPD区间','逾期区间'},
}


def normalize(value, expected, column, scale):
    if value is None: return None
    if expected is None: return value
    if isinstance(expected,(int,float,Decimal)) or (isinstance(expected,str) and re.fullmatch(r'-?\d+(?:\.\d+)?',expected) and column not in ('客户编号','借据编号','DPD分组')):
        try:
            text=str(value).strip().replace(',','')
            number=Decimal(text[:-1])/100 if text.endswith('%') else Decimal(text)
            if not number.is_finite(): return ('invalid',str(value))
            if isinstance(expected,int) and not isinstance(expected,bool):
                return number if number==number.to_integral() else ('non_integer',str(value))
            return number.quantize(Decimal(1).scaleb(-scale),rounding=ROUND_HALF_UP)
        except (InvalidOperation,ValueError): return ('non_numeric',str(value))
    text=str(value)
    if column=='月份' and re.fullmatch(r'\d{4}-\d{2}(?:-01(?:[ T]00:00:00)?)?',text): return text[:7]
    if column=='申请日期': return text[:10] if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T]00:00:00)?',text) else text
    return text


def compare(case, actual):
    expected=case['expected_rows']; gold_cols=case['expected_columns']
    if len(actual)!=len(expected): return dict(values_match=False,reason=f'row_count:{len(actual)}!={len(expected)}')
    if not expected: return dict(values_match=True,review_required=True,reason='empty_result_needs_schema_review')
    actual_cols=list(actual[0]); scales=case.get('scales',{})
    if len(actual_cols)<len(gold_cols): return dict(values_match=False,reason='missing_columns')
    if len(actual_cols)>10: return dict(values_match=False,reason='too_many_columns_for_automatic_mapping',review_required=True)
    references=[next((r[c] for r in expected if r[c] is not None),None) for c in gold_cols]
    def encode(rows,mapping):
        return [tuple(normalize(row.get(a),ref,c,scales.get(c,2)) for c,a,ref in zip(gold_cols,mapping,references)) for row in rows]
    e=encode(expected,gold_cols)
    choices=[]
    for c in gold_cols:
        candidates=[a for a in actual_cols if a==c or a in ALIASES.get(c,set())]
        choices.append(candidates[0] if len(candidates)==1 else None)
    candidates=[]
    if all(choices) and len(set(choices))==len(choices): candidates.append(tuple(choices))
    candidates=chain(candidates,permutations(actual_cols,len(gold_cols)))
    seen=set()
    for mapping in candidates:
        if mapping in seen: continue
        seen.add(mapping)
        a=encode(actual,mapping)
        same=a==e if case.get('ordered') else Counter(a)==Counter(e)
        if same:
            semantic=all(a==c or a in ALIASES.get(c,set()) for c,a in zip(gold_cols,mapping))
            return dict(values_match=True,column_mapping=dict(zip(gold_cols,mapping)),review_required=not semantic or len(actual_cols)!=len(gold_cols),reason='equivalent_values_semantics_pending' if not semantic else 'matched')
    return dict(values_match=False,reason='value_or_order_mismatch',actual_columns=actual_cols)


class MeteredModel(ModelClient):
    def __init__(self,cfg,trace_path):
        super().__init__(cfg);self.requests=[];self.trace_path=trace_path
    def _post(self,url,payload):
        t=time.perf_counter(); event={'service':'chat' if 'messages' in payload else 'rerank' if 'documents' in payload else 'embedding','model':payload.get('model'),'started_at':datetime.now(timezone.utc).isoformat()}
        try:
            result=super()._post(url,payload)
            event.update(ok=True,usage=result.get('usage'),response_model=result.get('model'))
            if 'choices' in result: event['response']=result['choices']
            return result
        except Exception as exc:
            event.update(ok=False,error=str(exc)[:1000]);raise
        finally:
            event['seconds']=round(time.perf_counter()-t,4)
            self.requests.append(event);save(self.trace_path,self.requests)


class FullSchemaIndex(SchemaIndex):
    def retrieve(self,query,*,retrieval_terms=None,threshold=None,access_scope=None):
        scope=access_scope if isinstance(access_scope,AccessScope) else AccessScope.from_dict(access_scope)
        # No model retrieval calls; identical authorized metadata document content.
        hits=[]
        for d in self._build_raw_documents():
            if scope.allows_field(d['table_id'],d['field_name']):
                hits.append({**d,'score':1.0,'source':'full_authorized_schema'})
        return {'query':query,'retrieval_terms':retrieval_terms or [],'hits':hits,'selected_count':len(hits),'embedding_source':'not_used_full_schema','rerank_source':'not_used_full_schema'}


def config_for(run,case_id):
    return replace(settings,credit_demo_mode=False,external_model_enabled=True,credit_reference_date='2026-10-01',session_archive_path=str(run/'sessions'/f'{case_id}.db'))


def run_case(case,run,variant):
    cfg=config_for(run,case['id']);model=MeteredModel(cfg,run/'traces'/f"{case['id']}.json")
    index=(FullSchemaIndex if variant=='full_schema' else SchemaIndex)(model,cfg,index_path=ROOT/'schema_cache.json')
    service=AskDataService(model,index,cfg)
    sql_calls=[];retrieval_calls=[]
    original_sql=service.workflow.database_engine.execute
    def measured_sql(*args,**kw):
        t=time.perf_counter();r=original_sql(*args,**kw)
        sql_calls.append({'seconds':round(time.perf_counter()-t,4),**asdict(r)})
        return r
    service.workflow.database_engine.execute=measured_sql
    original_retrieve=index.retrieve
    def measured_retrieve(*args,**kw):
        t=time.perf_counter();r=original_retrieve(*args,**kw)
        retrieval_calls.append({'seconds':round(time.perf_counter()-t,4),'fields':[h['doc_id'] for h in r.get('hits',[])]})
        return r
    index.retrieve=measured_retrieve
    t=time.perf_counter()
    result=service.submit(case['question'],case['id'],user_id=case['user'])
    seconds=round(time.perf_counter()-t,4)
    score=compare(case,result.rows) if result.sql else {'values_match':False,'reason':'no_final_sql'}
    if case['id']=='CREDIT-Q-029' and result.sql and 'loan_daily_snapshots' in result.sql.lower():
        score.update(values_match=False,reason='forbidden_snapshot_path')
    return dict(id=case['id'],difficulty=case['difficulty'],question=case['question'],user=case['user'],variant=variant,
                seconds=seconds,sql_success=bool(result.sql and result.validation.get('sql_executed')),
                comparison=score,model_requests=model.requests,sql_calls=sql_calls,retrieval_calls=retrieval_calls,
                result=result.model_dump(mode='json'))


def summarize(records,planned):
    n=len(records); sql=sum(bool(r.get('sql_success')) for r in records)
    correct=sum(bool(r.get('comparison',{}).get('values_match')) for r in records)
    times=sorted(r['seconds'] for r in records if 'seconds' in r)
    requests=[q for r in records for q in r.get('model_requests',[])]
    return dict(planned=planned,attempted=n,sql_success=sql,value_matches_pending_semantic_review=correct,
                sql_execution_rate=sql/n if n else None,provisional_value_accuracy=correct/n if n else None,
                mean_seconds=sum(times)/len(times) if times else None,p50_seconds=times[math.ceil(len(times)*.5)-1] if times else None,p95_seconds=times[math.ceil(len(times)*.95)-1] if times else None,
                model_request_count=len(requests),chat_input_tokens=sum((q.get('usage') or {}).get('prompt_tokens',0) for q in requests if q['service']=='chat'),
                chat_output_tokens=sum((q.get('usage') or {}).get('completion_tokens',0) for q in requests if q['service']=='chat'),
                analysis_available=sum(r.get('result',{}).get('validation',{}).get('model_analysis')=='grounded_references_checked' for r in records))


def main():
    p=argparse.ArgumentParser();p.add_argument('--allow-external-model',action='store_true');p.add_argument('--variant',choices=['rag','full_schema'],default='rag');p.add_argument('--run',required=True);p.add_argument('--case-id',action='append');p.add_argument('--worker');p.add_argument('--workers',type=int,default=2);p.add_argument('--case-timeout',type=int,default=360)
    args=p.parse_args()
    if not args.allow_external_model: raise SystemExit('Explicit --allow-external-model required')
    run=ROOT/args.run;run.mkdir(parents=True,exist_ok=True);(run/'sessions').mkdir(exist_ok=True)
    cases=json.loads((ROOT/'cases.json').read_text())
    assert all(c.get('gold_success') for c in cases), 'Invalid gold reference'
    if args.case_id: cases=[c for c in cases if c['id'] in args.case_id]
    if args.worker:
        case=next(c for c in cases if c['id']==args.worker)
        try: row=run_case(case,run,args.variant)
        except Exception as exc: row={'id':case['id'],'difficulty':case['difficulty'],'question':case['question'],'error':str(exc),'sql_success':False,'comparison':{'values_match':False,'reason':'runner_exception'}}
        save(run/'cases'/f"{case['id']}.json",row);return
    cfg=config_for(run,'initialization')
    meta={'created_at':datetime.now(timezone.utc).isoformat(),'mode':'live_external_model','variant':args.variant,'case_ids':[c['id'] for c in cases],'case_sha256':sha(ROOT/'cases.json'),'application_sha256':application_hash(),'runner_sha256':sha(__file__),'config':cfg.public_status(),'hosts':sorted({urlsplit(u).netloc for u in (cfg.chat_url,cfg.embeddings_url,cfg.rerank_url)}),'temperature':cfg.temperature,'timeout':cfg.timeout,'max_retries':cfg.max_retries,'workers':args.workers,'case_timeout':args.case_timeout,'data_version':json.loads((BASE_DIR/'data/databases/credit_ops/_database_manifest.json').read_text())['data_version'],'scope_note':'Authored project evaluation; not unseen holdout; no gold answers sent to model.'}
    if (run/'manifest.json').exists():
        old=json.loads((run/'manifest.json').read_text())
        for key in meta:
            if key!='created_at' and old[key]!=meta[key]:raise SystemExit(f'Incompatible resume: {key}')
    else:save(run/'manifest.json',meta)
    if args.variant=='rag':
        init=MeteredModel(cfg,run/'initialization_requests.json')
        SchemaIndex(init,cfg,index_path=ROOT/'schema_cache.json').ensure_built()
    pending=[c for c in cases if not (run/'cases'/f"{c['id']}.json").exists()]
    def worker(case):
        cmd=[sys.executable,__file__,'--allow-external-model','--variant',args.variant,'--run',args.run,'--worker',case['id']]
        with (run/f"{case['id']}.log").open('w') as log:
            try:subprocess.run(cmd,stdout=log,stderr=log,timeout=args.case_timeout,check=True)
            except (subprocess.TimeoutExpired,subprocess.CalledProcessError) as exc:
                traces=run/'traces'/f"{case['id']}.json"
                save(run/'cases'/f"{case['id']}.json",{'id':case['id'],'difficulty':case['difficulty'],'question':case['question'],'sql_success':False,'seconds':args.case_timeout,'error':type(exc).__name__,'model_requests':json.loads(traces.read_text()) if traces.exists() else [],'comparison':{'values_match':False,'reason':'process_timeout_or_failure'}})
        return json.loads((run/'cases'/f"{case['id']}.json").read_text())
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(worker,c) for c in pending]
        for future in as_completed(futures):
            r=future.result();print(json.dumps({'id':r['id'],'sql':r['sql_success'],'comparison':r['comparison'],'seconds':r.get('seconds')},ensure_ascii=False),flush=True)
            records=[json.loads((run/'cases'/f"{c['id']}.json").read_text()) for c in cases if (run/'cases'/f"{c['id']}.json").exists()]
            save(run/'summary.json',summarize(records,len(cases)))
    records=[json.loads((run/'cases'/f"{c['id']}.json").read_text()) for c in cases]
    save(run/'summary.json',summarize(records,len(cases)))
    print(json.dumps(summarize(records,len(cases)),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
