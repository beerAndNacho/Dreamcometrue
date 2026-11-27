from datetime import date
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from app import kr_value as v, kr_market as km
from app.db import Session, MarketCache
from app.main import app

AUTH=('admin','test-admin-password-1234'); HEAD={'X-Macro-Admin':'1'}; CODE='005930'; CORP='00126380'; RECEIPT='20260301000001'

def fr(account,value,prev=None,div='IS',**kw):
    return {'corp_code':CORP,'bsns_year':'2025','reprt_code':'11011','currency':'KRW','rcept_no':RECEIPT,'sj_div':div,'account_id':account,'thstrm_amount':str(value),'frmtrm_amount':str(prev) if prev is not None else '',**kw}

def dr(label,value,kind='보통주',**kw):
    return {'corp_code':CORP,'rcept_no':RECEIPT,'se':label,'stock_knd':kind,'thstrm':str(value),'stlm_dt':'2025-12-31',**kw}

def financial_rows():
    return [fr('ifrs-full_Revenue',1000,800),fr('dart_OperatingIncomeLoss',100,50),fr('ifrs-full_ProfitLoss',90,60),fr('ifrs-full_ProfitLossAttributableToOwnersOfParent',80,40),fr('ifrs-full_BasicEarningsLossPerShare',10,5),fr('ifrs-full_EquityAttributableToOwnersOfParent',450,350,'BS'),fr('ifrs-full_Equity',600,400,'BS'),fr('ifrs-full_CashFlowsFromUsedInOperatingActivities',120,None,'CF'),fr('ifrs-full_PurchaseOfPropertyPlantAndEquipment',-30,None,'CF'),fr('ifrs-full_PurchaseOfIntangibleAssets',10,None,'CF')]

def test_annual_parent_roe_fcf_and_growth():
    r=v.parse_annual(financial_rows(),CORP,2025,'CFS')
    assert (r['roe'],r['fcf'],r['capex'],r['operating_margin'])==(20,80,40,10)
    assert r['growth']['revenue']==25 and r['growth']['eps']==100
    assert v.parse_annual(financial_rows(),CORP,2025,'OFS')['roe']==18

def test_annual_missing_conflicting_and_wrong_period():
    rows=financial_rows(); r=v.parse_annual(rows[:-1]+[fr('ifrs-full_Revenue',123,10),fr('ifrs-full_BasicEarningsLossPerShare',999,10,currency='USD'),fr('ifrs-full_Revenue',999,10,bsns_year='2024')],CORP,2025,'CFS')
    assert r['fcf'] is None and r['values']['revenue'] is None and r['operating_margin'] is None
    assert r['values']['eps']==10
    with pytest.raises(km.MarketError):v.parse_annual(rows+[fr('ifrs-full_Revenue',1000,800,rcept_no='20260302000001')],CORP,2025,'CFS')
    with pytest.raises(km.MarketError):v.parse_annual(rows,CORP,2024,'CFS')

def test_dividend_class_zero_and_missing():
    rows=[dr('주당 현금배당금(원)',0),dr('주당 현금배당금(원)',100,'우선주'),dr('현금배당수익률(%)','-'),dr('(연결)현금배당성향(%)',25,'-')]
    r=v.parse_dividend(rows,CORP,2025)
    assert r['dps']==0 and r['reported_yield'] is None and r['reported_payout']==25
    assert r['closing']=='2025-12-31'
    r=v.parse_dividend(rows+[dr('주당현금배당금(원)',50)],CORP,2025)
    assert r['dps'] is None
    with pytest.raises(km.MarketError):v.parse_dividend([dr('주당현금배당금(원)',50,'우선주')],CORP,2025)

