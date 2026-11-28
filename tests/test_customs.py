from datetime import date
import httpx,pytest
from app.customs import parse_xml,fetch,normalized_key
from app.catalog import BY_ID
from app.providers import DataError,NotConfigured
from app.settings import load_settings,save_settings,public_settings

def xml(rows,code='00',count=None):
    items=''.join(f'<item><year>{m}</year><hsCode>{hs}</hsCode><expDlr>{v}</expDlr></item>' for m,hs,v in rows)
    return f'<response><header><resultCode>{code}</resultCode></header><body><items>{items}</items><totalCount>{len(rows) if count is None else count}</totalCount></body></response>'.encode()

def test_customs_parser():
    a,b=date(2026,9,1),date(2026,10,1)
    rows=[('2026.09','8542','2000000'),('2026.10','8542','3000000'),('총계','-','5000000')]
    assert parse_xml(xml(rows),'8542',a,b)=={a:2.,b:3.}
    for body in [xml([('2026.09','854232','10')]),xml([('2026.09','8542','-1')]),xml(rows+[(rows[0][0],'8542','1')]),xml(rows,count=99),xml(rows,code='03'),b'<!DOCTYPE x><x/>',b'not xml']:
        with pytest.raises(DataError):parse_xml(body,'8542',a,b)

def test_customs_fetch_completed_months_and_encoded_key():
    requests=[]
    def handler(req):
        requests.append(req)
        assert req.url.host=='apis.data.go.kr'
        assert req.url.params['serviceKey']=='x+/='
        assert req.url.params['hsSgn']=='854232'
        assert req.url.params['strtYymm']=='202609' and req.url.params['endYymm']=='202610'
        return httpx.Response(200,content=xml([('2026.09','854232','2000000'),('2026.10','854232','3000000')]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert fetch(c,BY_ID['kr_memory_exports'],date(2026,9,1),date(2026,11,27),{'CUSTOMS_API_KEY':'x%2B%2F%3D'})[date(2026,10,1)]==3.
        with pytest.raises(NotConfigured):fetch(c,BY_ID['kr_memory_exports'],date(2026,9,1),date(2026,11,27),{})
    assert len(requests)==1

def test_key_not_returned():
    old=load_settings()['CUSTOMS_API_KEY']
    try:
        save_settings({'CUSTOMS_API_KEY':'custom-secret%2B%2F%3D'})
        assert load_settings()['CUSTOMS_API_KEY']=='custom-secret+/='
        assert public_settings()['configured']['CUSTOMS_API_KEY']
        assert 'custom-secret' not in str(public_settings())
        with pytest.raises(ValueError):normalized_key('https://bad.test/?key=secret')
    finally:save_settings({'CUSTOMS_API_KEY':old})


def test_customs_rejects_missing_boundary_months():
    a,b=date(2026,9,1),date(2026,10,1)
    for rows in [[('2026.09','8542','2000000')],[('2026.10','8542','3000000')]]:
        with pytest.raises(DataError,match='누락'):
            parse_xml(xml(rows),'8542',a,b)
