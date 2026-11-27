from datetime import datetime
import httpx
import pytest
from fastapi.testclient import TestClient
from app import kr_insights as ki
from app import kr_market as km
from app.main import app

AUTH=('admin','test-admin-password-1234')
HEAD={'X-Macro-Admin':'1'}

def row(code, f, i, name='test'):
    return {'mksc_shrn_iscd':code,'hts_kor_isnm':name,'frgn_ntby_tr_pbmn':f,'orgn_ntby_tr_pbmn':i,'stck_prpr':'10000','prdy_ctrt':'-1.2'}

def test_rank_units_sign_order_and_missing():
    rows=[row('005930','10000','-100'),row('000660','30000','15000'),row('000270','-40000','-10000'),row('000001','NaN',''),row('000002','0','10')]
    buy=ki.parse_ranking(rows,'foreign','buy')
    assert [(r['code'],r['foreign']) for r in buy]==[('000660',300),('005930',100)]
    assert ki.parse_ranking(rows,'institution','sell')[0]['code']=='000270'
    assert ki.parse_ranking([], 'foreign', 'buy')==[]
    with pytest.raises(km.MarketError):ki.parse_ranking([row('005930','NaN','')],'foreign','buy')
    with pytest.raises(km.MarketError):ki.parse_ranking([row('005930','100','200'),row('005930','200','200')],'foreign','buy')
    assert len(ki.parse_ranking([row(f'{n:06}',str(n+1),'0') for n in range(50)],'foreign','buy'))==30


