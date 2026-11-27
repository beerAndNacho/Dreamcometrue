from datetime import datetime, timedelta, timezone, date
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from app import notifications as n
from app import release_calendar as cal
from app.db import AlertDelivery, AlertState, Session, init, now
from app.alert_rules import parse_rules, normalize_rules
from app.settings import save_settings, load_settings, public_settings
from app.main import app

AUTH=('admin','test-admin-password-1234');HEAD={'X-Macro-Admin':'1'}
TOKEN='123456789:example_fake_bot_token_1234567890'
CFG={'TELEGRAM_BOT_TOKEN':TOKEN,'TELEGRAM_CHAT_ID':'123456789'}

@pytest.fixture
def clean():
    init();old=load_settings();fields=[k for k in old if k.startswith(('ALERT_','TELEGRAM_'))]
    with Session.begin() as db:
        db.execute(delete(AlertDelivery));db.execute(delete(AlertState))
    yield
    save_settings({k:old[k] for k in fields})
    with Session.begin() as db:
        db.execute(delete(AlertDelivery));db.execute(delete(AlertState))


def success(req):
    assert req.url.host=='api.telegram.org' and req.url.path.endswith('/sendMessage') and req.method=='POST'
    body=json.loads(req.content);assert body['chat_id']==123456789 and len(body['text'])<=2000
    assert 'parse_mode' not in body
    return httpx.Response(200,json={'ok':True,'result':{'message_id':7,'chat':{'id':123456789,'type':'private'}}})


def test_rules_validation():
    assert normalize_rules(' 005930,above,100000.00\n000270,below,90000')=='005930,above,100000\n000270,below,90000'
    for text in ['005930,up,100','005930,above,NaN','005930,above,0','05930,above,10','005930,above,100\n005930,above,100.0','005930,above,1e6','005930,above,-1','005930,above,100000001']:
        with pytest.raises(ValueError):parse_rules(text)
    assert n.crossed(99,100,'above',100) and n.crossed(101,100,'below',100)
    assert not n.crossed(100,110,'above',100) and not n.crossed(100,90,'below',100)


def test_settings_encrypted_preserve_kosis(clean):
    before=load_settings()
    with pytest.raises(ValueError):save_settings({'ALERT_ENABLED':'1'})
    with pytest.raises(ValueError):save_settings({'TELEGRAM_CHAT_ID':'-10012345'})
    with pytest.raises(ValueError):save_settings({'TELEGRAM_BOT_TOKEN':'invalid/token'})
    save_settings({**CFG,'ALERT_ENABLED':'1','ALERT_PRICE_RULES':'005930,above,100.0'})
    assert load_settings()['TELEGRAM_BOT_TOKEN']==TOKEN
    assert TOKEN not in json.dumps(public_settings()) and '123456789' not in json.dumps(public_settings())
    for k in ('KOSIS_API_KEY','KOSIS_LEADING_QUERY','KOSIS_COINCIDENT_QUERY'):assert before[k]==load_settings()[k]
    with pytest.raises(ValueError):save_settings({'TELEGRAM_BOT_TOKEN':''})
    save_settings({'ALERT_ENABLED':'0','TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''})


def test_send_validates_response_and_hides_secrets():
    with httpx.Client(transport=httpx.MockTransport(success)) as c:n.send_telegram(c,CFG,'test')
    for response in [httpx.Response(429,json={'description':TOKEN}),httpx.Response(200,json={'ok':True,'result':{'message_id':1,'chat':{'id':1,'type':'private'}}}),httpx.Response(200,text=TOKEN),httpx.Response(401,json={'description':TOKEN})]:
        with httpx.Client(transport=httpx.MockTransport(lambda req:response)) as c:
            with pytest.raises(n.DeliveryError) as e:n.send_telegram(c,CFG,'test')
            assert TOKEN not in str(e.value)
    with httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ReadTimeout(TOKEN,request=req)))) as c:
        with pytest.raises(n.DeliveryError) as e:n.send_telegram(c,CFG,'test')
        assert TOKEN not in str(e.value)


