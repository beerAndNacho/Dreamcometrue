from datetime import date
from urllib.parse import parse_qsl
import httpx
import pytest
from fastapi.testclient import TestClient
from app import admin, providers as p
from app.catalog import BY_ID
from app.main import app
from app.settings import load_settings, save_settings, public_settings, query_fingerprint

START, END = date(2025, 1, 1), date(2025, 2, 28)
KEY = 'example+/key=='

@pytest.fixture
def isolated(monkeypatch,tmp_path):
    monkeypatch.setenv('SETTINGS_DIR',str(tmp_path))
    for k in ('KOSIS_API_KEY','KOSIS_LEADING_QUERY','KOSIS_COINCIDENT_QUERY'):
        monkeypatch.setenv(k,'')

def test_key_only_settings_and_slash(isolated):
    cfg=save_settings({'KOSIS_API_KEY':KEY})
    assert cfg['KOSIS_API_KEY']==KEY
    assert dict(parse_qsl(cfg['KOSIS_LEADING_QUERY']))=={'orgId':'101','tblId':'DT_1C8015','itmId':'T1','objL1':'A03'}
    assert dict(parse_qsl(cfg['KOSIS_COINCIDENT_QUERY']))['objL1']=='B03'
    assert KEY not in str(public_settings())
    custom='orgId=101&tblId=OTHER&itmId=T1&objL1=AB'
    cfg=save_settings({'KOSIS_LEADING_QUERY':custom})
    assert query_fingerprint(cfg['KOSIS_LEADING_QUERY'])==query_fingerprint(custom)
    assert load_settings()['KOSIS_API_KEY']==KEY
    cfg=save_settings({'KOSIS_LEADING_QUERY':''})
    assert dict(parse_qsl(cfg['KOSIS_LEADING_QUERY']))['objL1']=='A03'
    with pytest.raises(ValueError): save_settings({'ECOS_API_KEY':'invalid/path'})

@pytest.mark.parametrize('sid,code,name',[('kr_leading','A03','선행지수 순환변동치'),('kr_coincident','B03','동행지수 순환변동치')])
def test_default_request_and_identity(sid,code,name):
    def handler(req):
        assert req.url.host=='kosis.kr'
        assert req.url.params['apiKey']==KEY
        assert req.url.params['jsonVD']=='Y'
        assert req.url.params['objL1']==code
        assert req.url.params['startPrdDe']=='202501'
        return httpx.Response(200,json=[{'ORG_ID':'101','TBL_ID':'DT_1C8015','ITM_ID':'T1','C1':code,'C1_NM':name,'PRD_SE':'M','PRD_DE':'202501','DT':'100.1'}])
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert p.kosis(c,BY_ID[sid],START,END,{'KOSIS_API_KEY':KEY})=={START:100.1}

@pytest.mark.parametrize('payload,match',[
    ({'err':'11','errMsg':KEY},'오류 11'),
    ([{'err':'20','errMsg':KEY}],'오류 20'),
    ({'err':KEY,'errMsg':KEY},'오류 응답'),
    ([], '조회결과 없음'),
    ([{'ITM_ID':'T1','C1':'B03','C1_NM':'동행지수 순환변동치','PRD_DE':'202501','DT':'100'}],'코드 불일치'),
    ([{'ITM_ID':'T1','C1':'A03','C1_NM':'선행종합지수','PRD_DE':'202501','DT':'100'}],'지표명 불일치'),
    ([{'ITM_ID':'T1','C1':'A03','PRD_DE':'202501','DT':'100'}],'지표명 불일치'),
])
def test_safe_errors(payload,match):
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=payload))) as c:
        with pytest.raises(p.KosisError,match=match) as error:
            p.kosis(c,BY_ID['kr_leading'],START,END,{'KOSIS_API_KEY':KEY})
        assert KEY not in str(error.value)

def test_invalid_json():
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,text='<html>'+KEY+'</html>'))) as c:
        with pytest.raises(p.KosisError,match='JSON 응답 형식') as error:
            p.kosis(c,BY_ID['kr_leading'],START,END,{'KOSIS_API_KEY':KEY})
        assert KEY not in str(error.value)

def test_admin_connection_safe_diagnostics(monkeypatch,isolated):
    monkeypatch.setattr(admin,'DEMO',False)
    save_settings({'KOSIS_API_KEY':KEY})
    def fail(*args): raise p.KosisError('KOSIS 오류 11 · 활용신청 확인')
    monkeypatch.setattr(p,'kosis',fail)
    with TestClient(app) as c:
        result=c.post('/api/admin/test/KOSIS',auth=('admin','test-admin-password-1234'),headers={'X-Macro-Admin':'1'})
        assert result.status_code==200
        assert not result.json()['ok']
        assert all('오류 11' in item['message'] for item in result.json()['results'])
        assert KEY not in result.text
        assert c.get('/admin',auth=('admin','test-admin-password-1234')).status_code==200
        assert 'kosisPreset' in c.get('/admin',auth=('admin','test-admin-password-1234')).text
