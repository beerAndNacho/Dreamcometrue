"""Forward price observations from genuinely saved daily snapshots, no replay.

A snapshot is eligible only if its KST creation date equals its saved date.
Signals are evaluated using the first subsequently captured *new* price
period after the signal day (not the price already known at signal time).
Non-overlapping samples per domain/horizon; descriptive, not strategy returns.
"""
import json
from bisect import bisect_left
from datetime import date,datetime,timedelta
from statistics import mean,median
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .db import DailySnapshot,Session
from .market_diagnosis import RULES,number

VERSION='experimental-v1'
STATES=('경계','혼조','경계 신호 없음')
KST=ZoneInfo('Asia/Seoul')
MIN_SAMPLE=10
ENTRY_DAYS=10
EXIT_DAYS=10
TARGETS={'korea':'kospi','semiconductor':'sox'}
def day(v):
    try:return date.fromisoformat(v)
    except (TypeError,ValueError):return None

def eligible(raw,asof,demo):
    if not isinstance(raw,dict) or raw.get('demo') is not demo:return False
    d=day(raw.get('asof'))
    if d is None or d>asof:return False
    try:stamp=datetime.fromisoformat(raw['generated_at'])
    except (KeyError,TypeError,ValueError):return False
    return stamp.tzinfo is not None and stamp.astimezone(KST).date()==d

def price_tape(rows,sid):
    """First stored version of each increasing observed price date."""
    tape=[]
    for s in rows:
        saved=day(s['asof']);x=(s.get('indicators') or {}).get(sid) or {}
        observed=day(x.get('period'));v=x.get('value');identity=x.get('source_identity')
        if not x.get('fresh') or not observed or observed>saved or not number(v) or v<=0 or not identity:continue
        if (saved-observed).days>7 or (tape and observed<=tape[-1]['date']):continue
        tape.append({'date':observed,'saved':saved,'value':v,'identity':identity})
    return tape

def evaluate(rows,asof,demo,horizon=30):
    if horizon not in (30,90):raise ValueError('30 또는 90일')
    # The latest same-day snapshot is the source of truth (one row per day).
    unique={s['asof']:s for s in rows if eligible(s,asof,demo)}
    rows=[unique[k] for k in sorted(unique)]
    tapes={sid:price_tape(rows,sid) for sid in ('sp500','kospi','sox')}
    result=[]
    for key,label,_ in RULES:
        target=TARGETS.get(key,'sp500');tape=tapes[target];dates=[x['date'] for x in tape]
        samples=[];pending=missing=overlap=hold=signals=0;previous_exit=None
        for s in rows:
            model=s.get('market_diagnosis') or {}
            if model.get('version')!=VERSION:continue
            domain=next((x for x in model.get('domains',[]) if x.get('id')==key),None)
            if not domain:continue
            if domain.get('state') not in STATES or domain.get('available')!=domain.get('total') or not domain.get('total'):
                hold+=1;continue
            signals+=1;signal=day(s['asof'])
            if previous_exit is not None and signal<=previous_exit:overlap+=1;continue
            i=bisect_left(dates,signal+timedelta(days=1))
            if i>=len(tape) or tape[i]['saved']>signal+timedelta(days=ENTRY_DAYS):
                if asof<=signal+timedelta(days=ENTRY_DAYS):pending+=1
                else:missing+=1
                continue
            entry=tape[i];target_day=entry['date']+timedelta(days=horizon)
            j=bisect_left(dates,target_day)
            if j>=len(tape) or tape[j]['saved']>target_day+timedelta(days=EXIT_DAYS):
                if asof<=target_day+timedelta(days=EXIT_DAYS):pending+=1
                else:missing+=1
                continue
            exit=tape[j]
            if entry['identity']!=exit['identity'] or exit['saved']<=entry['saved']:
                missing+=1;continue
            # Ensure no source switch inside the evaluated interval.
            if any(p['identity']!=entry['identity'] for p in tape[i:j+1]):
                missing+=1;continue
            change=(exit['value']/entry['value']-1)*100
            if not number(change):missing+=1;continue
            previous_exit=exit['saved']
            samples.append({'signal_day':str(signal),'state':domain['state'],'entry_period':str(entry['date']),'entry_saved':str(entry['saved']),'entry_value':entry['value'],'exit_period':str(exit['date']),'exit_saved':str(exit['saved']),'exit_value':exit['value'],'price_change_pct':round(change,6)})
        stats=[]
        for state in STATES:
            values=[s['price_change_pct'] for s in samples if s['state']==state];n=len(values)
            stats.append({'state':state,'n':n,'mean_pct':round(mean(values),4) if n>=MIN_SAMPLE else None,'median_pct':round(median(values),4) if n>=MIN_SAMPLE else None,'negative_count':sum(v<0 for v in values) if n>=MIN_SAMPLE else None})
        result.append({'id':key,'name':label,'target':target,'signals':signals,'hold':hold,'n':len(samples),'pending':pending,'missing':missing,'overlap_excluded':overlap,'stats':stats,'samples':samples[-20:]})
    return {'version':VERSION,'demo':demo,'asof':str(asof),'horizon_days':horizon,'snapshot_count':len(rows),'first_snapshot':rows[0]['asof'] if rows else None,'last_snapshot':rows[-1]['asof'] if rows else None,'min_group_sample':MIN_SAMPLE,'domains':result,'note':'실제 저장 스냅샷만 평가. 신호일 다음 관측일부터 저장된 가격지수 비교; 30/90 달력일 및 최대 10일 저장 허용. 분야별 평가 구간 중복 제외. 그룹 10건 미만 통계 보류. 배당·환율·비용 미반영, 거래 수익률·검증된 예측력·하락확률 아님. 과거 신호 재생성 금지.'}

def validation(asof,demo,horizon=30,days=3650):
    with Session() as db:
        entries=db.scalars(select(DailySnapshot).where(DailySnapshot.day>=asof-timedelta(days=days),DailySnapshot.day<=asof).order_by(DailySnapshot.day)).all()
        rows=[]
        for entry in entries:
            try:s=json.loads(entry.payload)
            except (ValueError,TypeError):continue
            if isinstance(s,dict) and s.get('asof')==str(entry.day):rows.append(s)
    return dict(evaluate(rows,asof,demo,horizon),lookback_days=days)