def test_dedup_restart_and_failure(clean):
    clock=datetime.now(timezone.utc);notice=n.Notice('daily','day1','test');calls=[]
    with httpx.Client(transport=httpx.MockTransport(lambda req:(calls.append(req),success(req))[1])) as c:
        assert n.deliver(notice,CFG,clock,c)=='sent';assert n.deliver(notice,CFG,clock,c)=='duplicate';assert len(calls)==1
        pending=n.Notice('flow','interrupted','test');assert n.claim(pending,clock);n.interrupt_alerts()
        assert n.deliver(pending,CFG,clock,c)=='duplicate';assert len(calls)==1
    fail=n.Notice('price','failure','test')
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(500))) as c:
        assert n.deliver(fail,CFG,clock,c)=='failed';assert n.deliver(fail,CFG,clock,c)=='duplicate'
    with Session() as db:assert db.get(AlertDelivery,n.key_for(pending)).state=='unknown';assert db.get(AlertDelivery,n.key_for(fail)).state=='failed'


def test_pair_stale_and_missing():
    day=date(2026,11,27);obj={'state':'ok','stale':False,'rows':[{'date':'2026-11-25','close':90},{'date':'2026-11-26','close':100}]}
    assert n.valid_row_pair(obj,day,'close')==(90,100,'2026-11-26')
    for change in [{'stale':True},{'state':'error'},{'rows':[{'date':'2026-11-25','close':90},{'date':'2026-11-27','close':100}]},{'rows':[{'date':'2026-11-25','close':90},{'date':'2026-11-26','close':float('nan')}]}]:assert n.valid_row_pair({**obj,**change},day,'close') is None
    assert n.valid_row_pair(obj,day+timedelta(days=10),'close') is None


def test_risk_baseline_missing_and_repeat(clean):
    day=date(2026,11,27)
    def d(score,band):return {'model':{'score':score,'label':band},'indicators':[]}
    assert n.risk_notice(d(35,'낮음'),day) is None
    notice=n.risk_notice(d(40,'주의'),day);assert notice and notice.kind=='risk'
    assert n.risk_notice(d(40,'주의'),day)==notice # no loss if send budget deferred
    n.record_risk(notice,day);assert n.risk_notice(d(40,'주의'),day) is None
    assert n.risk_notice(d(None,None),day) is None
    assert n.risk_notice(d(85,'높음'),day) is None
    assert n.risk_notice(d(20,'낮음'),day+timedelta(days=3)) is None


def test_calendar_and_dividend_filter(monkeypatch):
    clock=datetime(2026,11,27,22,0,tzinfo=n.KST)
    e={'state':'예정','origin':'BLS 구독','id':'a','title':'CPI','agency':'BLS','when':(clock+timedelta(minutes=30)).isoformat()}
    monkeypatch.setattr(cal,'calendar_data',lambda **_: {'bls_stale':True,'events':[e,{**e,'id':'b','origin':'사용자 확인','title':'private note'}, {**e,'id':'c','state':'취소'}]})
    out=list(n.calendar_notices(clock));assert len(out)==1 and 'private note' not in out[0].text and 'BLS' in out[0].text
    monkeypatch.setattr(n.km,'watches',lambda:[{'code':'005930','name':'private note'}])
    tomorrow=str(clock.date()+timedelta(days=1));row={'receipt':'20261127000001','status':'확정','pay_date':tomorrow}
    monkeypatch.setattr(n.kv,'payload',lambda k:{'state':'ok','data':{'items':[row,{**row,'status':'취소'}]}})
    out=list(n.dividend_notices(clock.date()));assert len(out)==1 and 'private note' not in out[0].text and tomorrow in out[0].text


