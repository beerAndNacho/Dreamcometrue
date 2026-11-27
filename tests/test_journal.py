from datetime import date, datetime, timedelta
from decimal import Decimal as D
import pytest
import json
from fastapi.testclient import TestClient
from sqlalchemy import delete
from app.main import app
from app.journal import Book, Entry, Mark, JournalStore, LedgerError, evaluate, benchmarks
from app.collect import today
from app.db import Session, init, Obs, now, Status, MarketCache, MarketWatch, Revision
from app.analysis import month_before
from app.vintage_backtest import simulate, cutoff_for, parse_prices

AUTH=('admin','test-admin-password-1234');HEAD={'X-Macro-Admin':'1'}
A=date(2024,1,2);B=date(2024,1,3);C=date(2024,1,4);E=date(2024,1,5)
def entry(i,kind,d=A,**kw):return Entry(id=str(i),day=d,kind=kind,**kw)
def deposit(n=1000000,d=A):return entry(1,'deposit',d,amount=n)
def mark(d,s='005930',price=100,fx=1,cur='KRW'):return Mark(day=d,symbol=s,currency=cur,price=price,fx=fx)
def ev(entries,marks=(),days=(),asof=E):return evaluate(Book(entries=list(entries),marks=list(marks),valuation_days=list(days)),asof)

@pytest.fixture
def clean():
    init()
    with Session.begin() as db:db.execute(delete(JournalStore))
    yield
    with Session.begin() as db:db.execute(delete(JournalStore))


def test_moving_average_partial_sell_and_cash_reconciliation():
    rows=[deposit(),entry(2,'buy',symbol='005930',quantity=10,price=100,fee=10),entry(3,'buy',B,symbol='005930',quantity=10,price=200,fee=10),entry(4,'sell',C,symbol='005930',quantity=5,price=300,fee=10),entry(5,'dividend',C,symbol='005930',amount=100,fee=15),entry(6,'expense',C,amount=5)]
    r=ev(rows,[mark(E,price=250)],[E])['current']
    assert D(r['positions'][0]['quantity'])==15
    assert D(r['positions'][0]['cost_krw'])==2265
    assert D(r['realized'])==735 and D(r['unrealized'])==1485
    assert D(r['dividend'])==85 and D(r['expense'])==5
    assert D(r['total_profit'])==735+1485+85-5
    assert D(r['equity'])==1002300


def test_fx_components_and_full_exit():
    rows=[deposit(),entry(2,'buy',symbol='QQQ',currency='USD',quantity=2,price=100,fee=2,fx=1000),entry(3,'sell',B,symbol='QQQ',currency='USD',quantity=1,price=120,fee=1,fx=1200)]
    r=ev(rows,[mark(E,'QQQ',110,1300,'USD')],[E])['current']
    assert D(r['realized'])==41800 and D(r['realized_fx'])==20200
    assert D(r['unrealized'])==42000 and D(r['unrealized_fx'])==30300
    assert D(r['total_profit'])==83800
    r=ev(rows+[entry(4,'sell',E,symbol='QQQ',currency='USD',quantity=1,price=110,fx=1300)])['current']
    assert r['positions']==[] and D(r['realized'])==83800


def test_partial_rounding_conserves_cost():
    rows=[deposit(),entry(2,'buy',symbol='005930',quantity=3,price='1.01'),entry(3,'buy',symbol='005930',quantity=1,price='1.02'),entry(4,'sell',B,symbol='005930',quantity=1,price=2),entry(5,'sell',B,symbol='005930',quantity=1,price=2),entry(6,'sell',B,symbol='005930',quantity=2,price=2)]
    r=ev(rows)['current'];assert r['positions']==[] and D(r['realized'])==D('3.95') and D(r['total_profit'])==D('3.95')


def test_reject_invalid_order_cash_oversell_and_huge_values():
    for rows in ([entry(2,'buy',symbol='005930',quantity=1,price=10),deposit(d=B)],[deposit(5),entry(2,'buy',symbol='005930',quantity=1,price=10)],[deposit(),entry(2,'sell',symbol='005930',quantity=1,price=10)],[deposit(),entry(2,'withdraw',amount=1000001)],[deposit(),entry(2,'buy',symbol='QQQ',currency='USD',quantity='999999999',price='999999999999999',fx='99999')]):
        with pytest.raises(LedgerError):ev(rows)
    for kw in ({'currency':'USD'},{'price':0},{'fx':1200},{'quantity':'0.0000001'},{'quantity':'NaN'}):
        with pytest.raises(ValueError):entry(2,'buy',**{'symbol':'QQQ','quantity':1,'price':10,**kw})
    with pytest.raises(ValueError):entry(2,'deposit',today()+timedelta(days=1),amount=1)


