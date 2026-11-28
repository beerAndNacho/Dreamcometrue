from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from app.signal_validation import evaluate
from app.sector_diagnosis import ratio_yoy
from app.main import app
from fastapi.testclient import TestClient

def snapshot(d,p,v,*,state='경계',version='experimental-v1',demo=False,identity='FRED:SP500:',created=None):
    return {'asof':str(d),'generated_at':datetime.combine(created or d,datetime.min.time(),tzinfo=ZoneInfo('Asia/Seoul')).isoformat(),'demo':demo,'market_diagnosis':{'version':version,'domains':[{'id':'growth','state':state,'available':3,'total':3}]},'indicators':{'sp500':{'period':str(p),'value':v,'fresh':True,'source_identity':identity}}}

def growth(rows,end,horizon=30):
    return evaluate(rows,end,False,horizon)['domains'][0]

def test_forward_saved_new_price_and_overlap():
    start=date(2026,1,1);a=start+timedelta(days=1);b=a+timedelta(days=30)
    rows=[snapshot(start,start,90),snapshot(a,a,100),snapshot(a+timedelta(days=1),a,999),snapshot(b,b,110)]
    r=growth(rows,b+timedelta(days=15))
    assert r['n']==1 and r['samples'][0]['entry_period']==str(a)
    assert r['samples'][0]['price_change_pct']==10
    assert r['overlap_excluded']==3 and r['stats'][0]['mean_pct'] is None

def test_missing_versions_mismatch_and_pending():
    d=date(2026,1,1);a=d+timedelta(days=1);b=a+timedelta(days=30)
    assert growth([snapshot(d,d,100),snapshot(a,a,100)],a)['pending']>=1
    assert growth([snapshot(d,d,100),snapshot(a,a,100,identity='A'),snapshot(b,b,110,identity='B')],b+timedelta(days=20))['n']==0
    assert growth([snapshot(d,d,100,version='old'),snapshot(a,a,100,demo=True),snapshot(b,b,100,created=b+timedelta(days=1))],b+timedelta(days=20))['signals']==0

def test_ratio_same_month_and_no_zero():
    d=date(2026,10,1);old=date(2025,10,1);asof=date(2026,11,10)
    rows={'electronics_inventory':[(old,100),(d,150)],'electronics_shipments':[(old,50),(d,50)]}
    x=ratio_yoy(rows,asof)
    assert x['value']==1 and x['current_ratio']==3 and x['previous_ratio']==2
    rows['electronics_shipments'][-1]=(d,0)
    assert ratio_yoy(rows,asof)['value'] is None
    assert ratio_yoy(rows,asof,True)['value'] is None

def test_endpoint_and_dashboard():
    with TestClient(app) as c:
        assert c.get('/api/diagnosis/validation?horizon=7').status_code==422
        r=c.get('/api/diagnosis/validation?horizon=90').json()
        assert r['demo'] and r['horizon_days']==90 and len(r['domains'])==7
        d=c.get('/api/dashboard').json()
        assert len(d['semiconductor']['groups'])==5
        assert len(d['diagnostics'])==5
