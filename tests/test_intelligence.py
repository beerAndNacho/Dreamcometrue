import json
from datetime import date, datetime, timedelta, timezone
import pytest, httpx
from fastapi.testclient import TestClient
from sqlalchemy import delete
from app import intelligence as a, release_calendar as cal, kr_market as km
from app.db import Session, MarketCache
from app.main import app
from app.settings import load_settings, save_settings, public_settings
AUTH=('admin','test-admin-password-1234');HEAD={'X-Macro-Admin':'1'}

@pytest.fixture
def clean():
    old=load_settings(); keys=['AI_PROVIDER','OPENAI_API_KEY','OPENAI_MODEL','GEMINI_API_KEY','GEMINI_MODEL','ANTHROPIC_API_KEY','ANTHROPIC_MODEL','AI_DAILY_LIMIT','AI_MAX_OUTPUT','BLS_AUTO_REFRESH','AI_REPORT_AUTO_SAVE','AI_REPORT_AUTO_RUN']
    with Session.begin() as db:
        db.execute(delete(a.InsightReport));db.execute(delete(cal.CalendarEntry));db.execute(delete(MarketCache).where(MarketCache.key==cal.CACHE))
    yield
    save_settings({k:old[k] for k in keys})
    with Session.begin() as db:
        db.execute(delete(a.InsightReport));db.execute(delete(cal.CalendarEntry));db.execute(delete(MarketCache).where(MarketCache.key==cal.CACHE))

ICS='\r\n'.join(['BEGIN:VCALENDAR','VERSION:2.0','BEGIN:VEVENT','UID:release-summer','DTSTART;TZID=America/New_York:20260710T083000','SUMMARY:Consumer Price Index for June','  2026','END:VEVENT','BEGIN:VEVENT','UID:release-winter','DTSTART:20261210T083000','SUMMARY:Employment Situation','END:VEVENT','BEGIN:VEVENT','UID:holiday','DTSTART;VALUE=DATE:20261225','SUMMARY:Holiday','END:VEVENT','BEGIN:VEVENT','UID:cancel','DTSTART:20261201T100000','STATUS:CANCELLED','SUMMARY:Cancelled','END:VEVENT','END:VCALENDAR'])

def test_bls_dst_folding_and_cancel():
    items=cal.parse_bls(ICS);assert len(items)==2 and '2026' in items[0]['title']
    assert items[0]['when']=='2026-07-10T12:30:00+00:00';assert items[1]['when']=='2026-12-10T13:30:00+00:00'
    assert cal.local_instant(datetime(2026,7,10,8,30),'America/New_York').astimezone(cal.KST).hour==21
    assert cal.local_instant(datetime(2026,12,10,8,30),'America/New_York').astimezone(cal.KST).hour==22
    for dt in [datetime(2026,3,8,2,30),datetime(2026,11,1,1,30)]:
        with pytest.raises(ValueError):cal.local_instant(dt,'America/New_York')

def test_bls_bad_feed():
    for bad in ['<html>error</html>',ICS.replace('END:VCALENDAR',''),ICS.replace('UID:release-summer','RRULE:FREQ=DAILY\r\nUID:release-summer'),ICS.replace('America/New_York','Somewhere/Else')]:
        with pytest.raises(ValueError):cal.parse_bls(bad)
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=ICS))) as c:
        assert len(cal.fetch_bls(c))==2