def test_missing_mark_no_carry_forward_and_retroactive_edit():
    rows=[deposit(),entry(2,'buy',symbol='005930',quantity=1,price=100)]
    r=ev(rows,[mark(A)], [A,E]);assert r['current']['equity'] is None and r['current']['unrealized'] is None
    assert r['current']['missing']==['005930/KRW'] and r['valuations'][0]['equity']=='1000000.00'
    assert r['performance']['return_pct'] is None
    r=ev(rows+[entry(3,'buy',B,symbol='000270',quantity=1,price=100)], [mark(A),mark(E)], [A,E]);assert r['current']['missing']==['000270/KRW']
    with pytest.raises(ValueError):Book(entries=[deposit(),deposit()])
    with pytest.raises(ValueError):Book(marks=[mark(A),mark(A)],valuation_days=[A])
    with pytest.raises(ValueError):Book(marks=[mark(A)])


def test_flow_adjusted_performance_and_drawdown():
    rows=[deposit(1000),entry(2,'buy',symbol='005930',quantity=10,price=100),entry(3,'deposit',B,amount=500),entry(4,'withdraw',C,amount=200)]
    r=ev(rows,[mark(A),mark(B,price=110),mark(C,price=90)],[A,B,C],C)
    # 1600 / (1000+500), 1200 / (1600-200)
    assert r['performance']['return_pct']==pytest.approx((1600/1500*1200/1400-1)*100)
    assert r['performance']['mdd_pct']==pytest.approx((1200/1400-1)*100)
    assert D(r['current']['contributed'])==1300 and D(r['current']['total_profit'])==-100
    broken=ev(rows,[mark(A),mark(C,price=90)],[A,C],C)['performance'];assert broken['return_pct'] is None and '입출금' in broken['reason']
    # No intermediate cash flow: irregular interval okay, but not a daily MDD.
    good=ev(rows[:2],[mark(A),mark(C,price=80)],[A,C],C)['performance'];assert good['return_pct']==pytest.approx(-20) and good['mdd_pct']==pytest.approx(-20)
    zero=ev([deposit(1000),entry(2,'withdraw',B,amount=1000)],[],[A,B],B)['performance'];assert zero['return_pct'] is None


def test_exact_date_benchmarks(monkeypatch):
    init(); dates=[date(1901,1,2),date(1901,1,3)]
    with Session.begin() as db:
        for sid,day,v in [('kospi',dates[0],100),('kospi',dates[1],110),('sp500',dates[0],200),('sp500',dates[1]+timedelta(days=1),220),('nasdaq',dates[0],200),('nasdaq',dates[1],220),('usdkrw',dates[0],1000),('usdkrw',dates[1],1200)]:db.merge(Obs(series=sid,period=day,value=v,fetched=now()))
    try:
        p={'reason':None,'points':[{'day':str(d),'index':100.} for d in dates]}
        with Session() as db:r=benchmarks(db,p)
        assert r[0]['return_pct']==pytest.approx(10) and r[1]['return_pct'] is None
        assert r[2]['return_pct']==pytest.approx(10) and r[2]['krw_return_pct']==pytest.approx(32)
    finally:
        with Session.begin() as db:db.execute(delete(Obs).where(Obs.period>=dates[0],Obs.period<=dates[1]+timedelta(days=1)))


def test_authenticated_atomic_save_conflict_export_and_asof(clean):
    client=TestClient(app)
    assert client.get('/journal').status_code==401 and client.get('/api/journal').status_code==401 and client.get('/api/journal/export').status_code==401
    book=Book(entries=[deposit(),entry(2,'buy',B,symbol='005930',quantity=1,price=100)],marks=[mark(E)],valuation_days=[E]).model_dump(mode='json');payload={'revision':0,**book}
    assert client.put('/api/journal',auth=AUTH,json=payload).status_code==403
    assert client.put('/api/journal',auth=AUTH,headers={**HEAD,'Origin':'https://untrusted.invalid'},json=payload).status_code==403
    r=client.put('/api/journal',auth=AUTH,headers=HEAD,json=payload);assert r.status_code==200,r.text;assert r.json()['revision']==1
    assert client.put('/api/journal',auth=AUTH,headers=HEAD,json=payload).status_code==409
    bad={'revision':1,**book,'entries':book['entries'][1:]};assert client.put('/api/journal',auth=AUTH,headers=HEAD,json=bad).status_code==400
    r=client.get('/api/journal',auth=AUTH,params={'asof':str(A)});assert r.json()['revision']==1 and len(r.json()['current']['positions'])==0
    r=client.get('/api/journal/export',auth=AUTH);assert r.json()['book']==book and r.headers['cache-control']=='no-store'
    r=client.put('/api/journal',auth=AUTH,headers=HEAD,json={'revision':1,**book});assert r.status_code==200 and r.json()['revision']==2
    assert client.put('/api/journal',auth=AUTH,headers=HEAD,json={'revision':1,**book}).status_code==409


def vintage_fixture():
    start=date(2022,1,1);ds=[month_before(start,-i) for i in range(13)];rows=[(d,[100+i*2,100+i,100+i*.1]) for i,d in enumerate(ds)];revisions=[]
    for i in range(-4,13):
        period=month_before(start,-i);seen=cutoff_for(month_before(period,-1))-timedelta(days=1);revisions.append((period,seen,100.5+i*.1,i+5))
    return rows,revisions


