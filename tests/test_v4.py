from copy import deepcopy
from datetime import date,timedelta
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.catalog import BY_ID
from app.guide import GUIDE,explain
from app.analysis import score_inputs,risk
from app.briefing import compact,compare,save_daily_snapshot,build_briefing
from app.dashboard_data import build_dashboard
from app.collect import today,LOCK
from app.db import Session,DailySnapshot


def test_all_38_guides_and_metadata():
    assert set(GUIDE)==set(BY_ID)
    with TestClient(app) as c:
        g=c.get('/api/guide').json()['indicators'];assert len(g)==38
        for x in g:
            assert all(x[k] for k in ('definition','rising','falling','caution','frequency_label','url'))
            assert c.get('/api/guide/'+x['id']).json()==x
        assert c.get('/api/guide/nope').status_code==404

@pytest.mark.parametrize('v,h,ma,phase,score,weights',[
    (19.999,3.999,0,'확장',0,[65,25,10]),
    (20,4,-.001,'확장',50,[55,25,20]),
    (30,6,-1,'하강',100,[10,40,50]),
    (30,3,-1,'하강',70,[10,40,50]),
    (30,4,0,'확장',45,[55,25,20]),
    (10,1,5,'회복',0,[70,20,10]),
    (10,1,1,'둔화',10,[45,35,20]),
])
def test_model_thresholds(v,h,ma,phase,score,weights):
    m=score_inputs(v,h,ma,phase);assert m['score']==score and m['allocation']==weights
    assert sum(m['allocation'])==100
    assert m['inputs']=={'vix':v,'hy':h,'ma_gap':ma,'phase':phase}


def test_live_and_scenario_same_rules_and_read_only():
    with TestClient(app) as c:
        d=c.get('/api/dashboard').json();before=d['model']
        if before['score'] is not None:
            assert c.post('/api/scenario',json=before['inputs']).json()=={**before,'hypothetical':True}
        for p in ({'vix':-1},{'hy':-1},{'ma_gap':-100},{'phase':'미확인'},{'vix':'NaN'},{'vix':1001}):
            body={'vix':25,'hy':3.7,'ma_gap':-2,'phase':'확장',**p}
            assert c.post('/api/scenario',json=body).status_code==422
        r=c.post('/api/scenario',json={'vix':45,'hy':8,'ma_gap':-25,'phase':'하강'})
        assert r.json()['score']==100 and r.json()['hypothetical']
        assert c.get('/api/dashboard').json()['model']==before


def test_period_change_pct_and_report():
    with TestClient(app) as c:
        d=c.get('/api/dashboard').json();items={x['id']:x for x in d['indicators']}
        for sid in ('sp500','kospi','usdkrw'):
            x=items[sid]; p=c.get('/api/series/'+sid+'?limit=2').json()['points']
            assert x['previous_period']==p[0]['date']
            assert x['change_pct']==pytest.approx((p[1]['value']/p[0]['value']-1)*100)
        assert items['core_pce']['change_pct'] is None
        assert '규칙 기반 아침 브리핑' in c.get('/api/report').text
        assert c.get('/static/orbital-v4.js').status_code==200


def test_snapshot_comparison_uses_saved_data_and_same_source():
    with TestClient(app):
        d=build_dashboard(today());past=compact(d)
        lookup={x['id']:x for x in d['indicators']}
        # Older observation vs newer; same period revision; stale, different-source and future source exclusion.
        for sid in ('sp500','vix','hy','kr_leading','nasdaq'):
            x=lookup[sid];x['fresh']=True;x['value']=100.;x['period']='2025-01-02';x['source_identity']='same';past['indicators'][sid]={'fresh':True,'value':99.,'period':'2025-01-01','source_identity':'same'}
        past['indicators']['vix']['period']='2025-01-02'
        lookup['hy']['fresh']=False
        lookup['kr_leading']['source_identity']='changed'
        past['indicators']['nasdaq']['period']='2025-01-03'
        changes=compare(d,past);ids={x['id']:x for x in changes}
        assert set(ids)=={'sp500','vix'}
        assert ids['sp500']['kind']=='관측일 갱신' and ids['vix']['kind']=='동일 관측일 값 수정'
        assert compare({**d,'busy':True},past)==[]
        assert compare(d,{**past,'demo':not d['demo']})==[]


def test_persisted_prior_and_upsert():
    with TestClient(app) as c:
        day=today()-timedelta(days=2)
        d=build_dashboard(today());old=deepcopy(d);old['asof']=str(day)
        key=next(x for x in old['indicators'] if x['id']=='vix');key['value']-=1
        # Save the exact payload under yesterday's observation snapshot date, without rewriting historical observations.
        save_daily_snapshot(day,old);save_daily_snapshot(day,old)
        try:
            with Session() as db: assert db.get(DailySnapshot,day) is not None
            b=c.get('/api/dashboard').json()['briefing'];assert b['previous_date']==str(day)
            assert any(x['id']=='vix' and x['kind']=='동일 관측일 값 수정' for x in b['changes'])
            assert '이전 날짜' not in b['comparison_note']
        finally:
            with Session.begin() as db: db.delete(db.get(DailySnapshot,day))


def test_active_collection_suppresses_signal_and_comparison():
    with TestClient(app) as c:
        assert LOCK.acquire(blocking=False)
        try:
            d=c.get('/api/dashboard').json()
            assert d['busy'] and d['model']['score'] is None and d['model']['allocation'] is None
            assert not d['briefing']['changes'] and '판단 보류' in d['briefing']['headline']
        finally: LOCK.release()
