from app.market_diagnosis import diagnosis,quality
from app.catalog import BY_ID
from app.main import app
from fastapi.testclient import TestClient

def item(sid,value,window=None,delta=None,fresh=True):
    s=BY_ID[sid]
    return dict(id=sid,name=s.name,provider=s.provider,unit=s.unit,state='ok',period='2026-10-01',value=value,fresh=fresh,history_context={'windows':{window:{'period':'2026-07-01','delta':delta,'relative_pct':delta}}} if window else {})

def test_growth_threshold_and_missing():
    rows=[item('sahm',.5),item('ip',-1),item('claims4w',200000,'3m',1000)]
    d=diagnosis(rows)['domains'][0]
    assert d['state']=='경계' and d['adverse_count']==3
    rows[1]['fresh']=False
    d=diagnosis(rows)['domains'][0]
    assert d['state']=='판단 보류' and d['available']==2
    assert all(x['available']==0 for x in diagnosis(rows,True)['domains'])

def test_neutral_zero_and_quality_counts():
    rows=[item('sahm',0),item('ip',0),item('claims4w',200000,'3m',0)]
    assert diagnosis(rows)['domains'][0]['state']=='경계 신호 없음'
    rows[0]['value']=None
    q=quality(rows)['providers']['FRED']
    assert q['fresh']==2 and q['missing']==1 and q['total']==3

def test_dashboard_diagnosis():
    with TestClient(app) as c:
        d=c.get('/api/dashboard').json()
        assert len(d['market_diagnosis']['domains'])==7
        assert sum(x['total'] for x in d['provider_quality']['providers'].values())==len(d['indicators'])