def test_filing_filter_and_source():
    def handler(req):
        assert req.url.path=='/api/list.json' and req.url.params['corp_code']==CORP and req.url.params['end_de']=='20261127'
        return httpx.Response(200,json={'status':'000','total_page':1,'list':[{'corp_code':CORP,'rcept_no':RECEIPT,'rcept_dt':'20260301','report_nm':'현금ㆍ현물배당결정','rm':'정'},{'corp_code':CORP,'rcept_no':'20260302000001','rcept_dt':'20260302','report_nm':'사업보고서'},{'corp_code':'99999999','rcept_no':'20260303000001','rcept_dt':'20260303','report_nm':'배당'},{'corp_code':CORP,'rcept_no':'20261201000001','rcept_dt':'20261201','report_nm':'배당'}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r=v.filings(c,{'DART_API_KEY':'fake'},CORP,date(2026,11,27))
    assert r['complete'] and len(r['items'])==1 and r['items'][0]['remark']=='정'

def test_scenario_math_and_invalid_inputs():
    x=v.Scenario(price=100,eps=10,bps=80,dps=5,per=[5,10,15],pbr=[.5,1,2],operating_profit=100,invested_capital=500,tax=25,capital=100,dividend_yield=10,price_return=0,dividend_tax=0,years=2)
    r=v.scenario(x)
    assert (r['per'],r['pbr'],r['yield'],r['payout'],r['roic'])==(10,1.25,5,50,15)
    assert r['per_prices']==[50,100,150] and r['pbr_prices']==[40,80,160]
    assert r['points'][-1]=={'year':2,'reinvest':121,'cash_total':120}
    assert v.scenario(x.model_copy(update={'eps':-1}))['per_prices'] is None
    for update in ({'price':0},{'per':[10,5,15]},{'per':[0,1,2]},{'pbr':[1,2,float('nan')]},{'bps':0},{'years':31},{'price_return':-100}):
        with pytest.raises(ValueError):v.Scenario(**{**x.model_dump(),**update})

def test_routes_auth_demo_query_and_missing_watch(monkeypatch):
    monkeypatch.setattr(km,'watches',lambda:[{'code':CODE,'name':'테스트','corp_code':CORP}]);monkeypatch.setattr(v,'today',lambda:date(2026,11,27))
    with TestClient(app) as c:
        assert c.get('/kr-value').status_code==401 and c.get('/kr-value',auth=AUTH).status_code==200
        assert c.get('/api/kr-value?code='+CODE).status_code==401
        r=c.get('/api/kr-value?code='+CODE+'&years=10',auth=AUTH)
        assert r.status_code==200 and r.json()['demo'] and len(r.json()['history'])==10
        assert c.get('/api/kr-value?code='+CODE+'&years=7',auth=AUTH).status_code==422
        assert c.get('/api/kr-value?code=111111',auth=AUTH).status_code==404
        assert c.post('/api/kr-value/refresh',auth=AUTH,headers=HEAD,json={'code':CODE}).status_code==409
        data={'price':100,'eps':10,'per':[5,10,15],'pbr':[1,2,3]}
        assert c.post('/api/kr-value/scenario',auth=AUTH,json=data).status_code==403
        assert c.post('/api/kr-value/scenario',auth=AUTH,headers=HEAD,json=data).json()['per']==10

def test_schedule_validation_confirmation_and_concurrent_update(monkeypatch):
    monkeypatch.setattr(v,'DEMO',False);monkeypatch.setattr(v,'today',lambda:date(2026,11,27));monkeypatch.setattr(km,'watches',lambda:[{'code':CODE,'name':'테스트','corp_code':CORP}]);k=v.key('schedule',CODE,'COMMON')
    req={'code':CODE,'confirmed':True,'events':[{'receipt':RECEIPT,'record_date':'2026-12-31','ex_date':'2026-12-30','pay_date':'2027-04-01','dps':1000,'status':'확정'}]}
    with TestClient(app) as c:
        try:
            assert c.put('/api/kr-value/schedule',auth=AUTH,headers=HEAD,json={**req,'confirmed':False}).status_code==400
            assert c.put('/api/kr-value/schedule',auth=AUTH,headers=HEAD,json={**req,'events':[dict(req['events'][0],ex_date='2027-01-01')]}).status_code==422
            assert c.put('/api/kr-value/schedule',auth=AUTH,headers=HEAD,json={**req,'events':req['events']*2}).status_code==422
            assert c.put('/api/kr-value/schedule',auth=AUTH,headers=HEAD,json=req).status_code==200
            assert v.payload(k)['data']['items'][0]['dps']==1000
            assert c.put('/api/kr-value/schedule',auth=AUTH,headers=HEAD,json={**req,'events':[]}).status_code==409
            assert c.put('/api/kr-value/schedule',auth=AUTH,headers=HEAD,json={**req,'events':[],'expected_updated':v.payload(k)['updated']}).status_code==200
            assert v.payload(k)['data']['items']==[]
        finally:
            with Session.begin() as db:db.execute(delete(MarketCache).where(MarketCache.key==k))

def test_refresh_partial_preserves_cached_and_checks_identity(monkeypatch):
    monkeypatch.setattr(v,'DEMO',False);monkeypatch.setattr(v,'today',lambda:date(2026,11,27));monkeypatch.setattr(v,'LAST',{});monkeypatch.setattr(v,'load_settings',lambda:{'DART_API_KEY':'fake-secret'});monkeypatch.setattr(v,'available_years',lambda count:[2025]);monkeypatch.setattr(v,'filings',lambda *a:{'items':[],'complete':True});monkeypatch.setattr(km,'watches',lambda:[{'code':CODE,'name':'테스트','corp_code':CORP}]);
    def dart(c,cfg,endpoint,params):
        if endpoint=='company':return {'stock_code':CODE}
        assert params['bsns_year']=='2025' and params['reprt_code']=='11011'
        if endpoint=='fnlttSinglAcntAll':
            assert params['fs_div']=='CFS';return {'list':financial_rows()}
        raise RuntimeError('fake-secret must not leak')
    monkeypatch.setattr(km,'dart_get',dart)
    keys=[v.key('financial',CODE,'CFS',2025),v.key('dividend',CODE,'COMMON',2025),v.key('events',CODE,'COMMON')]
    with TestClient(app) as c:
        try:
            km.store(keys[1],[{'year':2025,'dps':1000}],merge=False)
            r=c.post('/api/kr-value/refresh',auth=AUTH,headers=HEAD,json={'code':CODE,'force':True})
            assert r.status_code==200 and [x['state'] for x in r.json()['results']]==['ok','error','ok']
            assert 'fake-secret' not in r.text and v.payload(keys[1])['data']['dps']==1000 and v.payload(keys[1])['state']=='error'
            assert c.post('/api/kr-value/refresh',auth=AUTH,headers=HEAD,json={'code':CODE}).status_code==429
            monkeypatch.setattr(v,'LAST',{});monkeypatch.setattr(km,'dart_get',lambda *a:{'stock_code':'111111'})
            assert c.post('/api/kr-value/refresh',auth=AUTH,headers=HEAD,json={'code':CODE}).status_code==400
        finally:
            with Session.begin() as db:db.execute(delete(MarketCache).where(MarketCache.key.in_(keys)))


def test_historical_multiple_csv_and_auth(monkeypatch):
    header='date,price,eps,bps\n'; raw=header+'2026-11-25,100,10,80\n2026-11-26,100,-5,\n'
    rows=v.parse_multiples(raw,date(2026,11,27))
    assert (rows[0]['per'],rows[0]['pbr'],rows[1]['per'])==(10,1.25,None)
    for bad in ['2026-11-27,100,10,80','2026-11-22,100,10,80','2026-11-26,0,10,80','2026-11-26,100,,','2026-11-26,100,inf,80','2026-11-26,100,10,-1','2026-11-26,100,10,80\n2026-11-26,100,10,80']:
        with pytest.raises(ValueError):v.parse_multiples(header+bad,date(2026,11,27))
    monkeypatch.setattr(v,'DEMO',False);monkeypatch.setattr(v,'today',lambda:date(2026,11,27));monkeypatch.setattr(km,'watches',lambda:[{'code':CODE,'name':'테스트','corp_code':CORP}]); k=v.key('multiples',CODE,'COMMON')
    data={'code':CODE,'csv_text':raw,'source_note':'confirmed snapshot','confirmed':True}
    with TestClient(app) as c:
        try:
            assert c.put('/api/kr-value/multiples',auth=AUTH,json=data).status_code==403
            assert c.put('/api/kr-value/multiples',auth=AUTH,headers=HEAD,json={**data,'confirmed':False}).status_code==400
            assert c.put('/api/kr-value/multiples',auth=AUTH,headers=HEAD,json=data).status_code==200
            assert v.payload(k)['data']['items']==rows
            assert c.put('/api/kr-value/multiples',auth=AUTH,headers=HEAD,json=data).status_code==409
            assert c.put('/api/kr-value/multiples',auth=AUTH,headers=HEAD,json={**data,'csv_text':header+'bad','expected_updated':v.payload(k)['updated']}).status_code==422
            assert v.payload(k)['data']['items']==rows
        finally:
            with Session.begin() as db:db.execute(delete(MarketCache).where(MarketCache.key==k))
