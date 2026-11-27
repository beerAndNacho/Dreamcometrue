from datetime import date,timedelta
import os,subprocess,sys
import httpx,pytest
from fastapi.testclient import TestClient
from app.main import app
from app.analysis import risk, fresh, backtest
from app.catalog import BY_ID,SERIES
from app.providers import ecos,DataError,kosis
from app.settings import clean_query,query_fingerprint
from app.db import Session,history
from app.collect import today

AUTH=('admin','test-admin-password-1234')
HEAD={'X-Macro-Admin':'1'}

def test_report_quality_trend():
    with TestClient(app) as c:
        d=c.get('/api/dashboard').json()
        assert d['quality']['total']==len(SERIES)
        assert d['quality']['fresh']+d['quality']['missing']+d['quality']['stale']==len(SERIES)
        assert len(d['indicators'][0]['trend'])<=30
        assert d['generated_at'] and all(i['age_days'] is not None for i in d['indicators'])
        r=c.get('/api/report'); assert r.status_code==200 and '가상 데이터' in r.text and '코스피' in r.text
        assert 'attachment' in r.headers['content-disposition']
        assert 'Content-Security-Policy' in c.get('/').headers
        assert c.get('/static/cosmos.css').status_code==200
        assert c.get('/static/orbits.svg').status_code==200
        assert len(c.get('/api/series/cpi?limit=120').json()['points'])==109
        assert c.get('/api/series/cpi?limit=3').json()['points']==c.get('/api/series/cpi?limit=120').json()['points'][-3:]

def test_portfolio_private_roundtrip():
    payload={'holdings':[{'symbol':'테스트','currency':'KRW','eps':10,'price':100,'qty':2,'pers':[8,10,12]}],'stock':8000,'bond':1000,'cash':1000,'ai':60,'ai_return':-30,'other_return':-15,'bond_return':2,'mode':'manual','target':[60,30,10]}
    with TestClient(app) as c:
        assert c.get('/api/portfolio').status_code==401
        assert c.put('/api/portfolio',auth=AUTH,json=payload).status_code==403
        r=c.put('/api/portfolio',auth=AUTH,headers=HEAD,json=payload)
        assert r.status_code==200 and r.json()['snapshot']==payload
        assert c.get('/api/portfolio',auth=AUTH).json()==r.json()
        assert c.put('/api/portfolio',auth=AUTH,headers=HEAD,json={**payload,'target':[20,20,20]}).status_code==422
        invalid={**payload,'holdings':[dict(payload['holdings'][0],pers=[12,10,8])]}
        assert c.put('/api/portfolio',auth=AUTH,headers=HEAD,json=invalid).status_code==422
        assert c.put('/api/portfolio',auth=AUTH,headers={**HEAD,'Origin':'https://evil.example'},json=payload).status_code==403
        assert c.post('/api/admin/reset-kosis/kr_leading?confirm=true',auth=AUTH,headers=HEAD).status_code==409

def test_body_limit():
    with TestClient(app) as c:
        assert c.post('/api/backtest',content=b'a'*1_000_001).status_code==413
        def stream():
            yield b'a'*600_000
            yield b'b'*600_000
        assert c.post('/api/backtest',content=stream()).status_code==413

def test_ecos_wrong_identity():
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'StatisticSearch':{'list_total_count':1,'row':[{'ITEM_NAME1':'국고채(3년)','ITEM_CODE1':'010210000','TIME':'20250103','DATA_VALUE':'3'}]}}))) as c:
        with pytest.raises(DataError,match='항목명'): ecos(c,BY_ID['kr10y'],date(2025,1,1),date(2025,2,1),{'ECOS_API_KEY':'test'})

