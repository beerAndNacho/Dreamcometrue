from datetime import date
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from app import kr_pressure as p, kr_market as km
from app.db import Session, MarketCache
from app.main import app

DAY=date(2026,11,26)
AUTH=('admin','test-admin-password-1234')
HEAD={'X-Macro-Admin':'1'}
CFG={'KIS_APP_KEY':'key-test','KIS_APP_SECRET':'secret-test'}


def test_program_and_short_parser():
    assert p.program_row({'arbt_smtm_ntby_qty':'-150','nabt_smtn_ntby_qty':'250'})=={'arbitrage_qty':-150,'nonarbitrage_qty':250,'total_qty':100}
    assert p.program_row({'arbt_smtm_ntby_qty':'NaN','nabt_smtn_ntby_qty':'250'}) is None
    assert p.program_row({'arbt_smtm_ntby_qty':'1.5','nabt_smtn_ntby_qty':'2'}) is None
    assert p.short_row({'ssts_cntg_qty':'12','acml_vol':'100'})['short_pct']==12
    for q,v in [('101','100'),('-1','100'),('0','0'),('inf','100'),('1.5','100')]:
        assert p.short_row({'ssts_cntg_qty':q,'acml_vol':v}) is None
    assert p.loan_row({'new_stcn':'0','rdmp_stcn':'100','rmnd_stcn':'0'})=={'new_qty':0,'returned_qty':100,'balance_qty':0}
    assert p.loan_row({'new_stcn':'-1','rdmp_stcn':'100','rmnd_stcn':'0'}) is None


def test_kis_endpoints_cutoff_and_dates():
    km.TOKEN.clear()
    def handler(req):
        if req.url.path=='/oauth2/tokenP':return httpx.Response(200,json={'access_token':'token','expires_in':86400})
        assert req.method=='GET' and req.headers['authorization']=='Bearer token'
        path=req.url.path.rsplit('/',1)[-1]
        if path=='comp-program-trade-daily':
            assert req.headers['tr_id']=='FHPPG04600001' and req.url.params['FID_MRKT_CLS_CODE']=='Q'
            assert req.url.params['FID_COND_MRKT_DIV_CODE']=='J' and req.url.params['FID_INPUT_DATE_2']=='20261126'
            row={'arbt_smtm_ntby_qty':'-100','nabt_smtn_ntby_qty':'250'}; arr='output'; dt='stck_bsop_date'
        elif path=='daily-short-sale':
            assert req.headers['tr_id']=='FHPST04830000' and req.url.params['FID_INPUT_ISCD']=='005930'
            assert req.url.params['FID_INPUT_DATE_2']=='20261126'
            row={'ssts_cntg_qty':'10','acml_vol':'100'}; arr='output2'; dt='stck_bsop_date'
        else:
            assert path=='daily-loan-trans' and req.headers['tr_id']=='HHPST074500C0'
            assert req.url.params['MRKT_DIV_CLS_CODE']=='3' and req.url.params['MKSC_SHRN_ISCD']=='005930' and req.url.params['END_DATE']=='20261126'
            row={'new_stcn':'50','rdmp_stcn':'20','rmnd_stcn':'1000'}; arr='output1'; dt='bsop_date'
        return httpx.Response(200,json={'rt_cd':'0',arr:[{**row,dt:'20261127'},{**row,dt:'20261126'},{**row,dt:'20261122'}]})
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            assert p.fetch_program(c,CFG,'KOSDAQ',DAY)==[{'date':'2026-11-26','arbitrage_qty':-100,'nonarbitrage_qty':250,'total_qty':150}]
            assert p.fetch_short(c,CFG,'005930',DAY)[0]['short_pct']==10
            assert p.fetch_loan(c,CFG,'005930',DAY)[0]['balance_qty']==1000
    finally:km.TOKEN.clear()


def test_period_metrics_and_direction():
    rows=[{'date':'2026-11-25','short_qty':10,'volume':100,'short_pct':10},{'date':'2026-11-26','short_qty':90,'volume':900,'short_pct':10}]
    rows[0]['short_qty']=50; rows[0]['short_pct']=50
    assert p.summarize('short',rows,2)['short_pct']==14
    assert p.summarize('short',rows,5) is None
    loans=[{'date':x['date'],'new_qty':3,'returned_qty':2,'balance_qty':10+i} for i,x in enumerate(rows)]
    assert p.summarize('loan',loans,2)=={'new_qty':6,'returned_qty':4,'balance_qty':11,'balance_change':1}
    a={'latest':{'date':'2026-11-26','foreign':10},'stale':False}; b={'latest':{'date':'2026-11-26','foreign_contracts':-5},'stale':False}
    assert p.comparison(a,b,'KOSPI')['label']=='방향 엇갈림'
    assert p.comparison(a,b,'KOSDAQ')['state']=='unavailable'
    assert p.comparison(a,{**b,'stale':True},'KOSPI')['state']=='unavailable'
    assert p.comparison(a,{**b,'latest':{**b['latest'],'date':'2026-11-25'}},'KOSPI')['state']=='unavailable'