def test_report_waits_for_collect():
    clock=datetime(2026,11,27,9,tzinfo=n.KST);cfg={'AUTO_COLLECT':'1','COLLECT_HOUR_KST':'8'}
    assert not n.report_ready(cfg,clock,{'last_run':None})
    assert not n.report_ready(cfg,clock,{'last_run':{'state':'running','started':'2026-11-26T23:00:00Z','finished':None}})
    assert n.report_ready(cfg,clock,{'last_run':{'state':'partial','started':'2026-11-26T23:00:00Z','finished':'2026-11-26T23:30:00Z'}})
    assert n.report_ready({**cfg,'AUTO_COLLECT':'0'},clock,{'last_run':None})


def test_notification_auth_and_pwa(clean):
    with TestClient(app) as c:
        assert c.get('/notifications').status_code==401 and c.get('/api/notifications').status_code==401
        assert c.get('/notifications',auth=AUTH).status_code==200
        assert c.post('/api/notifications/test',auth=AUTH,json={'confirm':True}).status_code==403
        assert c.post('/api/notifications/test',auth=AUTH,headers=HEAD,json={'confirm':True}).status_code==409
        assert c.get('/api/notifications',auth=AUTH).json()['demo'] is True
        m=c.get('/manifest.webmanifest');assert m.status_code==200 and 'manifest+json' in m.headers['content-type']
        assert m.json()['display']=='standalone'
        for icon in m.json()['icons']:
            response=c.get(icon['src']);assert response.status_code==200 and response.content[:8]==b'\x89PNG\r\n\x1a\n'
        sw=c.get('/sw.js');assert sw.status_code==200 and sw.headers['cache-control']=='no-store' and 'caches.open' not in sw.text
        assert '/manifest.webmanifest' in c.get('/').text


def test_scheduler_caps_and_off(clean,monkeypatch):
    clock=datetime.now(n.KST);cfg={**CFG,'ALERT_ENABLED':'1','ALERT_DAILY':'0','ALERT_RISK':'0','ALERT_FLOW':'1','ALERT_CALENDAR':'0','ALERT_DIVIDEND':'0','ALERT_PRICE_RULES':''}
    monkeypatch.setattr(n,'DEMO',False);monkeypatch.setattr(n,'load_settings',lambda:cfg)
    monkeypatch.setattr(n,'flow_notices',lambda day:[n.Notice('flow',str(i),'test') for i in range(25)])
    calls=[]
    monkeypatch.setattr(n,'send_telegram',lambda client,cfg,text:calls.append(text))
    for _ in range(8):n.run_once(clock)
    assert len(calls)==20 and n.count_today(clock.date())==20
    assert n.run_once(clock)==[]
    cfg['ALERT_ENABLED']='0';assert n.run_once(clock+timedelta(days=1))==[]


def test_scheduler_cooldown_and_test_delivery(clean,monkeypatch):
    save_settings({**CFG,'ALERT_ENABLED':'0'})
    monkeypatch.setattr(n,'DEMO',False)
    calls=[]
    monkeypatch.setattr(n,'send_telegram',lambda client,cfg,text:calls.append(text))
    with TestClient(app) as c:
        assert c.post('/api/notifications/test',auth=AUTH,headers=HEAD,json={'confirm':False}).status_code==400
        r=c.post('/api/notifications/test',auth=AUTH,headers=HEAD,json={'confirm':True});assert r.status_code==200 and r.json()['state']=='sent'
        assert c.post('/api/notifications/test',auth=AUTH,headers=HEAD,json={'confirm':True}).status_code==429
    assert len(calls)==1
    cfg={**CFG,'ALERT_ENABLED':'1','ALERT_DAILY':'0','ALERT_RISK':'0','ALERT_FLOW':'1','ALERT_CALENDAR':'0','ALERT_DIVIDEND':'0','ALERT_PRICE_RULES':''}
    monkeypatch.setattr(n,'load_settings',lambda:cfg)
    monkeypatch.setattr(n,'flow_notices',lambda day:[n.Notice('flow','first','test'),n.Notice('flow','second','test')])
    def fail(*args):
        calls.append('failed');raise n.DeliveryError('mock failure')
    monkeypatch.setattr(n,'send_telegram',fail)
    assert n.run_once()==['failed']
    assert n.run_once()==[] and len(calls)==2
