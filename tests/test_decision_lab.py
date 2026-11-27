from datetime import date,timedelta
from fastapi.testclient import TestClient
import pytest
from app import global_market as g, decision_lab as lab, kr_market as km
from app.main import app
from app.collect import LOCK
from app.settings import save_settings, load_settings
AUTH=('admin','test-admin-password-1234');HEAD={'X-Macro-Admin':'1'}

def test_completed_sessions_and_gap_detection():
    end=date(2026,11,26)
    rows=g.demo_rows('QQQ',end)
    assert len(rows)==600 and rows[-1]['date']=='2026-11-25'
    assert g.quality(rows,end,'ok')=='분석 가능'
    assert '누락' in g.quality(rows[:300]+rows[301:],end,'ok')
    assert '최근' in g.quality(rows[:-1],end,'ok')
    assert '누락' in g.quality(rows[:300]+[rows[299]]+rows[301:],end,'ok')
    with pytest.raises(km.MarketError):g.parse_prices([{'xymd':'20261126','open':'10','high':'12','low':'9','clos':'11','tvol':'100'}],end)

def test_strategy_lag_and_cost():
    # The price jumps at session 201. Signal at 200 is flat; cannot capture that jump.
    c=[100.]*201+[200.,220.,198.]
    strat=lab.simulate_strategy(c,200,'ma200',1)
    hold=lab.simulate_strategy(c,200,'hold',1)
    assert strat['curve'][:2]==[100.,100.]
    assert strat['curve'][2]==pytest.approx(99.) # Buy at session 202, not the earlier close.
    assert strat['curve'][3]==pytest.approx(89.1)
    assert strat['trades']==1 and strat['invested_pct']==pytest.approx(100/3)
    assert hold['curve'][-1]==pytest.approx(196.02)
    assert hold['mdd_pct']==pytest.approx(-10)

def test_identical_benchmark_and_cost():
    dates=g.expected_window(date(2026,11,26),261)
    prices={'QQQ':[100+i for i in range(261)]}
    r=lab.strategy_result(lab.StrategyRequest(symbol='QQQ',benchmark='QQQ',periods=60,fee_pct=.25),dates,prices)
    assert len(r['dates'])==61 and len(r['strategies'])==5
    assert r['strategies'][0]['curve']==r['strategies'][-1]['curve']
    assert r['strategies'][0]['return_pct']==pytest.approx((360/300*.9975-1)*100)

def test_portfolio_weights_shock_and_correlation():
    dates=g.expected_window(date(2026,11,26),61)
    a=[100.]
    for i in range(60):a.append(a[-1]*(1+(.01 if i%2 else -.02)))
    b=[2*v for v in a]
    req=lab.PortfolioRequest(positions=[{'symbol':'QQQ','weight':40,'shock_pct':-20},{'symbol':'SOXX','weight':30,'shock_pct':-30}],benchmark='VOO',periods=60,loss_budget_pct=10)
    r=lab.portfolio_result(req,dates,{'QQQ':a,'SOXX':b,'VOO':a})
    assert r['cash_pct']==30 and r['max_weight_pct']==40 and r['stress_pct']==-17 and r['stress_exceeds_budget']
    assert r['beta']==pytest.approx(.7)
    assert r['correlation']['values'][0][1]==pytest.approx(1)
    assert r['relative'][0]['relative_pp']==pytest.approx(0)
    assert r['curve'][1]==pytest.approx(98.6)
    assert lab.corr([1,1,1],[2,3,4]) is None