def test_csv_strict_schema_and_values():
    hdr='date,foreign_contracts,institution_contracts,individual_contracts\n'
    assert p.parse_import('futures',hdr+'2026-11-26,-100,50,50\n',DAY)[0]['foreign_contracts']==-100
    for data in ['2026-11-27,1,2,3','2026-11-22,1,2,3','2026-11-26,1.2,2,3','2026-11-26,NaN,2,3','2026-11-26,1,2','2026-11-26,1,2,3,4','2026-11-26,1,2,3\n2026-11-26,1,2,3']:
        with pytest.raises(ValueError):p.parse_import('futures',hdr+data+'\n',DAY)
    hdr2='date,balance_qty,balance_pct\n'
    assert p.parse_import('short_balance',hdr2+'2026-11-26,10000,0.12',DAY)[0]['balance_pct']==.12
    for val in ['-1,0.2','1,101','1,-1','1,NaN']:
        with pytest.raises(ValueError):p.parse_import('short_balance',hdr2+'2026-11-26,'+val,DAY)


def test_demo_auth_routes_and_no_external_requests(monkeypatch):
    def fail(*a,**kw):raise AssertionError('external API must not run in demo')
    monkeypatch.setattr(p,'fetch_program',fail)
    with TestClient(app) as c:
        assert c.get('/kr-pressure').status_code==401
        assert c.get('/kr-pressure',auth=AUTH).status_code==200
        assert c.get('/api/kr-pressure').status_code==401
        r=c.get('/api/kr-pressure?market=KOSPI&code=&period=20',auth=AUTH).json()
        assert r['demo'] and r['data']['program']['summary'] and r['comparison']['state']=='ok'
        assert c.get('/api/kr-pressure?period=7',auth=AUTH).status_code==422
        assert c.get('/api/kr-pressure?code=111111',auth=AUTH).status_code==404
        assert c.post('/api/kr-pressure/refresh',auth=AUTH).status_code==403
        assert c.post('/api/kr-pressure/refresh',auth=AUTH,headers=HEAD).status_code==409
        assert c.post('/api/kr-pressure/import',auth=AUTH,headers=HEAD,json={'kind':'futures','csv':'x'}).status_code==409


def test_manual_import_confirmation_merge_and_staleness(monkeypatch):
    monkeypatch.setattr(p,'DEMO',False);monkeypatch.setattr(p,'today',lambda:date(2026,11,27))
    key=p.key('futures','KOSPI',''); hdr='date,foreign_contracts,institution_contracts,individual_contracts\n'
    with TestClient(app) as c:
        try:
            req={'kind':'futures','csv':hdr+'2026-11-26,100,-50,-50\n','confirmed':True}
            assert c.post('/api/kr-pressure/import',auth=AUTH,json=req).status_code==403
            assert c.post('/api/kr-pressure/import',auth=AUTH,headers={**HEAD,'Origin':'https://evil.example'},json=req).status_code==403
            assert c.post('/api/kr-pressure/import',auth=AUTH,headers=HEAD,json={**req,'confirmed':False}).status_code==400
            assert c.post('/api/kr-pressure/import',auth=AUTH,headers=HEAD,json=req).status_code==200
            assert p.state('futures','KOSPI','')['latest']['foreign_contracts']==100
            assert c.post('/api/kr-pressure/import',auth=AUTH,headers=HEAD,json={**req,'csv':hdr+'2026-11-26,-100,50,50\n'}).status_code==200
            assert p.state('futures','KOSPI','')['latest']['foreign_contracts']==-100
            assert c.post('/api/kr-pressure/import',auth=AUTH,headers=HEAD,json={**req,'csv':hdr+'2026-11-27,1,2,3\n'}).status_code==400
            assert p.state('futures','KOSPI','')['latest']['foreign_contracts']==-100
            monkeypatch.setattr(p,'today',lambda:date(2026,12,8))
            assert p.state('futures','KOSPI','')['stale']
        finally:
            with Session.begin() as db:db.execute(delete(MarketCache).where(MarketCache.key==key))


def test_refresh_independent_errors_and_preserves_last(monkeypatch):
    monkeypatch.setattr(p,'DEMO',False);monkeypatch.setattr(p,'today',lambda:date(2026,11,27));monkeypatch.setattr(p,'LAST',-1000.);monkeypatch.setattr(p,'monotonic',lambda:1000.);monkeypatch.setattr(p,'sleep',lambda _:None)
    monkeypatch.setattr(p,'load_settings',lambda:CFG);monkeypatch.setattr(p.km,'watches',lambda:[{'code':'005930','name':'Test'}])
    monkeypatch.setattr(p,'fetch_program',lambda *a:[{'date':'2026-11-26','arbitrage_qty':1,'nonarbitrage_qty':2,'total_qty':3}]);monkeypatch.setattr(p,'fetch_short',lambda *a:[{'date':'2026-11-26','short_qty':1,'volume':10,'short_pct':10}])
    def fail(*a):raise RuntimeError('secret-test should not be exposed')
    monkeypatch.setattr(p,'fetch_loan',fail)
    with TestClient(app) as c:
        keys=[p.key(k,'KOSPI','005930') for k in ('program','short','loan')]
        try:
            km.store(keys[2],[{'date':'2026-11-25','new_qty':1,'returned_qty':0,'balance_qty':3}])
            r=c.post('/api/kr-pressure/refresh?code=005930',auth=AUTH,headers=HEAD)
            assert r.status_code==200 and [x['ok'] for x in r.json()['results']]==[True,True,False]
            assert 'secret-test' not in r.text
            assert p.state('loan','KOSPI','005930')['stale'] and p.state('loan','KOSPI','005930')['latest']['balance_qty']==3
            assert c.post('/api/kr-pressure/refresh?code=005930',auth=AUTH,headers=HEAD).status_code==429
        finally:
            with Session.begin() as db:db.execute(delete(MarketCache).where(MarketCache.key.in_(keys)))
