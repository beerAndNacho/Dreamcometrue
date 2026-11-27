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


def test_ecos_connection_uses_saved_key_and_reports_provider_errors(config, monkeypatch):
    from app.catalog import BY_ID
    from app.db import init

    init()
    monkeypatch.setattr(admin, 'DEMO', False)
    issued_key = 'mock-ecos-key'
    expected_items = {(BY_ID[sid].code, BY_ID[sid].item) for sid in admin.SAMPLES['ECOS']}
    requested_items = set()
    response_code = None

    def handler(request):
        assert request.url.host == 'ecos.bok.or.kr'
        _, _, service, key, *_ = request.url.path.split('/')
        assert service == 'StatisticSearch' and key == issued_key
        table, freq, start, end, item = request.url.path.split('/')[-5:]
        assert freq == 'D' and len(start) == len(end) == 8
        requested_items.add((table, item))
        if response_code:
            return httpx.Response(200, json={'RESULT': {'CODE': response_code, 'MESSAGE': issued_key}})
        return httpx.Response(200, json={'StatisticSearch': {
            'list_total_count': 1,
            'row': [{'STAT_CODE': table, 'ITEM_CODE1': item, 'TIME': end, 'DATA_VALUE': '3.0'}],
        }})

    client_class = httpx.Client
    client = TestClient(app, headers=HEADERS)
    client.auth = AUTH
    monkeypatch.setattr(admin.httpx, 'Client', lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs,
    ))

    with client:
        path = '/api/admin/test/ECOS'
        missing = client.post(path).json()
        assert len(missing['results']) == 6
        assert all('저장된 ECOS API 키가 없습니다' in result['message'] for result in missing['results'])

        sample = client.put('/api/admin/settings', json={'values': {'ECOS_API_KEY': 'sample'}})
        assert sample.status_code == 200
        sample_result = client.post(path).json()
        assert all('공개 sample 키' in result['message'] for result in sample_result['results'])
        assert not requested_items

        saved = client.put('/api/admin/settings', json={'values': {'ECOS_API_KEY': issued_key}})
        assert saved.status_code == 200 and saved.json()['configured']['ECOS_API_KEY']
        assert issued_key not in saved.text
        success = client.post(path)
        assert success.json()['ok']
        assert requested_items == expected_items

        for response_code in ('INFO-100', 'INFO-200'):
            error = client.post(path)
            assert not error.json()['ok']
            assert all(response_code in result['message'] for result in error.json()['results'])
            assert issued_key not in error.text


def test_kosis_connection_explains_failures_without_leaking_key(config, monkeypatch):
    monkeypatch.setattr(admin, 'DEMO', False)
    key = 'mock-kosis-key'
    query = 'orgId=101&tblId=DT_1C8015&itmId=T1&objL1='
    save_settings({
        'KOSIS_API_KEY': key,
        'KOSIS_LEADING_QUERY': query + 'A03',
        'KOSIS_COINCIDENT_QUERY': query + 'B03',
    })
    requested_codes = set()

    def handler(request):
        assert request.url.host == 'kosis.kr'
        assert request.url.params['apiKey'] == key
        assert request.url.params['prdSe'] == 'M'
        code = request.url.params['objL1']
        requested_codes.add(code)
        return response(request)

    client_class = httpx.Client
    monkeypatch.setattr(admin.httpx, 'Client', lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    monkeypatch.setattr(admin.p.time, 'sleep', lambda _: None)

    response = lambda request: httpx.Response(200, json=[{
        'C1': request.url.params['objL1'], 'ITM_ID': 'T1',
        'PRD_DE': request.url.params['endPrdDe'], 'DT': '100.1',
    }])
    assert admin.test_connection('KOSIS')['ok']
    assert requested_codes == {'A03', 'B03'}

    def timeout(request):
        raise httpx.ConnectTimeout(str(request.url), request=request)

    cases = [
        (lambda _: httpx.Response(200, json={'err': '11', 'errMsg': key}), 'KOSIS 오류 11'),
        (lambda _: httpx.Response(200, json={'errCode': 21, 'errMsg': key}), 'KOSIS 오류 21'),
        (lambda _: httpx.Response(200, json={'err': '30', 'errMsg': key}), 'KOSIS 오류 30'),
        (lambda _: httpx.Response(200, json={'err': '40', 'errMsg': key}), 'KOSIS 오류 40'),
        (lambda _: httpx.Response(200, json={'err': key}), '인식할 수 없는 오류'),
        (lambda _: httpx.Response(503, text=key), 'HTTP 503'),
        (lambda _: httpx.Response(200, text=key), 'JSON 통계 응답'),
        (timeout, '앱 서버에서 KOSIS에 연결하지 못했습니다'),
    ]
    for response, expected in cases:
        result = admin.test_connection('KOSIS')
        assert not result['ok']
        assert len(result['results']) == 2
        assert all(expected in row['message'] for row in result['results'])
        assert key not in str(result)
