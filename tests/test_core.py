import math
from datetime import date,timedelta
import httpx,pytest
from app.analysis import cycle, transform, risk, backtest, month_before
from app.catalog import BY_ID, SERIES
from app.providers import points, DataError, fred, ecos, oecd, kosis, NotConfigured
from app.db import init,Session,write_series,history,Revision
from sqlalchemy import select

def test_catalog():
    assert len(SERIES)==85 and len(BY_ID)==len(SERIES)
    assert len({(s.provider,s.code,s.item) for s in SERIES})==len(SERIES)

def test_parse_missing_and_duplicate():
    assert points([{'t':'202501','v':'100.3'},{'t':'202502','v':'.'}],'t','v')=={date(2025,1,1):100.3}
    with pytest.raises(DataError): points([{'t':'202501','v':1},{'t':'202501','v':2}],'t','v')

def test_transforms():
    assert transform('cpi',[(date(2024,1,1),100),(date(2025,1,1),104)])[-1][1]==pytest.approx(4)
    assert transform('payroll',[(date(2025,1,1),100),(date(2025,2,1),103)])[-1][1]==3

def test_cycles_and_missing():
    for a,b,p in [(99,99.5,'회복'),(99,101,'확장'),(102,101,'둔화'),(101,99,'하강'),(100,100,'보합')]:
        assert cycle([(date(2025,1,1),a),(date(2025,4,1),b)])['phase']==p
    assert cycle([(date(2025,4,1),100)])['phase']=='미확인'

def test_risk_missing_and_score():
    end=date(2025,11,1)
    assert risk({},end,'확장')['score'] is None
    days=[end-timedelta(days=n) for n in range(290,0,-1) if (end-timedelta(days=n)).weekday()<5]
    rows={'vix':[(end,35)],'hy':[(end,7)],'sp500':[(d,300-i) for i,d in enumerate(days)]}
    result=risk(rows,end,'하강')
    assert result['score']==100 and result['allocation']==[10,40,50]
    assert risk(rows,end+timedelta(days=20),'하강')['score'] is None

def test_revision_idempotence():
    init(); d=date(2020,1,1); sid='test_revision'
    from app.db import Obs
    from sqlalchemy import delete
    with Session.begin() as s:
        s.execute(delete(Obs).where(Obs.series==sid)); s.execute(delete(Revision).where(Revision.series==sid))
    with Session.begin() as s: assert write_series(s,sid,{d:10})==1
    with Session.begin() as s: assert write_series(s,sid,{d:10})==0
    with Session.begin() as s: assert write_series(s,sid,{d:11})==1
    with Session() as s:
        assert history(s,sid)==[(d,11)] and len(s.scalars(select(Revision).where(Revision.series==sid)).all())==2

def test_fred_mock(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY','test')
    def handler(req):
        assert req.url.params['series_id']=='CPIAUCSL'
        return httpx.Response(200,json={'count':2,'observations':[{'date':'2025-01-01','value':'300'},{'date':'2025-02-01','value':'.'}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c: assert fred(c,BY_ID['cpi'],date(2025,1,1),date(2025,2,1))=={date(2025,1,1):300.}

def test_ecos_mock(monkeypatch):
    monkeypatch.setenv('ECOS_API_KEY','test')
    def handler(req):
        assert '/722Y001/D/20250101/20250201/0101000' in str(req.url)
        return httpx.Response(200,json={'StatisticSearch':{'list_total_count':1,'row':[{'TIME':'20250103','DATA_VALUE':'3.0'}]}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c: assert ecos(c,BY_ID['kr_rate'],date(2025,1,1),date(2025,2,1))=={date(2025,1,3):3.}

def test_oecd_mock():
    def handler(req):
        assert 'DF_CLI' in str(req.url)
        return httpx.Response(200,text='REF_AREA,TIME_PERIOD,OBS_VALUE\nUSA,2025-01,100.1\nKOR,2025-01,99.8\nGBR,2025-01,101.3\n')
    with httpx.Client(transport=httpx.MockTransport(handler)) as c: assert oecd(c,date(2025,1,1),date(2025,2,1))['cli_kr']=={date(2025,1,1):99.8}

def test_kosis_mock(monkeypatch):
    monkeypatch.setenv('KOSIS_API_KEY','test');monkeypatch.setenv('KOSIS_LEADING_QUERY','orgId=101&tblId=test&objL1=A&itmId=B')
    def handler(req):
        assert req.url.params['tblId']=='test'
        return httpx.Response(200,json=[{'ITM_ID':'B','C1':'A','PRD_DE':'202501','DT':'100.1'}])
    with httpx.Client(transport=httpx.MockTransport(handler)) as c: assert kosis(c,BY_ID['kr_leading'],date(2025,1,1),date(2025,2,1))=={date(2025,1,1):100.1}

def test_backtest():
    cli=[(month_before(date(2025,1,1),n),100+2*math.sin((100-n)/8)) for n in range(100,0,-1)]
    prices=[(month_before(date(2025,1,1),n),[100*1.01**(60-n),100*1.003**(60-n),100*1.002**(60-n)]) for n in range(60,0,-1)]
    r=backtest(cli,prices); assert r['months']==59 and 0<r['strategy_return']<r['benchmark_return'] and r['mdd']<=0
    with pytest.raises(ValueError): backtest(cli,prices[:30]+prices[31:])

def test_unconfigured(monkeypatch):
    monkeypatch.delenv('FRED_API_KEY',raising=False)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('unexpected network'))) as c:
        with pytest.raises(NotConfigured): fred(c,BY_ID['cpi'],date(2025,1,1),date(2025,2,1))