def test_calendar_manual_dates_results_and_security(clean,monkeypatch):
    with TestClient(app) as c:
        assert c.get('/api/calendar').status_code==401
        assert c.get('/api/calendar',auth=AUTH).json()['demo']
        assert c.post('/api/calendar/bls',auth=AUTH,headers=HEAD).status_code==409
        monkeypatch.setattr(cal,'DEMO',False)
        d=cal.today()-timedelta(days=1)
        req={'title':'PCE 테스트','agency':'BEA','day':str(d),'time':'08:30','zone':'America/New_York','source':'https://www.bea.gov/news/schedule','state':'발표','unit':'% YoY','actual':3.0,'previous':2.8,'expected':2.9,'result_source':'https://www.bea.gov/news','confirm':True}
        assert c.put('/api/calendar/event',auth=AUTH,json=req).status_code==403
        for mod in [{'source':'https://bea.gov.evil.test/'},{'confirm':False},{'unit':''},{'day':str(cal.today()+timedelta(days=2))},{'result_source':'javascript:alert(1)'}]:
            assert c.put('/api/calendar/event',auth=AUTH,headers=HEAD,json={**req,**mod}).status_code==422
        r=c.put('/api/calendar/event',auth=AUTH,headers=HEAD,json=req);assert r.status_code==200,r.text;eid=r.json()['id']
        item=c.get('/api/calendar',auth=AUTH).json()['events'][0];assert item['origin']=='사용자 확인' and item['surprise']==pytest.approx(.1) and item['kst'].endswith('+09:00')
        assert c.put('/api/calendar/event',auth=AUTH,headers=HEAD,json={**req,'id':eid,'expected':2.7}).status_code==200
        assert c.delete('/api/calendar/event/'+eid,auth=AUTH,headers=HEAD).status_code==200

def test_calendar_failed_refresh_preserves_cache(clean,monkeypatch):
    with TestClient(app) as c:
        monkeypatch.setattr(cal,'DEMO',False)
        monkeypatch.setattr(cal,'fetch_bls',lambda _:cal.parse_bls(ICS))
        assert c.post('/api/calendar/bls',auth=AUTH,headers=HEAD).status_code==200
        assert c.post('/api/calendar/bls',auth=AUTH,headers=HEAD).status_code==429
        with Session.begin() as db:db.get(MarketCache,cal.CACHE).attempted-=timedelta(minutes=2)
        def fail(_):raise httpx.ConnectError('private details')
        monkeypatch.setattr(cal,'fetch_bls',fail)
        r=c.post('/api/calendar/bls',auth=AUTH,headers=HEAD);assert r.status_code==502 and 'private details' not in r.text
        assert len(km.cached(cal.CACHE)['rows'])==2 and km.cached(cal.CACHE)['state']=='error'

def good_sections(records):
    refs=[x['id'] for x in records]
    return json.dumps({'sections':[{'kind':'watch','text':'변동성과 신용 위험을 함께 확인해야 합니다.','refs':refs[:1]},{'kind':'up','text':'금융 여건이 완화되는지 확인합니다.','refs':refs[1:2]}]},ensure_ascii=False)

