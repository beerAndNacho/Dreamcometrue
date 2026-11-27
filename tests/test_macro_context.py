from datetime import date,timedelta
import pytest,httpx,json
from app.analysis import month_before,transform
from app.catalog import BY_ID,SERIES
from app.macro_context import history_context,diagnostics
from app.providers import ecos,DataError,oecd
from app.main import app
from fastapi.testclient import TestClient

def test_monthly_rate_context_is_point_change_and_five_years():
    end=date(2026,10,1)
    rows=[(month_before(end,n),float(60-n)) for n in range(60,-1,-1)]
    h=history_context('cpi',rows)
    assert h['windows']['3m']=={'period':'2026-07-01','value':57.,'delta':3.}
    assert h['five_year']['count']==60 and h['five_year']['percentile']==100
    assert h['five_year']['mean']==29.5
    assert history_context('cpi',rows[1:])['five_year'] is None
    assert history_context('cpi',rows[:12]+rows[13:])['five_year'] is None
    assert '3m' not in history_context('cpi',rows[:-4]+rows[-3:])['windows']

def test_daily_weekend_and_negative_price():
    rows=[(date(2026,9,30),100.),(date(2026,10,30),120.)]
    h=history_context('sp500',rows)
    assert h['windows']['1m']['relative_pct']==20
    assert history_context('sp500',rows[:1])['windows']=={}
    assert 'relative_pct' not in history_context('oil',[(rows[0][0],-5.),rows[1]])['windows']['1m']
    assert history_context('sp500',rows)['five_year'] is None
    assert '1m' not in history_context('kr_gdp',[(date(2026,4,1),2.),(date(2026,7,1),3.)])['windows']

def test_aligned_diagnostics_and_missing():
    d=date(2026,11,25);asof=d+timedelta(days=2)
    vals={'fed_assets':[(d,7000000.)],'tga':[(d,800000.)],
          'rrp':[(d,20.),(d+timedelta(days=1),99.)],
          'sofr':[(d,4.3)],'iorb':[(d,4.2),(asof,9.)]}
    a,b=diagnostics(vals,asof)
    assert a['value']==6180 and a['period']==str(d) and len(a['sources'])==3
    assert b['value']==pytest.approx(.1)
    vals['tga']=[(d-timedelta(days=7),800000.)]
    assert diagnostics(vals,asof)[0]['value'] is None
    assert all(x['value'] is None for x in diagnostics(vals,asof,busy=True))

def test_ecos_cpi_identity_and_no_double_transform():
    row={'STAT_CODE':'901Y009','STAT_NAME':'소비자물가지수','ITEM_CODE1':'0','ITEM_NAME1':'총지수','UNIT_NAME':'2020=100','TIME':'202610','DATA_VALUE':'120'}
    def fetch(obj):
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'StatisticSearch':{'list_total_count':1,'row':[obj]}}))) as c:
            return ecos(c,BY_ID['kr_cpi'],date(2026,10,1),date(2026,10,31),{'ECOS_API_KEY':'fake'})
    assert fetch(row)=={date(2026,10,1):120.}
    for bad in [{'ITEM_NAME1':'식료품'},{'STAT_CODE':'bad'},{'UNIT_NAME':'%'},{'ITEM_CODE1':'1'}]:
        with pytest.raises(DataError):fetch({**row,**bad})
    assert transform('kr_exports',[(date(2026,10,1),12.)])==[(date(2026,10,1),12.)]

def test_oecd_country_expansion_and_report_context():
    def response(req):
        assert 'USA+KOR+GBR+JPN+CHN+DEU+IND+G7' in str(req.url)
        return httpx.Response(200,text='REF_AREA,TIME_PERIOD,OBS_VALUE\nJPN,2026-10,101\nCHN,2026-10,98\n')
    with httpx.Client(transport=httpx.MockTransport(response)) as c:
        v=oecd(c,date(2026,1,1),date(2026,11,1))
        assert v['cli_jp']=={date(2026,10,1):101.} and isinstance(v['cli_us'],DataError)
    with TestClient(app) as c:
        d=c.get('/api/dashboard').json()
        assert len(d['indicators'])==len(SERIES) and {'cli_jp','cli_cn','cli_de','cli_in','cli_g7'}<=d['cycles'].keys()
        report=c.get('/api/intelligence',auth=('admin','test-admin-password-1234')).json()
        assert report['prompt'] and report['report']['demo']
        data=json.loads(report['prompt'].split('=== 관측자료 JSON ===\n',1)[1])
        assert any(e['id']=='cpi' and e.get('history_context') for e in data['evidence'])
        assert any(e['id']=='sox' for e in data['evidence'])
