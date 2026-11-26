import os
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import DEMO
from app.settings import load_settings, public_settings, directory, save_settings
from app import admin

AUTH=('admin','test-admin-password-1234')
HEADERS={'X-Macro-Admin':'1'}

@pytest.fixture
def config(monkeypatch,tmp_path):
    monkeypatch.setenv('SETTINGS_DIR',str(tmp_path))
    monkeypatch.setenv('ADMIN_PASSWORD',AUTH[1])
    monkeypatch.setenv('AUTO_COLLECT','0')
    for k in ('FRED_API_KEY','ECOS_API_KEY','KOSIS_API_KEY','KOSIS_LEADING_QUERY','KOSIS_COINCIDENT_QUERY'):
        monkeypatch.delenv(k,raising=False)
    return tmp_path

def test_settings_encryption_and_environment_override(config,monkeypatch):
    monkeypatch.setenv('FRED_API_KEY','env-key')
    assert load_settings()['FRED_API_KEY']=='env-key'
    save_settings({'FRED_API_KEY':'secret-test-123','KOSIS_LEADING_QUERY':'https://kosis.kr/?apiKey=secret-url&orgId=101&tblId=TEST&objL1=A&itmId=B'})
    assert load_settings()['FRED_API_KEY']=='secret-test-123'
    assert 'secret-test-123' not in (directory()/'settings.enc').read_text()
    assert 'secret-url' not in str(public_settings())
    assert 'secret-test-123' not in str(public_settings())
    save_settings({'FRED_API_KEY':''})
    assert not load_settings()['FRED_API_KEY']
    with pytest.raises(ValueError): save_settings({'KOSIS_COINCIDENT_QUERY':'https://kosis.kr/?bad=1'})
    with pytest.raises(ValueError): save_settings({'COLLECT_HOUR_KST':'25'})

@pytest.mark.skipif(not DEMO,reason='demo mode integration test')
def test_admin_http(config):
    with TestClient(app) as c:
        assert c.get('/admin').status_code==401
        assert c.get('/api/admin/settings').status_code==401
        assert c.get('/api/admin/settings',auth=('admin','wrong')).status_code==401
        assert c.get('/admin',auth=AUTH).status_code==200
        assert c.get('/static/admin.html').status_code==404
        assert c.post('/api/collect').status_code==401
        assert c.put('/api/admin/settings',auth=AUTH,json={'values':{'FRED_API_KEY':'test-key'}}).status_code==403
        r=c.put('/api/admin/settings',auth=AUTH,headers={**HEADERS,'Origin':'https://evil.example'},json={'values':{'FRED_API_KEY':'test-key'}})
        assert r.status_code==403
        r=c.put('/api/admin/settings',auth=AUTH,headers=HEADERS,json={'values':{'FRED_API_KEY':'test-key','AUTO_COLLECT':'0'}})
        assert r.status_code==200 and r.json()['configured']['FRED_API_KEY']
        assert 'test-key' not in r.text
        assert c.get('/api/admin/status',auth=AUTH).status_code==200
        assert c.post('/api/admin/test/FRED',auth=AUTH,headers=HEADERS).status_code==409
        assert c.post('/api/collect',auth=AUTH,headers=HEADERS).status_code==409
        assert c.get('/api/admin/settings',auth=AUTH).headers['Cache-Control']=='no-store'
        assert c.get('/',headers={'Host':'evil.example'}).status_code==400
        r=c.put('/api/admin/settings',auth=AUTH,headers=HEADERS,json={'values':{'FRED_API_KEY':'test-key','HISTORY_START':'invalid-secret'}})
        assert r.status_code==400 and 'invalid-secret' not in r.text
        assert c.put('/api/admin/settings',auth=AUTH,headers=HEADERS,json={'values':{'FRED_API_KEY':['secret']}}).status_code==422

def test_connection_with_mock(config,monkeypatch):
    # Exercise the live connection handler using a mock transport; never call the internet.
    monkeypatch.setattr(admin,'DEMO',False)
    save_settings({'FRED_API_KEY':'mock-registered-key'})
    orig=httpx.Client
    def handler(request):
        assert request.url.params['api_key']=='mock-registered-key'
        return httpx.Response(200,json={'count':1,'observations':[{'date':'2026-10-01','value':'320.0'}]})
    monkeypatch.setattr(admin.httpx,'Client',lambda **kw:orig(transport=httpx.MockTransport(handler)))
    r=admin.test_connection('FRED')
    assert r['ok'] and r['results'][0]['value']==320
    assert 'mock-registered-key' not in str(r)