def test_kosis_fingerprint_and_single_item():
    a='orgId=101&tblId=TEST&itmId=T1+&objL1=A&apiKey=ignored'
    b='objL1=A&itmId=T1&tblId=TEST&orgId=101'
    assert query_fingerprint(a)==query_fingerprint(b)
    assert 'ignored' not in clean_query(a)
    with pytest.raises(ValueError):clean_query('orgId=101&tblId=TEST&itmId=T1+T2&objL1=A')
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=[{'ITM_ID':'T2','C1':'A','PRD_DE':'202501','DT':'100.1'}]))) as c:
        with pytest.raises(DataError):kosis(c,BY_ID['kr_leading'],date(2025,1,1),date(2025,2,1),{'KOSIS_API_KEY':'test','KOSIS_LEADING_QUERY':b})

def test_risk_invalid_prices():
    end=date(2025,11,1)
    rows={'vix':[(end,20)],'hy':[(end,4)],'sp500':[(end-timedelta(days=200-i),0) for i in range(200)]}
    assert risk(rows,end,'확장')['score'] is None
    assert not fresh('vix',[(end-timedelta(days=8),20)],end)
    assert risk(rows,end,'보합')['allocation'] is None

def test_full_collection_and_incremental_and_provenance(tmp_path):
    env={**os.environ,'HOSTED_MODE':'0','DEMO_MODE':'0','DATABASE_URL':'sqlite:///'+str(tmp_path/'live.db'),'SETTINGS_DIR':str(tmp_path/'config'),'AUTO_COLLECT':'0','HISTORY_START':'2020-01-01'}
    code=r'''
from datetime import date,timedelta
from app.db import init,Session,Status,SeriesSource,history
from app.settings import save_settings
from app import collect as c
from app.catalog import SERIES
from app.main import dashboard,app
from fastapi.testclient import TestClient
from unittest.mock import patch
from app.analysis import month_before
from sqlalchemy import select
init()
q='orgId=101&tblId=TEST&itmId=A&objL1=B'
save_settings({'FRED_API_KEY':'mock','ECOS_API_KEY':'mock','KOSIS_API_KEY':'mock','KOSIS_LEADING_QUERY':q,'KOSIS_COINCIDENT_QUERY':q})
starts={}
def fake(client,s,start,end,cfg):
    starts[s.id]=start
    if s.freq=='D': return {end-timedelta(days=i):100+i*.01 for i in range(300) if end-timedelta(days=i)>=start and (end-timedelta(days=i)).weekday()<5}
    return {month_before(end.replace(day=1),i):100+i*.01 for i in range(1,25)}
def oecd(client,start,end):return {s.id:fake(client,s,start,end,{}) for s in SERIES if s.provider=='OECD'}
with patch.object(c.p,'fred',fake),patch.object(c.p,'ecos',fake),patch.object(c.p,'kosis',fake),patch.object(c.p,'oecd',oecd),patch.object(c.customs,'fetch',fake),patch.object(c.time,'sleep',lambda *_:None):
    assert c.collect()['ok']==len(SERIES)
    assert starts['sp500']==date(2020,1,1)
    assert c.collect(full=False)['ok']==len(SERIES)
    assert starts['sp500']>=c.today()-timedelta(days=46)
    assert len(dashboard()['indicators'])==len(SERIES)
    with Session() as db: assert db.get(SeriesSource,'kr_leading') and len(history(db,'sp500'))>200
    save_settings({'KOSIS_LEADING_QUERY':'orgId=101&tblId=OTHER&itmId=A&objL1=B'})
    d=dashboard();assert not next(x for x in d['indicators'] if x['id']=='kr_leading')['fresh']
    assert c.collect()['error']==1
    with TestClient(app) as client:
        auth=('admin','test-admin-password-1234');h={'X-Macro-Admin':'1'}
        assert client.post('/api/admin/reset-kosis/kr_leading',auth=auth,headers=h).status_code==400
        assert client.post('/api/admin/reset-kosis/kr_leading?confirm=true',auth=auth,headers=h).status_code==200
    assert c.collect()['ok']==len(SERIES)
    with Session() as db: assert db.get(SeriesSource,'kr_leading')
'''
    subprocess.run([sys.executable,'-c',code],env=env,check=True,timeout=50)
