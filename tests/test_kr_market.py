from datetime import date,timedelta
import io,zipfile
import httpx,pytest
from fastapi.testclient import TestClient
from app import kr_market as m
from app.main import app
from app.db import Session,MarketCache,MarketWatch
from app.settings import save_settings,public_settings,directory
from sqlalchemy import delete

AUTH=('admin','test-admin-password-1234'); HEAD={'X-Macro-Admin':'1'}
CFG={'KIS_APP_KEY':'key-test','KIS_APP_SECRET':'secret/test+','DART_API_KEY':'dart-test'}
DAY=date(2026,11,26)

def raw(d='20261126',foreign='12345',institution='-4500',individual='-7845'):
    return {'stck_bsop_date':d,'frgn_ntby_tr_pbmn':foreign,'orgn_ntby_tr_pbmn':institution,'prsn_ntby_tr_pbmn':individual,'bstp_nmix_prpr':'4711.21','bstp_nmix_prdy_ctrt':'0.29','stck_clpr':'70000','fund_ntby_tr_pbmn':'123'}

@pytest.fixture(autouse=True)
def reset(monkeypatch,tmp_path):
    monkeypatch.setenv('SETTINGS_DIR',str(tmp_path));m.TOKEN.clear();m.CORPS.clear()
    monkeypatch.setattr(m.time,'sleep',lambda x:None)
    yield
    m.TOKEN.clear();m.CORPS.clear()

def test_flow_units_null_and_duplicates():
    a=m.clean_rows([raw('20261127'),raw(),raw('20261125',foreign='NaN'),raw('20261124',foreign='0'),raw('20261122')],DAY,m.parse_flow)
    assert [x['date'] for x in a]==['2026-11-24','2026-11-26']
    assert a[-1]['foreign']==123.45 and a[-1]['institution']==-45 and a[-1]['fund']==1.23
    assert a[0]['foreign']==0 and a[-1]['other'] is None
    with pytest.raises(m.MarketError):m.clean_rows([raw(),raw(foreign='100')],DAY,m.parse_flow)
    with pytest.raises(m.MarketError):m.clean_rows([raw(foreign='')],DAY,m.parse_flow)

