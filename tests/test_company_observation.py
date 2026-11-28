from datetime import date,timedelta
from app.company_observation import metrics,usable_rows
D=date(2026,11,28)
def rows():
    return [{'date':str(D-timedelta(days=60-i)), 'close':100+i,'foreign':2,'institution':-1,'individual':0} for i in range(60)]
def stored(r):return {'state':'ok','stale':False,'rows':r}
def annual(rec='20260301000001',eps=10,dps=2):
    return [{'year':2025,'financial':{'state':'ok','data':{'year':2025,'scope':'CFS','receipt':rec,'values':{'eps':eps},'growth':{'revenue':4,'operating_profit':-1},'operating_margin':5,'roe':8,'fcf':100}},'dividend':{'state':'ok','data':{'year':2025,'receipt':rec,'dps':dps}}}]
def items(**kw):
    a={'asof':D,'history':annual(),'price':stored(rows()),'flow':stored(rows())};a.update(kw)
    return {x['id']:x['value'] for g in metrics(**a)['groups'] for x in g['items']}
def test_valid():
    x=items()
    assert x['company_annual_multiple']==15.9
    assert x['company_simple_payout']==20
    assert x['company_foreign_20']==40 and x['company_institution_60']==-60 and x['company_individual_5']==0
    assert x['company_price_change_5']==2.580645
def test_date_and_loss():
    x=items(history=annual('20261127000001'))
    assert x['company_annual_multiple'] is None and x['company_historical_yield'] is None
    x=items(history=annual(eps=-1,dps=0))
    assert x['company_annual_multiple'] is None and x['company_simple_payout'] is None and x['company_historical_yield']==0
def test_missing_stale_busy():
    r=rows();r[-1]['foreign']=None
    assert items(flow=stored(r))['company_foreign_5'] is None
    assert items(flow=stored(r))['company_institution_5']==-5
    r[-2]['date']=r[-1]['date'];assert usable_rows(stored(r),D,False,5) is None
    assert items(scope='OFS')['company_eps'] is None
    assert items(history=annual('20261201000001'))['company_eps'] is None
    assert items(price={'state':'error','stale':True,'rows':rows()})['company_close'] is None
    assert all(v is None for v in items(busy=True).values())
