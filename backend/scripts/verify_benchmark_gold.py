"""Independent CSV/Decimal calculations for all 40 frozen query gold results."""
import calendar
from collections import defaultdict
import csv
from datetime import date
from decimal import Decimal

from scripts.evaluate_live import ROOT, compare, save
from scripts.open_query_cases import historical_reference
from app.config import BASE_DIR
import json


def verify():
    data={p.stem:list(csv.DictReader(p.open())) for p in (BASE_DIR/'data/databases/credit_ops').glob('*.csv')}
    apps={r['application_id']:r for r in data['applications']}
    loans={r['loan_id']:r for r in data['loans']}
    products={r['product_id']:r['product_name'] for r in data['loan_products']}
    channels={r['channel_id']:r['channel_name'] for r in data['channels']}
    orgs={r['org_id']:r['org_name'] for r in data['org_units']}
    regions={r['org_id']:r['region'] for r in data['org_units']}
    customers={r['customer_id']:r for r in data['customers']}
    valid=lambda r:r['status'] in ('success','reversal')
    in_jul=lambda d:'2026-07-01'<=d<='2026-07-31'
    ap=[r for r in apps.values() if in_jul(r['applied_on'])]
    ls=[r for r in loans.values() if in_jul(r['originated_on'])]
    ds=[r for r in data['disbursements'] if valid(r)]
    rs=[r for r in data['repayment_transactions'] if valid(r)]
    es=[r for r in data['approval_events'] if r['is_final']=='1' and r['decision'] in ('approved','rejected')]
    js=[r for r in data['loan_daily_snapshots'] if r['snapshot_on']=='2026-07-31']
    ss=[r for r in data['loan_daily_snapshots'] if r['snapshot_on']=='2026-09-30']
    dec=lambda r,c:Decimal(r[c]); total=lambda rows,c:sum((dec(r,c) for r in rows),Decimal(0))
    mean=lambda rows,c:total(rows,c)/len(rows) if rows else None
    div=lambda a,b:a/b if b else None
    def groups(rows,key):
        result=defaultdict(list)
        for r in rows:result[key(r)].append(r)
        return result
    net=defaultdict(Decimal)
    for r in ds:
        l=loans[r['loan_id']];net[(l['customer_id'],r['disbursed_on'],r['loan_id'])]+=dec(r,'amount')
    first={}
    for (customer,day,loan),amount in sorted(net.items()):
        if amount>0 and customer not in first:first[customer]=(day,loan)
    values={}
    values[1]=[(len({r['application_id'] for r in ap}),)]
    values[2]=[(len({r['customer_id'] for r in ap}),)]
    values[3]=[(k,len(v)) for k,v in groups(ap,lambda r:r['application_status']).items()]
    amounts=[dec(r,'requested_amount') for r in ap]
    values[4]=[(min(amounts),max(amounts),sum(amounts)/len(amounts))]
    values[5]=[(len(ls),mean(ls,'term_months'))]
    values[6]=sorted((k,len(v)) for k,v in groups(ap,lambda r:r['applied_on']).items())
    values[7]=[(total([r for r in rs if in_jul(r['paid_on'])],'principal_paid'),)]
    values[8]=[(total([r for r in rs if in_jul(r['paid_on'])],'interest_paid'),)]
    values[9]=[(total([r for r in ds if in_jul(r['disbursed_on'])],'amount'),)]
    values[10]=[(k,len(v)) for k,v in groups([r for r in customers.values() if r['registered_on']<='2026-07-31'],lambda r:r['customer_segment']).items()]
    values[11]=[(channels[k],len(v)) for k,v in groups(ap,lambda r:r['channel_id']).items()]
    values[12]=[]
    for k,v in groups([r for r in es if in_jul(r['decided_on'])],lambda r:apps[r['application_id']]['org_id']).items():
        approved=sum(r['decision']=='approved' for r in v);values[12].append((orgs[k],approved,len(v),Decimal(approved)/len(v)))
    values[13]=[(products[k],total(v,'amount')) for k,v in groups([r for r in ds if '2026-07-01'<=r['disbursed_on']<='2026-09-30'],lambda r:loans[r['loan_id']]['product_id']).items()]
    values[14]=[(products[k],len(v),total(v,'principal'),max(int(r['term_months']) for r in v)) for k,v in groups(ls,lambda r:r['product_id']).items()]
    values[15]=[(k,mean(v,'requested_amount')) for k,v in groups(ap,lambda r:customers[r['customer_id']]['customer_segment']).items()]
    counts=defaultdict(int)
    for (channel,customer),v in groups([r for r in ds if in_jul(r['disbursed_on'])],lambda r:(loans[r['loan_id']]['channel_id'],loans[r['loan_id']]['customer_id'])).items():
        if total(v,'amount')>0:counts[channel]+=1
    values[16]=[(channels[k],v) for k,v in counts.items()]
    counts=defaultdict(int)
    for customer,(day,loan) in first.items():
        if in_jul(day):counts[loans[loan]['channel_id']]+=1
    values[17]=[(channels[k],v) for k,v in counts.items()]
    for n,k in [(18,1),(23,30)]:
        values[n]=[(products[p],sum(int(r['dpd'])>=k and dec(r,'outstanding_principal')>0 for r in v)) for p,v in groups(js,lambda r:loans[r['loan_id']]['product_id']).items()]
    values[19]=[(orgs[k],total(v,'outstanding_principal')) for k,v in groups(js,lambda r:loans[r['loan_id']]['org_id']).items()]
    values[20]=[(orgs[k],total([r for r in v if int(r['dpd'])>=1],'overdue_principal')) for k,v in groups(js,lambda r:loans[r['loan_id']]['org_id']).items()]
    for n,col in [(21,'outstanding_principal'),(22,'overdue_principal')]:
        values[n]=[]
        for k,v in groups(js,lambda r:loans[r['loan_id']]['org_id']).items():
            num=total([r for r in v if int(r['dpd'])>=1],col);den=total(v,'outstanding_principal');values[n].append((orgs[k],num,den,div(num,den)))
    values[24]=[]
    for k,v in groups([r for r in es if in_jul(r['decided_on'])],lambda r:apps[r['application_id']]['org_id']).items():
        days=sum((date.fromisoformat(r['decided_on'])-date.fromisoformat(apps[r['application_id']]['applied_on'])).days for r in v)
        values[24].append((orgs[k],Decimal(days)/len(v)))
    values[25]=[(name,sum(r['product_id']==p for r in ls)) for p,name in products.items()]
    values[26]=[(channels[k],total(v,'amount')) for k,v in groups([r for r in ds if in_jul(r['disbursed_on']) and regions[loans[r['loan_id']]['org_id']]=='华东'],lambda r:loans[r['loan_id']]['channel_id']).items()]
    values[27]=[]
    for p,name in products.items():
        f=total([r for r in ds if in_jul(r['disbursed_on']) and loans[r['loan_id']]['product_id']==p],'amount')
        paid=total([r for r in rs if in_jul(r['paid_on']) and loans[r['loan_id']]['product_id']==p],'principal_paid')
        values[27].append((name,f,paid,f-paid))
    by_customer=groups([r for r in loans.values() if r['originated_on']<='2026-07-31'],lambda r:r['customer_id'])
    values[28]=sorted([(k,len(v),total(v,'principal')) for k,v in by_customer.items() if len(v)>=2],key=lambda r:(-r[1],r[0]))[:5]
    hist=historical_reference(data,'2026-07-15',set(orgs))
    values[29]=[(orgs[k],total(v,'outstanding_principal'),total(v,'overdue_principal'),max(r['dpd'] for r in v)) for k,v in groups(hist,lambda r:loans[r['loan_id']]['org_id']).items()]
    values[30]=[];cumulative=0
    for m in range(1,9):
        month=f'2026-{m:02}';n=sum(r['applied_on'].startswith(month) for r in apps.values());cumulative+=n;values[30].append((month,n,cumulative))
    values[31]=[]
    for c,name in channels.items():
        a=total([r for r in ds if r['disbursed_on'].startswith('2026-08') and loans[r['loan_id']]['channel_id']==c],'amount')
        b=total([r for r in ds if r['disbursed_on'].startswith('2026-09') and loans[r['loan_id']]['channel_id']==c],'amount')
        values[31].append((name,a,b,b-a,div(b-a,a)))
    values[32]=[]
    for org,name in orgs.items():
        v=[r for r in es if apps[r['application_id']]['org_id']==org]
        a=[r for r in v if r['decided_on'].startswith('2026-08')];b=[r for r in v if r['decided_on'].startswith('2026-09')]
        ar=div(Decimal(sum(r['decision']=='approved' for r in a)),len(a));br=div(Decimal(sum(r['decision']=='approved' for r in b)),len(b))
        values[32].append((name,ar,br,(br-ar)*100 if ar is not None and br is not None else None,div(br-ar,ar) if ar is not None and br is not None else None))
    g=groups(ss,lambda r:regions[loans[r['loan_id']]['org_id']]);g['全部机构']=ss
    values[33]=[(k,div(total([r for r in v if int(r['dpd'])>=30],'outstanding_principal'),total(v,'outstanding_principal'))) for k,v in g.items()]
    values[34]=[]
    for m in range(1,10):
        day=f'2026-{m:02}-{calendar.monthrange(2026,m)[1]}'
        values[34].append((day[:7],total([r for r in data['loan_daily_snapshots'] if r['snapshot_on']==day],'outstanding_principal')))
    values[35]=[]
    for label,low,high in [('0',0,0),('1～29',1,29),('30～59',30,59),('60及以上',60,100000)]:
        v=[r for r in ss if low<=int(r['dpd'])<=high and dec(r,'outstanding_principal')>0]
        values[35].append((label,len(v),total(v,'outstanding_principal')))
    values[36]=[]
    for c,v in groups([r for r in loans.values() if r['originated_on']<='2026-08-31'],lambda r:r['customer_id']).items():
        amount=total([r for r in ds if r['disbursed_on']<='2026-08-31' and loans[r['loan_id']]['customer_id']==c],'amount')
        if len(v)>=2 and amount>100000:values[36].append((c,len(v),amount))
    values[36]=sorted(values[36],key=lambda r:(-r[2],r[0]))[:10]
    paid=groups([r for r in rs if r['paid_on'].startswith('2026-09')],lambda r:loans[r['loan_id']]['org_id'])
    amounts={k:total(v,'principal_paid')+total(v,'interest_paid') for k,v in paid.items()};all_paid=sum(amounts.values())
    values[37]=[(orgs[k],v,div(v,all_paid)) for k,v in amounts.items()]
    values[38]=sorted([(r['loan_id'],dec(loans[r['loan_id']],'principal'),dec(r,'outstanding_principal'),dec(loans[r['loan_id']],'principal')-dec(r,'outstanding_principal')) for r in ss],key=lambda r:(-r[3],r[0]))[:10]
    cohort={c:loans[l]['product_id'] for c,(d,l) in first.items() if d.startswith('2026-08')}
    values[39]=[(products[k],total(v,'outstanding_principal')) for k,v in groups([r for r in ss if loans[r['loan_id']]['customer_id'] in cohort],lambda r:cohort[loans[r['loan_id']]['customer_id']]).items()]
    values[40]=[]
    for k,v in groups([r for r in es if r['decided_on']<='2026-09-30' and in_jul(apps[r['application_id']]['applied_on'])],lambda r:apps[r['application_id']]['channel_id']).items():
        n=sum(r['decision']=='approved' for r in v);values[40].append((channels[k],n,len(v),Decimal(n)/len(v)))
    records=[]
    for c in json.loads((ROOT/'cases.json').read_text()):
        n=int(c['id'].split('-')[-1]); expected=[dict(zip(c['expected_columns'],row)) for row in values[n]]
        score=compare(c,expected)
        records.append({'id':c['id'],'independent_csv_matches_gold_sql':score['values_match'],'reference_rows':expected,'comparison':score})
        print(c['id'],score['values_match'],flush=True)
    save(ROOT/'gold_independent_verification.json',{'method':'Independent CSV, Python and Decimal; no SQL execution or metric compiler in calculation','cases':len(records),'passed':sum(r['independent_csv_matches_gold_sql'] for r in records),'results':records})
    assert all(r['independent_csv_matches_gold_sql'] for r in records)


if __name__=='__main__': verify()