def test_token_reuse_market_codes_and_cutoff():
    calls=[]
    def handler(req):
        calls.append(req)
        if req.url.path=='/oauth2/tokenP':return httpx.Response(200,json={'access_token':'fake-token','expires_in':86400})
        assert req.headers['authorization']=='Bearer fake-token';assert req.headers['tr_id']=='FHPTJ04040000'
        assert req.url.params['FID_INPUT_DATE_1']=='20261126'
        assert req.url.params['FID_INPUT_ISCD']==req.url.params['FID_INPUT_ISCD_2']
        assert (req.url.params['FID_INPUT_ISCD'],req.url.params['FID_INPUT_ISCD_1']) in [('0001','KSP'),('1001','KSQ')]
        return httpx.Response(200,json={'rt_cd':'0','output':[raw(),raw('20261127')]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        for name in m.MARKETS: assert len(m.fetch_flow(c,CFG,name,DAY,target=1))==1
    assert len(calls)==3

def test_market_backfill_and_invalid_index():
    def handler(req):
        if req.url.path=='/oauth2/tokenP':return httpx.Response(200,json={'access_token':'token','expires_in':86400})
        d=req.url.params['FID_INPUT_DATE_1']; x=raw(d); y=raw('20261127');y['bstp_nmix_prpr']='0'
        return httpx.Response(200,json={'rt_cd':'0','output':[x,y]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        rows=m.fetch_flow(c,CFG,'KOSPI',DAY,target=3)
    assert [r['date'] for r in rows]==['2026-11-24','2026-11-25','2026-11-26']

def test_price_and_stock_flow():
    def handler(req):
        if req.url.path=='/oauth2/tokenP':return httpx.Response(200,json={'access_token':'token','expires_in':86400})
        assert req.url.params['FID_COND_MRKT_DIV_CODE']=='J'
        assert req.url.params['FID_INPUT_ISCD']=='005930'
        if req.headers['tr_id']=='FHKST03010100':
            assert req.url.params['FID_ORG_ADJ_PRC']=='1'
            return httpx.Response(200,json={'rt_cd':'0','output1':{'stck_shrn_iscd':'005930'},'output2':[raw(),raw('20261127')]})
        assert req.headers['tr_id']=='FHPTJ04160001'
        return httpx.Response(200,json={'rt_cd':'0','output2':[raw()]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert m.fetch_price(c,CFG,'005930',DAY)[0]['close']==70000
        assert m.fetch_stock_flow(c,CFG,'005930',DAY)[0]['foreign']==123.45

def test_token_failure_redacted_and_throttled():
    def handler(req):return httpx.Response(200,json={'msg1':'secret/test+'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(m.MarketError,match='토큰 발급 실패'):m.kis_token(c,CFG)
        with pytest.raises(m.MarketError,match='60초'):m.kis_token(c,CFG)

def test_summary_counts_gaps_and_zero():
    rows=[]
    for d in (date(2026,11,19),date(2026,11,20),date(2026,11,23),date(2026,11,24),date(2026,11,25)):
        rows.append({'date':str(d),'foreign':5,'institution':-2,'individual':-3})
    s=m.summarize(rows);assert s['totals']['5']['foreign']==25 and s['totals']['20']['foreign'] is None and s['foreign_streak']==5
    rows[-1]['date']='2026-12-20';s=m.summarize(rows);assert s['totals']['5']['foreign'] is None and s['foreign_streak']==1
    rows[-1]['foreign']=0;assert m.summarize(rows)['foreign_streak']==0

def test_cache_merge_revisions_and_failure_retention():
    with TestClient(app):
        key='test:cache';a={'date':'2026-11-25','foreign':1};b={'date':'2026-11-26','foreign':2}
        m.store(key,[a,b]);m.store(key,[{**b,'foreign':4}]);m.store(key,error='failure')
        r=m.cached(key);assert r['state']=='error' and r['rows']==[a,{**b,'foreign':4}] and r['updated']
        with Session.begin() as db:db.execute(delete(MarketCache).where(MarketCache.key==key))

def test_dart_auto_corp_and_financial_period():
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:z.writestr('CORPCODE.xml','<result><list><corp_code>00126380</corp_code><stock_code>005930</stock_code></list></result>')
    def handler(req):
        if req.url.path.endswith('corpCode.xml'):return httpx.Response(200,content=buf.getvalue())
        if req.url.path.endswith('company.json'):return httpx.Response(200,json={'status':'000','stock_code':'005930','stock_name':'Samsung'})
        assert req.url.params['reprt_code']=='11014' and req.url.params['fs_div']=='CFS'
        base={'sj_div':'IS','currency':'KRW','corp_code':'00126380','bsns_year':'2026','reprt_code':'11014','rcept_no':'20261114000001','thstrm_add_amount':'99999999999'}
        rows=[{**base,'account_id':k,'thstrm_amount':v,'frmtrm_q_amount':p} for k,v,p in [('ifrs-full_Revenue','10000000000','8000000000'),('dart_OperatingIncomeLoss','2000000000','1000000000'),('ifrs-full_BasicEarningsLossPerShare','1000','800')]]
        return httpx.Response(200,json={'status':'000','list':rows})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r=m.fetch_financials(c,CFG,'005930','',2026,'11014','CFS')
        assert r['corp_code']=='00126380' and r['values']['revenue']==1e10 and r['values']['eps']==1000
        assert r['values']['net_income'] is None and r['yoy']['revenue']==25 and r['operating_margin']==20
        with pytest.raises(m.MarketError,match='불일치'):m.fetch_financials(c,CFG,'000660','00126380',2026,'11014','CFS')

def test_new_key_encryption():
    save_settings(CFG);p=public_settings();assert all(p['configured'][k] for k in CFG)
    assert all(v not in str(p) for v in CFG.values());assert all(v not in (directory()/'settings.enc').read_text() for v in CFG.values())

def test_http_demo_watch_and_report():
    with TestClient(app) as c:
        r=c.get('/api/kr-market').json();assert r['demo'] and all(len(x['rows'])==75 for x in r['markets'].values());assert c.get('/kr-market').status_code==200
        assert c.get('/static/kr-market.js').status_code==200
        assert c.get('/api/kr-market/watch').status_code==401
        body={'code':'005930','name':'<test>','corp_code':'00126380'}
        assert c.put('/api/kr-market/watch',auth=AUTH,json=body).status_code==403
        assert c.put('/api/kr-market/watch',auth=AUTH,headers={**HEAD,'Origin':'https://evil.example'},json=body).status_code==403
        assert c.put('/api/kr-market/watch',auth=AUTH,headers=HEAD,json={**body,'code':'123'}).status_code==422
        assert c.put('/api/kr-market/watch',auth=AUTH,headers=HEAD,json=body).status_code==200
        assert any(x['code']=='005930' and x['price']['state']=='demo' and len(x['price']['rows'])==75 for x in c.get('/api/kr-market/watch',auth=AUTH).json()['items'])
        assert c.delete('/api/kr-market/watch/005930',auth=AUTH,headers=HEAD).status_code==200
        assert c.post('/api/kr-market/refresh',auth=AUTH,headers=HEAD).status_code==409
        assert c.post('/api/kr-market/financials',auth=AUTH,headers=HEAD,json={'code':'005930','year':2026,'report':'11014'}).status_code==409
        assert c.get('/api/kr-market/export/KOSPI',auth=AUTH).headers['content-disposition'].endswith('KOSPI-demo-flow-eok.csv')
        assert '국내 증시 수급' in c.get('/api/report').text