def test_official_rank_parameters(monkeypatch):
    km.TOKEN.clear()
    def handler(req):
        if req.url.path.endswith('/tokenP'):
            return httpx.Response(200,json={'access_token':'test-token','expires_in':86400})
        assert req.url.path.endswith('/foreign-institution-total')
        assert req.headers['tr_id']=='FHPTJ04400000'
        assert dict(req.url.params)=={'FID_COND_MRKT_DIV_CODE':'V','FID_COND_SCR_DIV_CODE':'16449','FID_INPUT_ISCD':'1001','FID_DIV_CLS_CODE':'1','FID_RANK_SORT_CLS_CODE':'1','FID_ETC_CLS_CODE':'2'}
        return httpx.Response(200,json={'rt_cd':'0','output':[row('000660','100','-300')]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert ki.fetch_ranking(client,{'KIS_APP_KEY':'test-key','KIS_APP_SECRET':'test-secret'},ki.RankRequest(market='KOSDAQ',investor='institution',direction='sell'))[0]['institution']==-3
    km.TOKEN.clear()

DATES=['2026-11-19','2026-11-20','2026-11-23','2026-11-24','2026-11-25','2026-11-26']
def market(dates=DATES, stale=False):return {'stale':stale,'rows':[{'date':d} for d in dates]}
def watch(code, dates=DATES, stale=False, foreign=2, institution=1, price_stale=False):
    return {'code':code,'name':code,'flow':{'stale':stale,'rows':[{'date':d,'foreign':foreign,'institution':institution,'individual':-3} for d in dates]},'price':{'stale':price_stale,'rows':[{'date':d,'close':100+n*2} for n,d in enumerate(DATES)]}}


def test_watchlist_equal_dates_group_coverage_and_price():
    items=[watch('005930'),watch('000660',foreign=-1),watch('000270',dates=DATES[:-1]),watch('035720',stale=True)]
    result=ki.compare_watchlist(items,[market(),market()],5,{'005930':'반도체','000660':'반도체','000270':'자동차','035720':'반도체'})
    assert result['included']==2 and result['registered']==4
    assert result['dates']==DATES[-5:]
    assert result['rows'][0]['foreign']==10 and result['rows'][0]['both_buy']
    assert result['rows'][0]['price_return_pct']==10
    assert not result['rows'][1]['both_buy'] and len(result['excluded'])==2
    groups={r['name']:r for r in result['sectors']}
    assert groups['반도체']['included']==2 and groups['반도체']['registered']==3 and groups['반도체']['foreign']==5
    assert groups['자동차']['foreign'] is None


def test_compare_refuses_different_calendar_and_stale():
    for m in [market(DATES[:-1]),market(stale=True),market(['2026-11-01']+DATES[-4:])]:
        result=ki.compare_watchlist([watch('005930')],[market(),m],5,{})
        assert result['reason'] and not result['rows']
    result=ki.compare_watchlist([watch('005930',price_stale=True)],[market(),market()],5,{})
    assert result['included']==1 and result['rows'][0]['price_return_pct'] is None
    assert ki.compare_watchlist([watch('005930',foreign=float('nan'))],[market(),market()],5,{})['included']==0


def test_intraday_live_window_cache_and_lock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return cls(2026,11,27,11,20,tzinfo=tz)
    monkeypatch.setattr(ki,'DEMO',False)
    monkeypatch.setattr(ki,'datetime',Clock)
    monkeypatch.setattr(ki,'load_settings',lambda:{'KIS_APP_KEY':'test','KIS_APP_SECRET':'secret'})
    monkeypatch.setattr(ki,'RANK_CACHE',{})
    monkeypatch.setattr(ki,'LAST_ATTEMPT',-1000.)
    monkeypatch.setattr(ki,'monotonic',lambda:1000.)
    calls=[]
    def fetch(*args):calls.append(1);return [{'code':'005930','foreign':10}]
    monkeypatch.setattr(ki,'fetch_ranking',fetch)
    with TestClient(app) as c:
        r=c.post('/api/kr-insights/ranking',auth=AUTH,headers=HEAD,json={});assert r.status_code==200 and not r.json()['cached'] and r.json()['observation_date'] is None
        assert c.post('/api/kr-insights/ranking',auth=AUTH,headers=HEAD,json={}).json()['cached']
        assert len(calls)==1
        assert c.post('/api/kr-insights/ranking',auth=AUTH,headers=HEAD,json={'market':'KOSDAQ'}).status_code==429
        with ki.LOCK:
            assert c.post('/api/kr-insights/ranking',auth=AUTH,headers=HEAD,json={}).status_code==409
        class After(Clock):
            @classmethod
            def now(cls,tz=None):return cls(2026,11,27,16,0,tzinfo=tz)
        monkeypatch.setattr(ki,'datetime',After)
        assert c.post('/api/kr-insights/ranking',auth=AUTH,headers=HEAD,json={}).status_code==409


def test_api_demo_auth_and_compare():
    with TestClient(app) as c:
        assert c.get('/kr-insights').status_code==401
        assert c.get('/kr-insights',auth=AUTH).status_code==200
        assert c.get('/static/kr-insights.js').status_code==200
        url='/api/kr-insights/ranking'
        assert c.post(url,json={}).status_code==401
        assert c.post(url,auth=AUTH,json={}).status_code==403
        assert c.post(url,auth=AUTH,headers={**HEAD,'Origin':'https://evil.example'},json={}).status_code==403
        assert c.post(url,auth=AUTH,headers=HEAD,json={'market':'bad'}).status_code==422
        r=c.post(url,auth=AUTH,headers=HEAD,json={'direction':'sell'}).json()
        assert r['demo'] and r['kind']=='intraday_estimate' and r['observation_date'] is None and all(x['foreign']<0 for x in r['items'])
        body={'code':'005930','name':'<test>','corp_code':''}
        try:
            assert c.put('/api/kr-market/watch',auth=AUTH,headers=HEAD,json=body).status_code==200
            r=c.post('/api/kr-insights/watch',auth=AUTH,headers=HEAD,json={'period':5,'sectors':{'005930':'반도체'}})
            assert r.status_code==200 and r.json()['demo'] and any(s['name']=='반도체' for s in r.json()['sectors'])
            assert c.post('/api/kr-insights/watch',auth=AUTH,headers=HEAD,json={'period':7}).status_code==422
            assert c.post('/api/kr-insights/watch',auth=AUTH,headers=HEAD,json={'sectors':{'bad':'x'}}).status_code==422
        finally:c.delete('/api/kr-market/watch/005930',auth=AUTH,headers=HEAD)