def test_vintage_never_uses_later_revision_and_fee():
    rows,revs=vintage_fixture();r=simulate(rows,revs,10)
    assert r['months']==12 and r['points'][0]['phase']=='확장'
    assert r['points'][0]['turnover_pct']==pytest.approx(90) and r['points'][0]['cost_pct']==pytest.approx(.09)
    # Same period revised after test end must not affect past results, even if value changes phase.
    changed=simulate(rows,revs+[(rows[0][0],datetime(2026,1,1),90,99)],10)
    assert changed==r
    delayed=[(d,datetime(2026,1,1),v,i) for d,_,v,i in revs]
    with pytest.raises(ValueError,match='당시 저장된'):simulate(rows,delayed,10)
    for point,(d,_) in zip(r['points'],rows[:-1]):
        assert datetime.fromisoformat(point['cli_seen_utc'][:-1])<cutoff_for(d)
    # A revision seen on the boundary is excluded from the first period.
    late=simulate(rows,revs+[(date(2021,12,1),cutoff_for(rows[0][0]),99,99)],10)
    assert late['points'][0]==r['points'][0]


def test_vintage_csv_and_demo_restriction():
    rows,_=vintage_fixture();csv='month,stock,bond,cash\n'+'\n'.join(f'{d:%Y-%m},'+','.join(map(str,v)) for d,v in rows)
    assert len(parse_prices(csv))==13
    with pytest.raises(ValueError):parse_prices(csv+'\n'+csv.splitlines()[-1])
    with pytest.raises(ValueError):parse_prices(csv.replace('100.0','NaN'))
    client=TestClient(app);r=client.post('/api/journal/backtest',json={'csv':csv,'fee_bps':10},auth=AUTH,headers=HEAD)
    assert r.status_code==400 and '데모' in r.json()['detail']


def test_exact_mark_hint_and_no_implicit_save(clean,monkeypatch):
    from app import journal
    monkeypatch.setattr(journal,'DEMO',False)
    code='999991';day=date(1901,1,2)
    with Session.begin() as db:
        previous_status=db.get(Status,'usdkrw')
        db.merge(MarketWatch(code=code,name='fixture',corp_code=''))
        db.merge(MarketCache(key='price:'+code,payload=json.dumps([{'date':str(day),'close':12345}]),state='ok',message='fixture',updated=now(),attempted=now()))
        db.merge(Status(series='usdkrw',state='ok',message='fixture',attempted=now(),success=now()))
        db.merge(Obs(series='usdkrw',period=day,value=1234.56,fetched=now()))
    client=TestClient(app)
    def hint(d,s,c):return client.get('/api/journal/mark-hint',auth=AUTH,params={'day':str(d),'symbol':s,'currency':c}).json()
    try:
        assert hint(day,code,'KRW')['price']=='12345.000000'
        assert hint(day+timedelta(days=1),code,'KRW')['price'] is None
        assert hint(day,'QQQ','USD')['fx']=='1234.560000' and hint(day,'QQQ','USD')['price'] is None
        assert hint(day,'QQQ','GBP')['fx'] is None
        assert hint(today(),code,'KRW')['price'] is None
        with Session.begin() as db:
            db.get(MarketCache,'price:'+code).state='error'
        assert hint(day,code,'KRW')['price'] is None
        assert client.get('/api/journal',auth=AUTH).json()['revision']==0
    finally:
        with Session.begin() as db:
            db.execute(delete(MarketWatch).where(MarketWatch.code==code));db.execute(delete(MarketCache).where(MarketCache.key=='price:'+code));db.execute(delete(Obs).where(Obs.series=='usdkrw',Obs.period==day))
            db.execute(delete(Status).where(Status.series=='usdkrw'))
            if previous_status:db.merge(previous_status)


def test_vintage_live_endpoint_uses_seen_cutoff(monkeypatch):
    from app import vintage_backtest as vb
    monkeypatch.setattr(vb,'DEMO',False)
    rows,revs=vintage_fixture();offset=24*2
    # Use isolated earlier observations; other test seed revisions were seen today.
    rows=[(month_before(d,offset),v) for d,v in rows]
    revs=[(month_before(d,offset),cutoff_for(month_before(month_before(d,offset),-1))-timedelta(days=1),v,i) for d,seen,v,i in revs]
    ids=[]
    with Session.begin() as db:
        for d,seen,v,i in revs:
            r=Revision(series='cli_us',period=d,value=v,seen=seen);db.add(r);db.flush();ids.append(r.id)
    try:
        csv='month,stock,bond,cash\n'+'\n'.join(f'{d:%Y-%m},'+','.join(map(str,v)) for d,v in rows)
        client=TestClient(app);assert client.post('/api/journal/backtest',auth=AUTH,json={'csv':csv}).status_code==403
        response=client.post('/api/journal/backtest',auth=AUTH,headers=HEAD,json={'csv':csv,'fee_bps':10})
        assert response.status_code==200,response.text
        assert response.json()['months']==12 and response.json()['points'][0]['phase']=='확장'
    finally:
        with Session.begin() as db:db.execute(delete(Revision).where(Revision.id.in_(ids)))
