"""OpenDART diagnostics and regression tests; never send actual credentials."""
from datetime import date
import io, zipfile
import httpx, pytest
from fastapi.testclient import TestClient
from app import admin, kr_market as km, kr_value as kv
from app.main import app
from app.db import Session, MarketCache
from sqlalchemy import delete

AUTH=('admin','test-admin-password-1234')
HEAD={'X-Macro-Admin':'1'}
CFG={'DART_API_KEY':'secret-mock-not-real'}
CORP='00126380'; TICKER='005930'; YEAR=2024

@pytest.mark.parametrize('status',list(km.DART_STATUS))
def test_status_code_and_redaction(status):
    def handler(req):
        assert req.url.params['crtfc_key']==CFG['DART_API_KEY']
        return httpx.Response(200,json={'status':status,'message':CFG['DART_API_KEY']})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(km.DartError) as err:
            km.dart_get(c,CFG,'company',{'corp_code':CORP})
        assert err.value.code==status and f'[{status}]' in str(err.value)
        assert CFG['DART_API_KEY'] not in str(err.value)
        assert err.value.fatal==(status in km.DART_FATAL)

@pytest.mark.parametrize('body,expected',[(b'<result><status>012</status><message>secret-mock-not-real</message></result>','012'),(b'{"status":"020","message":"secret-mock-not-real"}','020'),(b'<html>error</html>','format'),(b'{"status":[]}','format')])
def test_corp_zip_error(body,expected):
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=body))) as c:
        with pytest.raises(km.DartError) as err:
            km.resolve_corp(c,CFG,TICKER,force=True)
        assert err.value.code==expected and CFG['DART_API_KEY'] not in str(err.value)

@pytest.mark.parametrize('kind,expected',[('http','http'),('network','network'),('json','format'),('unknown','unknown')])
def test_transport_errors(kind,expected):
    def handler(req):
        if kind=='network':raise httpx.ConnectTimeout('secret-mock-not-real',request=req)
        if kind=='http':return httpx.Response(429,content=b'secret-mock-not-real')
        if kind=='json':return httpx.Response(200,content=b'not json')
        return httpx.Response(200,json={'status':CFG['DART_API_KEY']})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(km.DartError) as err:km.dart_get(c,CFG,'company',{})
        assert err.value.code==expected and CFG['DART_API_KEY'] not in str(err.value)


def test_dart_admin_connection_steps(monkeypatch):
    monkeypatch.setattr(admin,'DEMO',False);monkeypatch.setattr(admin,'today',lambda:date(2026,11,27));monkeypatch.setattr(admin,'load_settings',lambda:CFG)
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:z.writestr('CORPCODE.xml',f'<result><list><corp_code>{CORP}</corp_code><stock_code>{TICKER}</stock_code></list></result>')
    calls=[]; error=[None]; cls=httpx.Client
    def handler(req):
        endpoint=req.url.path.rsplit('/',1)[-1]; calls.append(endpoint)
        if error[0]:return httpx.Response(200,json={'status':error[0],'message':CFG['DART_API_KEY']})
        if endpoint=='company.json':return httpx.Response(200,json={'status':'000','stock_code':TICKER})
        if endpoint=='corpCode.xml':return httpx.Response(200,content=buf.getvalue())
        assert req.url.params['bsns_year']==str(YEAR) and req.url.params['reprt_code']=='11011'
        if endpoint=='fnlttSinglAcntAll.json':return httpx.Response(200,json={'status':'000','list':[{'corp_code':CORP,'bsns_year':str(YEAR),'reprt_code':'11011','currency':'KRW','sj_div':'IS','account_id':'ifrs-full_Revenue','thstrm_amount':'1000','rcept_no':'20250301000001'}]})
        assert endpoint=='alotMatter.json'
        return httpx.Response(200,json={'status':'000','list':[{'corp_code':CORP,'rcept_no':'20250301000001','se':'주당현금배당금(원)','stock_knd':'보통주','thstrm':'20'}]})
    monkeypatch.setattr(admin.httpx,'Client',lambda **kw:cls(transport=httpx.MockTransport(handler)))
    try:
        with TestClient(app) as c:
            assert c.post('/api/admin/test/DART').status_code==401
            assert c.post('/api/admin/test/DART',auth=AUTH).status_code==403
            r=c.post('/api/admin/test/DART',auth=AUTH,headers=HEAD).json();assert r['ok'] and len(r['results'])==4
            assert 'corpCode.xml' in calls and 'alotMatter.json' in calls
            calls.clear();error[0]='012'
            r=c.post('/api/admin/test/DART',auth=AUTH,headers=HEAD);assert not r.json()['ok'] and len(calls)==1 and '[012]' in r.text and CFG['DART_API_KEY'] not in r.text
    finally:km.CORPS.clear()


def test_no_filings_is_empty_and_fatal_stops_batch(monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'status':'013'}))) as c:
        assert kv.filings(c,CFG,CORP,date(2026,11,27))=={'items':[],'complete':True}
    monkeypatch.setattr(kv,'DEMO',False);monkeypatch.setattr(kv,'LAST',{});monkeypatch.setattr(kv,'load_settings',lambda:CFG);monkeypatch.setattr(kv,'available_years',lambda n:[YEAR]);monkeypatch.setattr(km,'watches',lambda:[{'code':TICKER,'corp_code':CORP,'name':'test'}]);monkeypatch.setattr(kv.time,'sleep',lambda _:None)
    calls=[]
    def dart(c,cfg,endpoint,params):
        calls.append(endpoint)
        if endpoint=='company':return {'stock_code':TICKER}
        raise km.DartError('020',endpoint)
    monkeypatch.setattr(km,'dart_get',dart)
    k=kv.key('financial',TICKER,'CFS',YEAR)
    with TestClient(app) as c:
        try:
            km.store(k,[{'year':YEAR,'saved':True}],merge=False)
            r=c.post('/api/kr-value/refresh',auth=AUTH,headers=HEAD,json={'code':TICKER,'force':True}).json()
            assert [x['state'] for x in r['results']]==['error','skipped','skipped'] and calls==['company','fnlttSinglAcntAll']
            assert kv.payload(k)['data']['saved'] and '[020]' in kv.payload(k)['message']
        finally:
            with Session.begin() as db:db.execute(delete(MarketCache).where(MarketCache.key==k))


def test_admin_controls_and_default_report_year():
    with TestClient(app) as c:
        page=c.get('/admin',auth=AUTH).text
        assert 'data-test="DART"' in page and 'test-DART' in page
        js=c.get('/static/kr-market.js').text
        assert "String(Number(data.asof.slice(0,4))-1)" in js
        assert c.post('/api/admin/test/DART',auth=AUTH,headers=HEAD).status_code==409
