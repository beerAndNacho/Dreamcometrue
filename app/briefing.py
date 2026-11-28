"""Historical comparisons use actual saved snapshots, never reconstructed 'yesterday'."""
import json
from datetime import date
from math import isfinite
from sqlalchemy import select
from .db import DailySnapshot, Session, now

PRIORITY=('sp500','nasdaq','kospi','kosdaq','usdkrw','vix','hy','core_pce','cpi','fed','kr_rate','us10y','unrate','cli_us','cli_kr','cli_uk')

def compact(d):
    return {'demo':d['demo'],'asof':d['asof'],'generated_at':d['generated_at'],
            'indicators':{x['id']:{k:x.get(k) for k in ('value','period','fresh','source_identity')} for x in d['indicators']},
            'model':{k:d['model'].get(k) for k in ('score','allocation')},
            'phases':{k:v['phase'] for k,v in d['cycles'].items()},
            'market_diagnosis':d.get('market_diagnosis')}

def save_daily_snapshot(day,d):
    if d['busy'] or str(day)!=d['asof']: return
    payload=json.dumps(compact(d),ensure_ascii=False,allow_nan=False)
    with Session.begin() as db:
        db.merge(DailySnapshot(day=day,payload=payload,updated=now()))

def compare(d, prior):
    changes=[]
    if not prior or d['busy'] or prior.get('demo')!=d['demo']: return changes
    previous=prior['indicators']
    for x in d['indicators']:
        old=previous.get(x['id'])
        if not old or not x['fresh'] or not old['fresh'] or x['period']<old['period']: continue
        if x.get('source_identity')!=old.get('source_identity'): continue
        a,b=old['value'],x['value']
        if not all(isinstance(v,(float,int)) and isfinite(v) for v in (a,b)): continue
        if x['period']==old['period'] and abs(b-a)<1e-9: continue
        changes.append({'id':x['id'],'name':x['name'],'unit':x['unit'],'url':x['url'],'current':b,'previous':a,'change':b-a,'period':x['period'],'previous_period':old['period'],'kind':'관측일 갱신' if x['period']>old['period'] else '동일 관측일 값 수정'})
    return sorted(changes,key=lambda x:(PRIORITY.index(x['id']) if x['id'] in PRIORITY else len(PRIORITY),x['id']))

def build_briefing(d):
    model=d['model']; indicators={x['id']:x for x in d['indicators']}; notes=[]
    def note(kind,text,ids=()):
        notes.append({'kind':kind,'text':text,'series':list(ids)})
    def usable(sid):
        x=indicators[sid];return x if x['fresh'] and x['value'] is not None else None
    if d['busy']:
        return {'headline':'자료 갱신 중 — 판단 보류','notes':[],'comparison_note':'수집이 완료되면 이전 스냅샷과 비교합니다.','previous_date':None,'previous_at':None,'changes':[],'previous_model':None}
    phase=d['cycles']['cli_us']['phase']
    if model['score'] is None:
        headline='투자 모형 판단 보류'
        note('check','필수 자료 확인: '+', '.join(model['missing']))
    else:
        headline=f'미국 경기 {phase} · 규칙 기반 위험 {model["label"]}'
        reduction=model.get('equity_reduction',0)
        note('neutral',f'기본 주식 비중 {model["base_allocation"][0]}%, 위험 조정 {reduction}%p 축소 → 모델 주식 {model["allocation"][0]}%. 하락 확률이나 매매 지시가 아닙니다.',('cli_us','vix','hy','sp500'))
        if model['checks'][2]['points']:
            note('caution',f'S&P 500이 200거래일 평균을 하회합니다 ({model["checks"][2]["value"]:+.2f}%).',('sp500',))
        else: note('support','S&P 500이 200거래일 평균 이상에 있습니다. 추세 반전 가능성은 별도입니다.',('sp500',))
    for sid,threshold,name,unit in [('vix',20,'VIX','pt'),('hy',4,'하이일드 OAS','%p')]:
        x=usable(sid)
        if x: note('caution' if x['value']>=threshold else 'support',f'{name} {x["value"]:.2f}{unit} ({x["period"]}) — 모델 첫 경계 {threshold}{unit} '+('이상.' if x['value']>=threshold else '미만.'),(sid,))
    x=usable('pce')
    if x: note('caution' if x['value']>2 else 'neutral',f'전체 PCE 전년비 {x["value"]:.2f}% ({x["period"]}) — 연준의 장기 2% 목표와 비교. 단독으로 금리 결정을 예측하지 않습니다.',('pce',))
    for sid,name in [('cli_kr','한국'),('cli_uk','영국')]:
        c=d['cycles'][sid]
        if c['phase']!='미확인': note('neutral',f'{name} CLI {c["phase"]} ({c["date"]}, 3개월 {c["change"]:+.2f}pt).', (sid,))
    with Session() as db:
        row=db.scalar(select(DailySnapshot).where(DailySnapshot.day<date.fromisoformat(d['asof'])).order_by(DailySnapshot.day.desc()).limit(1))
    prior=json.loads(row.payload) if row else None
    if prior and prior.get('demo')!=d['demo']: prior=None
    changes=compare(d,prior)
    msg=(f'{prior["asof"]} 마지막 저장 스냅샷 대비 {len(changes)}개 갱신·수정. 양쪽에서 신선한 동일 출처 자료만 비교합니다.' if prior else '이전 날짜 스냅샷 없음 — 첫 수집 이후 다른 날짜의 수집이 완료되면 비교할 수 있습니다.')
    return {'headline':headline,'notes':notes,'comparison_note':msg,'previous_date':prior['asof'] if prior else None,'previous_at':prior['generated_at'] if prior else None,'changes':changes,'previous_model':prior['model'] if prior else None}