def test_input_validation_and_auth():
    with TestClient(app) as c:
        assert c.get('/decision-lab').status_code==401
        assert c.get('/decision-lab',auth=AUTH).status_code==200
        body={'symbol':'QQQ','benchmark':'VOO','periods':60,'fee_pct':.1}
        assert c.post('/api/decision-lab/strategy',auth=AUTH,json=body).status_code==403
        assert c.post('/api/decision-lab/strategy',auth=AUTH,headers=HEAD,json={**body,'fee_pct':True}).status_code==422
        for positions in [[{'symbol':'QQQ','weight':101}],[{'symbol':'QQQ','weight':0}],[{'symbol':'QQQ','weight':60},{'symbol':'SOXX','weight':60}],[{'symbol':'QQQ','weight':20},{'symbol':'QQQ','weight':20}],[{'symbol':'XYZ','weight':20}]]:
            assert c.post('/api/decision-lab/portfolio',auth=AUTH,headers=HEAD,json={'positions':positions}).status_code==422
        out=c.post('/api/decision-lab/strategy',auth=AUTH,headers=HEAD,json=body)
        assert out.status_code==200 and out.json()['status']=='ok' and out.json()['demo']
        risk=c.post('/api/decision-lab/portfolio',auth=AUTH,headers=HEAD,json={'positions':[{'symbol':'QQQ','weight':40}],'periods':60}).json()
        assert risk['status']=='ok' and risk['cash_pct']==60
        assert LOCK.acquire(blocking=False)
        try:assert c.post('/api/decision-lab/strategy',auth=AUTH,headers=HEAD,json=body).json()['status']=='withheld'
        finally:LOCK.release()

def test_alignment_and_stale_withhold(monkeypatch):
    original=g.state
    def state(s,full):
        out=original(s,full)
        if s=='QQQ':out['quality']='최근 완료 거래일 자료 확인 필요'
        return out
    monkeypatch.setattr(g,'state',state)
    with TestClient(app) as c:
        r=c.post('/api/decision-lab/strategy',auth=AUTH,headers=HEAD,json={'symbol':'QQQ','periods':60}).json()
        assert r['status']=='withheld' and 'QQQ' in r['reasons'] and 'strategies' not in r
    def short(s,full):
        out=original(s,full);out['rows']=out['rows'][-200:];return out
    monkeypatch.setattr(g,'state',short)
    assert lab.load_aligned(['QQQ'],261)[0] is None

def test_short_or_gapped_refresh_preserves_original(monkeypatch):
    monkeypatch.setattr(g,'DEMO',False);monkeypatch.setattr(g,'LAST',0)
    old=g.demo_rows('QQQ',g.cutoff());cfg={'KIS_APP_KEY':'fake','KIS_APP_SECRET':'fake'}
    km.store('global:adjusted:QQQ',old,merge=False)
    monkeypatch.setattr(g,'fetch_prices',lambda *a,**kw:old[:300]+old[301:])
    result=g.collect_selected(cfg,['QQQ']);assert result[0]['state']=='error'
    assert km.cached('global:adjusted:QQQ')['rows']==old
    monkeypatch.setattr(g,'fetch_prices',lambda *a,**kw:old[-200:])
    assert g.collect_selected(cfg,['QQQ'])[0]['state']=='error'
    assert km.cached('global:adjusted:QQQ')['rows']==old

def test_optional_schedule_validation_and_skip_current(monkeypatch):
    assert g.normalize_auto_symbols(' qqq, SOXX ')=='QQQ,SOXX'
    for value in ('XYZ','QQQ,QQQ','QQQ,SOXX,VOO,IEF'):
        with pytest.raises(ValueError):g.normalize_auto_symbols(value)
    before=load_settings()['GLOBAL_AUTO_SYMBOLS']
    try:
        assert save_settings({'GLOBAL_AUTO_SYMBOLS':'qqq,soxx'})['GLOBAL_AUTO_SYMBOLS']=='QQQ,SOXX'
        with pytest.raises(ValueError):save_settings({'GLOBAL_AUTO_SYMBOLS':'QQQ,QQQ'})
        assert g.collect_configured({'GLOBAL_AUTO_SYMBOLS':''})==[]
        assert g.collect_configured({'GLOBAL_AUTO_SYMBOLS':'QQQ'})==[] # demo never calls external API
    finally:save_settings({'GLOBAL_AUTO_SYMBOLS':before})
    monkeypatch.setattr(g,'DEMO',False)
    rows=g.demo_rows('QQQ',g.cutoff());km.store('global:adjusted:QQQ',rows,merge=False)
    monkeypatch.setattr(g,'fetch_prices',lambda *a,**kw:pytest.fail('unnecessary network call'))
    r=g.collect_configured({'GLOBAL_AUTO_SYMBOLS':'QQQ','KIS_APP_KEY':'fake','KIS_APP_SECRET':'fake'})
    assert r[0]['state']=='current'