def test_provider_payload_and_parsers():
    d={'period':'daily','evidence':[a.evidence_entry('vix','VIX',25,'pt','2026-11-25','https://fred.stlouisfed.org/'),a.evidence_entry('hy','OAS',3.5,'%p','2026-11-25','https://fred.stlouisfed.org/')],'private':'never transmit'}
    raw=good_sections(d['evidence'])
    for provider in a.PROVIDERS:
        url,h,body=a.provider_payload(provider,'test-model','test-secret',1200,d)
        assert 'test-secret' not in url and 'test-secret' not in json.dumps(body) and 'never transmit' not in json.dumps(body) and 'test-secret' in str(h)
        assert url.startswith('https://') and 'tools' not in body
    assert a.provider_payload('openai','test-model','key',256,d)[2]['store'] is False
    assert a.parse_response('openai',{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':raw}]}],'usage':{'input_tokens':30,'output_tokens':40}})==(raw,30,40)
    assert a.parse_response('gemini',{'candidates':[{'finishReason':'STOP','content':{'parts':[{'thought':True,'text':'ignore'},{'text':raw}]}}],'usageMetadata':{'promptTokenCount':50,'totalTokenCount':70}})==(raw,50,20)
    assert a.parse_response('anthropic',{'stop_reason':'end_turn','content':[{'type':'text','text':raw}],'usage':{'input_tokens':30,'output_tokens':40,'cache_read_input_tokens':12}})==(raw,42,40)
    assert a.parse_response('openai',{'status':'incomplete','usage':{'input_tokens':10,'output_tokens':20}})==('',10,20)
    with pytest.raises(ValueError):a.provider_payload('gemini','../../bad','key',256,d)

def test_grounding_validation():
    e=[{'id':'x'},{'id':'y'}];good=good_sections(e);assert len(a.validate_sections(good,e))==2
    for raw in [good.replace('"x"','"z"'),good.replace('변동성과','100원'),good.replace('변동성과','<script>'),good.replace('변동성과','https://bad.test'),good.replace('"up"','"watch"'),'not JSON']:
        with pytest.raises(ValueError):a.validate_sections(raw,e)

def test_ai_settings_and_no_key_return(clean):
    save_settings({'OPENAI_API_KEY':'fake-test-key','OPENAI_MODEL':'fake-model','AI_PROVIDER':'openai','AI_DAILY_LIMIT':'2'})
    p=public_settings();assert p['configured']['OPENAI_API_KEY'] and 'fake-test-key' not in json.dumps(p)
    for k,v in [('AI_DAILY_LIMIT','0'),('AI_MAX_OUTPUT','10000'),('AI_PROVIDER','custom'),('GEMINI_MODEL','https://evil.test'),('BLS_AUTO_REFRESH','bad')]:
        with pytest.raises(ValueError):save_settings({k:v})
    with TestClient(app) as c:
        assert c.get('/api/intelligence').status_code==401
        assert c.post('/api/intelligence/generate',auth=AUTH,json={}).status_code==403
        assert c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={'ai':True,'consent':True}).status_code==409

def test_rule_report_reuse_history_weekly_and_busy(clean):
    with TestClient(app) as c:
        p=c.get('/api/intelligence?period=weekly',auth=AUTH).json();assert p['report']['demo'] and p['report']['period']=='weekly' and len(p['report']['evidence'])>=38
        r=c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={'period':'weekly'}).json();assert r['state']=='ok' and not r['reused']
        r2=c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={'period':'weekly'}).json();assert r2['id']==r['id'] and r2['reused']
        assert c.get('/api/intelligence/history/'+str(r['id']),auth=AUTH).json()['report']==r['report']
        assert '가상 자료' in c.get(f'/api/intelligence/history/{r["id"]}/txt',auth=AUTH).text
        assert c.get('/api/intelligence/history',auth=AUTH).json()['config']['attempts']==0
        a.LOCK.acquire()
        try:
            b=c.get('/api/intelligence',auth=AUTH).json()['report'];assert b['busy'] and not b['evidence']
            assert c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={}).status_code==409
        finally:a.LOCK.release()

def test_ai_failed_attempt_quota_and_reuse(clean,monkeypatch):
    save_settings({'AI_PROVIDER':'openai','OPENAI_API_KEY':'fake-key','OPENAI_MODEL':'fake-model','AI_DAILY_LIMIT':'2'})
    monkeypatch.setattr(a,'DEMO',False)
    original=a.facts
    monkeypatch.setattr(a,'facts',lambda *args,**kw:{**original(*args,**kw),'demo':False})
    calls=[]
    def invoke(provider,model,key,limit,data):
        calls.append(provider)
        return (good_sections(data['evidence']) if len(calls)==1 else '{bad json',12,20)
    monkeypatch.setattr(a,'invoke',invoke)
    with TestClient(app) as c:
        assert c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={'ai':True}).status_code==400
        req={'ai':True,'consent':True};r=c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json=req).json();assert r['state']=='ok' and r['input_tokens']==12 and len(r['report']['sections'])==2
        assert c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json=req).json()['reused'] and len(calls)==1
        r=c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={**req,'period':'weekly'}).json();assert r['state']=='error' and not r['report']['sections'] and 'ai_error' in r['report']
        assert c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={**req,'period':'weekly'}).status_code==429
        cfg=c.get('/api/intelligence/history',auth=AUTH).json()['config'];assert cfg['attempts']==2 and cfg['input_tokens']==24 and cfg['output_tokens']==40
        assert c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={}).json()['provider']=='rules'

def test_stock_public_evidence_only(clean):
    with TestClient(app) as c:
        c.put('/api/kr-market/watch',auth=AUTH,headers=HEAD,json={'code':'000270','name':'private memo 999'})
        d=c.get('/api/intelligence?code=000270',auth=AUTH).json()['report'];assert any(x['id']=='stock_close' for x in d['evidence'])
        _,_,body=a.provider_payload('openai','text-model','key',1000,d);assert 'private memo' not in json.dumps(body,ensure_ascii=False)
        c.delete('/api/kr-market/watch/000270',auth=AUTH,headers=HEAD)


