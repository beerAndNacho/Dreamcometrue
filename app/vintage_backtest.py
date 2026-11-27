"""Monthly backtest using only CLI revisions already recorded by this installation.
A recorded ingestion timestamp is not the official publication timestamp.
"""
import csv
import io
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from .admin import require_admin, require_write
from .analysis import ALLOC, cycle, month_before
from .db import Revision, Session, DEMO
from .collect import today

router=APIRouter(tags=['journal backtest'])
KST=ZoneInfo('Asia/Seoul')

class VintageRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    csv:str=Field(min_length=1,max_length=250000)
    fee_bps:float=Field(default=10,ge=0,le=100,allow_inf_nan=False)


def parse_prices(text):
    r=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if r.fieldnames!=['month','stock','bond','cash']:raise ValueError('헤더: month,stock,bond,cash')
    rows=[]
    for row in r:
        if len(rows)>=600:raise ValueError('최대 600개월')
        d=date.fromisoformat(row['month']+'-01');v=[float(row[k]) for k in ('stock','bond','cash')]
        if d>=today().replace(day=1) or not all(math.isfinite(x) and 1e-12<=x<=1e15 for x in v):raise ValueError('완료된 과거 월 및 양수 총수익지수 필요')
        if None in row or len(row)!=4:raise ValueError('CSV 열 개수 오류')
        rows.append((d,v))
    rows.sort(key=lambda x:x[0])
    if len(rows)<13 or any(month_before(b[0],1)!=a[0] for a,b in zip(rows,rows[1:])):raise ValueError('연속 13개월 이상, 중복 없는 자료 필요')
    return rows


def cutoff_for(month):
    # Exclusive midnight KST boundary. Use the previous price month START
    # to avoid assuming its final trading close or publication time.
    return datetime.combine(month,time.min,tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)


def simulate(rows,revisions,fee_bps):
    if not math.isfinite(fee_bps) or not 0<=fee_bps<=100:raise ValueError('수수료 범위 확인')
    revisions=sorted(revisions,key=lambda x:(x[1],x[3]));known={};j=0;equity=bench=peak=bpeak=1.;mdd=bmdd=0.;weights=[0.,0.,1.];points=[];cost_total=0.;skipped=0
    for (prev,old),(d,vals) in zip(rows,rows[1:]):
        cutoff=cutoff_for(prev)
        while j<len(revisions) and revisions[j][1]<cutoff:
            period,seen,value,_=revisions[j];known[period]=(value,seen);j+=1
        usable=[(period,v[0]) for period,v in sorted(known.items()) if period<=prev and period.day==1 and math.isfinite(v[0]) and v[0]>0]
        c=cycle(usable)
        if not usable or (prev-usable[-1][0]).days>95 or c['phase'] not in ALLOC:
            if points:raise ValueError('검증 기간 중 당시 저장된 CLI가 누락·보합·오래됨: 연속 구간을 선택하세요.')
            skipped+=1;continue
        target=[x/100 for x in ALLOC[c['phase']]]
        # One-way fee on both purchase and sale notionals; cash leg excluded.
        turnover=sum(abs(a-b) for a,b in zip(target[:2],weights[:2]));fee=turnover*fee_bps/10000
        growth=[v/p for v,p in zip(vals,old)];gross=sum(w*g for w,g in zip(target,growth))
        cost_total+=equity*fee;equity*=gross*(1-fee);bench*=growth[0]*(1-fee_bps/10000 if not points else 1)
        if not all(math.isfinite(x) and 1e-100<=x<=1e100 for x in (equity,bench)):raise ValueError('수익률 계산 범위 초과')
        weights=[w*g/gross for w,g in zip(target,growth)];peak=max(peak,equity);bpeak=max(bpeak,bench);mdd=min(mdd,equity/peak-1);bmdd=min(bmdd,bench/bpeak-1)
        latest=usable[-1][0];prior=month_before(latest,3)
        points.append({'month':d.strftime('%Y-%m'),'strategy':equity,'benchmark':bench,'phase':c['phase'],'cli_period':str(latest),'cli_seen_utc':known[latest][1].isoformat()+'Z','prior_seen_utc':known[prior][1].isoformat()+'Z','turnover_pct':turnover*100,'cost_pct':fee*100})
    if len(points)<12:raise ValueError('당시 저장된 CLI로 연속 12개월 수익 구간을 확보하지 못했습니다. 최초 수집 이전에는 이 모드가 작동하지 않습니다.')
    return {'months':len(points),'skipped_months':skipped,'strategy_return_pct':(equity-1)*100,'benchmark_return_pct':(bench-1)*100,'mdd_pct':mdd*100,'benchmark_mdd_pct':bmdd*100,'fee_initial_capital_ratio':cost_total,'points':points,'warning':'직전 기준 월 시작 전(KST) 서버 수집 이력만 사용 (보수적 한 달 지연). 공식 최초 발표값 아님. 월말 총수익지수·동일 통화는 사용자 확인, 월별 CLI 기본 배분만 사용. 위험도·세금·실제 체결/슬리피지 제외. 비용은 목표 비중 대비 매수+매도 대금의 근사치.'}

@router.post('/api/journal/backtest',dependencies=[Depends(require_write)])
def run(req:VintageRequest):
    try:
        rows=parse_prices(req.csv)
        if DEMO:raise ValueError('데모 가상 데이터에서는 시점 검증 백테스트를 실행하지 않습니다.')
        with Session() as db:
            q=select(Revision.period,Revision.seen,Revision.value,Revision.id).where(Revision.series=='cli_us',Revision.seen<cutoff_for(rows[-2][0])).order_by(Revision.seen,Revision.id).limit(100001)
            revisions=db.execute(q).tuples().all()
        if len(revisions)>100000:raise ValueError('수집 수정 이력 범위 초과')
        return simulate(rows,revisions,req.fee_bps)
    except (ValueError,TypeError,KeyError,OverflowError,ZeroDivisionError,csv.Error) as e:
        # Only emit application-authored validation messages; never reflect CSV input.
        message=str(e) if isinstance(e,ValueError) and str(e).startswith(('헤더:', '최대 ', '완료된 ', 'CSV 열', '연속 ', '수수료 ', '검증 기간 ', '수익률 계산 ', '당시 저장된 ', '데모 가상 ', '수집 수정 ')) else 'CSV 형식 또는 계산 범위를 확인하세요.'
        raise HTTPException(400,message) from None
