from datetime import date,timedelta
import httpx
import pytest
from fastapi.testclient import TestClient
from app import global_market as g
from app import kr_market as km
from app.main import app
AUTH=('admin','test-admin-password-1234'); HEAD={'X-Macro-Admin':'1'}

def test_technical_known_series():
    rows=[{'close':float(i),'high':i+1.,'low':i-1.,'volume':100} for i in range(1,301)]
    m=g.technical(rows)
    assert m['ma']['200']==200.5 and m['ma']['20']==290.5 and m['rsi']==100
    assert m['macd']==pytest.approx(7) and m['signal']==pytest.approx(7)
    assert m['atr']==2 and m['volume_ratio']==1 and m['high252']==300 and m['low252']==49
    assert g.rsi([10]*30)==50 and g.rsi(list(range(30,0,-1)))==0
    assert g.technical(rows[:10])['ma200_gap'] is None
    assert g.diagnostics(m,'자료 없음',False)['state']=='판단 보류'
    assert g.diagnostics(m,'분석 가능',True)['state']=='판단 보류'

def row(d='20261125',**kw):return {'xymd':d,'open':'10','high':'12','low':'9','clos':'11','tvol':'100',**kw}

def test_parser_rejects_invalid_and_conflicts():
    end=date(2026,11,25)
    assert len(g.parse_prices([row(),row('20261126'),row('20261122')],end))==1
    for raw in [[row(clos='NaN')],[row(high='8')],[row(tvol='-1')],[row(),row(clos='10')],[]]:
        with pytest.raises(km.MarketError):g.parse_prices(raw,end)

def test_fetch_request_and_page(monkeypatch):
    monkeypatch.setattr(km,'kis_token',lambda *a:'mock-token');monkeypatch.setattr(g.time,'sleep',lambda x:None)
    calls=[]
    def handler(req):
        assert req.url.path.endswith('/dailyprice') and req.headers['tr_id']=='HHDFS76240000'
        assert req.url.params['MODP']=='1' and req.url.params['SYMB']=='QQQ'
        d=req.url.params['BYMD'];calls.append(d)
        return httpx.Response(200,json={'rt_cd':'0','output2':[row(d)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r=g.fetch_prices(c,{'KIS_APP_KEY':'fake','KIS_APP_SECRET':'fake'},'QQQ',date(2026,11,25),2)
    assert calls==['20261125','20261124'] and len(r)==2

def test_quality_and_demo_routes():
    end=date(2026,11,25); rows=g.demo_rows('QQQ',end)
    assert g.quality(rows,end,'demo')=='분석 가능'
    assert g.quality(rows,end+timedelta(days=10),'ok')!='분석 가능'
    assert g.quality(rows,end,'error')!='분석 가능'
    assert g.quality(rows[:10],end,'ok')!='분석 가능'
    with TestClient(app) as c:
        assert c.get('/global-market').status_code==401
        assert c.get('/global-market',auth=AUTH).status_code==200
        r=c.get('/api/global-market',auth=AUTH).json()
        assert r['demo'] and len(r['assets'])==37 and len(r['indices'])==4
        d=c.get('/api/global-market/QQQ',auth=AUTH).json()
        assert len(d['rows'])==600 and d['metrics']['ma']['200']>0
        assert c.get('/api/global-market/unknown',auth=AUTH).status_code==404
        assert c.post('/api/global-market/refresh',auth=AUTH,headers=HEAD,json={'symbols':['QQQ']}).status_code==409
        assert c.post('/api/global-market/refresh',auth=AUTH,json={'symbols':['QQQ']}).status_code==403

def test_live_error_preserves_rows(monkeypatch):
    monkeypatch.setattr(g,'DEMO',False);monkeypatch.setattr(g,'LAST',0);monkeypatch.setattr(g,'load_settings',lambda:{'KIS_APP_KEY':'fake','KIS_APP_SECRET':'fake'})
    monkeypatch.setattr(g,'fetch_prices',lambda *a: (_ for _ in ()).throw(RuntimeError('fake secret')))
    old=g.demo_rows('QQQ',g.cutoff());km.store('global:adjusted:QQQ',old,merge=False)
    with TestClient(app) as c:
        r=c.post('/api/global-market/refresh',auth=AUTH,headers=HEAD,json={'symbols':['QQQ']})
        assert r.status_code==200 and 'fake' not in r.text and r.json()['results'][0]['state']=='error'
        assert km.cached('global:adjusted:QQQ')['rows']==old
        assert g.state('QQQ')['diagnostics']['state']=='판단 보류'