def test_external_prompt_no_ai_and_historical(clean, monkeypatch):
    save_settings({'AI_PROVIDER':'off','OPENAI_API_KEY':'','GEMINI_API_KEY':'','ANTHROPIC_API_KEY':''})
    monkeypatch.setattr(a,'invoke',lambda *args:pytest.fail('API must not be called'))
    with TestClient(app) as c:
        p=c.get('/api/intelligence?period=weekly',auth=AUTH).json()
        assert p['prompt'] and '가상 자료' in p['prompt']
        data=json.loads(p['prompt'].split('=== 관측자료 JSON ===\n',1)[1])
        assert data['period']=='weekly' and data['demo']
        assert all({'id','value','period','unit','source'}<=e.keys() for e in data['evidence'])
        assert any(e['id']=='KOSPI_foreign_20' for e in data['evidence'])
        r=c.post('/api/intelligence/generate',auth=AUTH,headers=HEAD,json={'period':'weekly'}).json()
        old=r['prompt']
        orig=a.facts
        def changed(*args,**kwargs):
            d=orig(*args,**kwargs)
            if d['evidence']:d['evidence'][0]['value']=123456789
            return d
        monkeypatch.setattr(a,'facts',changed)
        assert c.get('/api/intelligence?period=weekly',auth=AUTH).json()['prompt']!=old
        assert c.get('/api/intelligence/history/'+str(r['id']),auth=AUTH).json()['prompt']==old
        assert c.get('/api/intelligence/history',auth=AUTH).json()['config']['attempts']==0
        a.LOCK.acquire()
        try: assert c.get('/api/intelligence',auth=AUTH).json()['prompt'] is None
        finally: a.LOCK.release()


def test_external_prompt_allowlist():
    d={'asof':'2026-11-27','period':'daily','demo':False,'busy':False,
       'evidence':[{'id':'x','name':'VIX','value':25,'unit':'pt','period':'2026-11-26','source':'https://fred.stlouisfed.org/',
                    'change':{'previous':20,'period':'2026-11-25','kind':'관측일 갱신','private':'SECRET'}}],
       'model':{'score':30,'private':'SECRET','checks':[{'label':'VIX','value':25,'points':15,'rule':'20','private':'SECRET'}]},
       'calendar':[{'title':'SECRET'}],'portfolio':'SECRET','settings':'SECRET','notes':[{'text':'SECRET'}]}
    prompt=a.external_prompt(d)
    assert 'SECRET' not in prompt and '2026-11-26' in prompt and 'https://fred.stlouisfed.org/' in prompt
    assert a.external_prompt({**d,'busy':True}) is None
    assert a.external_prompt({**d,'evidence':[]}) is None


def test_auto_archive_opt_in_and_no_retry(clean,monkeypatch):
    with TestClient(app) as c:
        monkeypatch.setattr(a,'DEMO',False)
        original=a.facts
        monkeypatch.setattr(a,'facts',lambda *args,**kw:{**original(*args,**kw),'demo':False})
        save_settings({'AI_REPORT_AUTO_SAVE':'0','AI_REPORT_AUTO_RUN':'0','AI_PROVIDER':'openai','OPENAI_API_KEY':'fake-key','OPENAI_MODEL':'fake-model'})
        calls=[]
        def fail(*args):
            calls.append(1)
            return '{bad JSON',12,20
        monkeypatch.setattr(a,'invoke',fail)
        a.archive_after_collect()
        assert not c.get('/api/intelligence/history',auth=AUTH).json()['items']
        save_settings({'AI_REPORT_AUTO_SAVE':'1'})
        a.archive_after_collect()
        h=c.get('/api/intelligence/history',auth=AUTH).json()
        assert len(h['items'])==1 and h['items'][0]['provider']=='rules' and not calls
        save_settings({'AI_REPORT_AUTO_RUN':'1'})
        a.archive_after_collect()
        a.archive_after_collect()
        h=c.get('/api/intelligence/history',auth=AUTH).json()
        assert len(calls)==1 and h['config']['attempts']==1
        assert any(x['provider']=='rules' and x['state']=='ok' for x in h['items'])
        assert any(x['provider']=='openai' and x['state']=='error' for x in h['items'])
        save_settings({'AI_REPORT_AUTO_RUN':'0'})
        a.archive_after_collect()
        assert len(calls)==1
