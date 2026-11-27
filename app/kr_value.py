"""Annual company value research: DART filings, explicit dividend dates, scenario math.
Never substitute missing data with zero; do not use revised filings as point-in-time valuations.
"""
from datetime import date, timedelta
from math import isfinite
import re, time, csv, io
from pathlib import Path
from typing import Literal
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from . import kr_market as km
from .admin import require_admin, require_write
from .collect import LOCK, today
from .db import DEMO
from .settings import load_settings

router=APIRouter(tags=['kr-value'])
SCOPE=Literal['CFS','OFS']
RECEIPT=r'^\d{14}$'
SOURCE='https://dart.fss.or.kr/dsaf001/main.do?rcpNo='
FIELDS={
 'revenue':(('IS','CIS'),('Revenue',)),
 'operating_profit':(('IS','CIS'),('OperatingIncomeLoss',)),
 'net_income':(('IS','CIS'),('ProfitLoss',)),
 'parent_income':(('IS','CIS'),('ProfitLossAttributableToOwnersOfParent',)),
 'eps':(('IS','CIS'),('BasicEarningsLossPerShare',)),
 'equity':(('BS',),('Equity',)),
 'parent_equity':(('BS',),('EquityAttributableToOwnersOfParent',)),
 'ocf':(('CF',),('CashFlowsFromUsedInOperatingActivities',)),
 'ppe_purchase':(('CF',),('PurchaseOfPropertyPlantAndEquipment',)),
 'intangible_purchase':(('CF',),('PurchaseOfIntangibleAssets',)),
}
PREFIXES=('ifrs-full_','ifrs_','dart_')

def ratio(a,b,scale=1):
    if a is None or b is None or b<=0: return None
    value=a/b*scale
    return round(value,4) if isfinite(value) else None

def single(rows,field):
    values={v for r in rows if (v:=km.num(r.get(field))) is not None}
    return next(iter(values)) if len(values)==1 else None

def receipt(rows):
    ids={r.get('rcept_no') for r in rows if isinstance(r.get('rcept_no'),str) and re.fullmatch(RECEIPT,r['rcept_no'])}
    return next(iter(ids)) if len(ids)==1 else None

def parse_annual(raw,corp,year,scope):
    if not isinstance(raw,list) or len(raw)>10000: raise km.MarketError('연간 재무 응답 형식 오류')
    rows=[r for r in raw if isinstance(r,dict) and r.get('corp_code')==corp and str(r.get('bsns_year'))==str(year) and str(r.get('reprt_code'))=='11011' and r.get('currency')=='KRW']
    # Require a single report receipt; never mix amendments or separate report periods.
    rec=receipt(rows)
    if not rec or any(r.get('rcept_no')!=rec for r in rows): raise km.MarketError('연간 재무 공시 식별 불가')
    values={}; prev={}
    for k,(divs,ids) in FIELDS.items():
        selected=[r for r in rows if r.get('sj_div') in divs and r.get('account_id') in {p+i for p in PREFIXES for i in ids}]
        values[k]=single(selected,'thstrm_amount'); prev[k]=single(selected,'frmtrm_amount')
    if not any(values[k] is not None for k in ('revenue','operating_profit','net_income','eps')): raise km.MarketError('표준 KRW 연간 실적 확인 불가')
    ni='parent_income' if scope=='CFS' else 'net_income'; eq='parent_equity' if scope=='CFS' else 'equity'
    current,prior=values[eq],prev[eq]
    avg=(current+prior)/2 if current is not None and prior is not None and min(current,prior)>0 else None
    # Capex uses absolute reported purchase cash outflows. Missing components are not assumed zero.
    capex=abs(values['ppe_purchase'])+abs(values['intangible_purchase']) if values['ppe_purchase'] is not None and values['intangible_purchase'] is not None else None
    return {'year':year,'receipt':rec,'scope':scope,'values':values,'roe':ratio(values[ni],avg,100),'operating_margin':ratio(values['operating_profit'],values['revenue'],100),'capex':capex,'fcf':values['ocf']-capex if values['ocf'] is not None and capex is not None else None,'growth':{k: (None if (r:=ratio(values[k],prev[k],100)) is None else round(r-100,4)) for k in ('revenue','operating_profit','net_income','eps')}}

def compact(v): return re.sub(r'\s+','',str(v or ''))

def parse_dividend(raw,corp,year):
    if not isinstance(raw,list) or len(raw)>2000: raise km.MarketError('배당 응답 형식 오류')
    rows=[r for r in raw if isinstance(r,dict) and r.get('corp_code')==corp]
    rec=receipt(rows)
    if not rec or any(r.get('rcept_no')!=rec for r in rows): raise km.MarketError('배당 공시 식별 불가')
    # stlm_dt identifies annual closing date, not record date. Some issuers have non-Dec fiscal years.
    closes={r.get('stlm_dt') for r in rows if r.get('stlm_dt')}
    closing=None
    if len(closes)==1:
        try: closing=date.fromisoformat(next(iter(closes))).isoformat()
        except (ValueError,TypeError): pass
    common=[r for r in rows if compact(r.get('stock_knd')) in ('보통주','보통주식')]
    def field(rs,labels): return single([r for r in rs if compact(r.get('se')) in labels],'thstrm')
    dps=field(common,('주당현금배당금(원)','주당현금배당금'))
    yld=field(common,('현금배당수익률(%)','현금배당수익률'))
    # Company-level payout is separate from per-share data and may use different profit definitions.
    payout=field([r for r in rows if compact(r.get('stock_knd')) in ('','-')],('현금배당성향(%)','현금배당성향','연결현금배당성향(%)','연결현금배당성향','(연결)현금배당성향(%)'))
    def nonnegative(x): return x if x is not None and x>=0 else None
    if dps is None and yld is None and payout is None: raise km.MarketError('보통주 배당 표준 항목 확인 불가')
    return {'year':year,'receipt':rec,'closing':closing,'dps':nonnegative(dps),'reported_yield':nonnegative(yld),'reported_payout':payout}

def filings(client,cfg,corp,asof):
    """Recent filing links only, not inferred ex-dates or a complete calendar."""
    items=[]; seen=set(); complete=True
    for page in range(1,5):
        response=km.dart_get(client,cfg,'list',{'corp_code':corp,'bgn_de':(asof-timedelta(days=365)).strftime('%Y%m%d'),'end_de':asof.strftime('%Y%m%d'),'page_no':str(page),'page_count':'100','sort':'date','sort_mth':'desc'},allow_no_data=True)
        if response.get('status')=='013': break
        if not isinstance(response.get('list'),list): raise km.DartError('format','list')
        for row in response['list']:
            if not isinstance(row,dict) or row.get('corp_code')!=corp: continue
            n=row.get('rcept_no'); d=km.day_value(row.get('rcept_dt')); title=str(row.get('report_nm') or '')[:180]
            if not isinstance(n,str) or not re.fullmatch(RECEIPT,n) or n in seen or not d or d>asof or '배당' not in title: continue
            seen.add(n); items.append({'receipt':n,'date':str(d),'title':title,'remark':str(row.get('rm') or '')[:50]})
        pages=km.num(response.get('total_page'))
        if pages is None: complete=False; break
        if page>=pages: break
        if page==4: complete=False
        time.sleep(.15)
    return {'items':items[:100],'complete':complete and len(items)<=100}

def identity(code):
    found=next((r for r in km.watches() if r['code']==code),None)
    if found is None: raise HTTPException(404,'국내 증시에서 관심종목을 먼저 등록하세요.')
    return found

def key(kind,code,scope='CFS',year=None): return f'value:{kind}:{code}:{scope}'+(f':{year}' if year is not None else '')

def payload(k):
    x=km.cached(k); rows=x.pop('rows'); return {**x,'data':rows[0] if rows else None}

def demo_history(year,scope):
    a=year-2015; rev=(25+a*2)*10**12; op=rev*(.07+a%4*.01); net=op*.72; eps=3200+a*410
    return {'year':year,'receipt':None,'scope':scope,'values':{'revenue':rev,'operating_profit':op,'net_income':net,'parent_income':net,'eps':eps,'ocf':op*1.3,'equity':rev*.7,'parent_equity':rev*.65},'operating_margin':op/rev*100,'roe':net/(rev*.65)*100,'fcf':op*.9,'capex':op*.4,'growth':{} }

def available_years(count): return list(range(max(2015,today().year-count),today().year))

@router.get('/kr-value',include_in_schema=False,dependencies=[Depends(require_admin)])
def page(): return FileResponse(Path(__file__).parent/'static'/'kr-value.html')

@router.get('/api/kr-value',dependencies=[Depends(require_admin)])
def dashboard(code:str=Query(pattern=r'^\d{6}$'),scope:SCOPE='CFS',years:int=Query(5,ge=5,le=10)):
    if years not in (5,10): raise HTTPException(422,'5년 또는 10년 선택')
    watch=identity(code); history=[]
    for year in available_years(years):
        if DEMO:
            f={'state':'demo','data':demo_history(year,scope),'updated':None}; d={'state':'demo','data':{'year':year,'dps':500+(year-2015)*160,'reported_yield':None,'reported_payout':None,'receipt':None,'closing':None},'updated':None}
        else:
            f=payload(key('financial',code,scope,year)); d=payload(key('dividend',code,'COMMON',year))
        history.append({'year':year,'financial':f,'dividend':d})
    price,_=km.watch_data(code); last=price['rows'][-1] if price['rows'] else None
    events=payload(key('events',code,'COMMON')); schedule=payload(key('schedule',code,'COMMON'))
    multiples=payload(key('multiples',code,'COMMON'))
    return {'demo':DEMO,'watch':watch,'history':history,'multiples':multiples,'scope':scope,'price':last,'price_stale':price.get('stale',True),'events':events,'schedule':schedule,'configured':bool(load_settings()['DART_API_KEY']),'asof':str(today())}

class Refresh(BaseModel):
    code:str=Field(pattern=r'^\d{6}$')
    scope:SCOPE='CFS'
    years:Literal[5,10]=5
    force:bool=False

LAST={}
@router.post('/api/kr-value/refresh',dependencies=[Depends(require_write)])
def refresh(req:Refresh):
    if DEMO: raise HTTPException(409,'데모 모드에서는 외부 API를 호출하지 않습니다.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 수집 작업 중입니다.')
    try:
        watch=identity(req.code); cfg=load_settings()
        if not cfg['DART_API_KEY']: raise HTTPException(400,'관리자에서 OpenDART 키를 등록하세요.')
        clock=time.monotonic()
        if clock-LAST.get(req.code,-1e9)<30: raise HTTPException(429,'30초 후 다시 요청하세요.')
        if len(LAST)>100: LAST.clear()
        LAST[req.code]=clock
        results=[]
        with httpx.Client(timeout=httpx.Timeout(12,connect=5),follow_redirects=False) as c:
            try:
                corp=watch['corp_code'] or km.resolve_corp(c,cfg,req.code)
                company=km.dart_get(c,cfg,'company',{'corp_code':corp})
                if str(company.get('stock_code','')).strip()!=req.code: raise km.MarketError('DART 기업 고유번호와 종목 코드 불일치')
            except km.MarketError as e: raise HTTPException(400,str(e)) from None
            except Exception: raise HTTPException(502,'기업 확인 중 연결 오류') from None
            jobs=[]
            for year in reversed(available_years(req.years)):
                params={'corp_code':corp,'bsns_year':str(year),'reprt_code':'11011'}
                jobs.extend([(key('financial',req.code,req.scope,year),lambda p=params,y=year:parse_annual(km.dart_get(c,cfg,'fnlttSinglAcntAll',{**p,'fs_div':req.scope}).get('list'),corp,y,req.scope)),(key('dividend',req.code,'COMMON',year),lambda p=params,y=year:parse_dividend(km.dart_get(c,cfg,'alotMatter',p).get('list'),corp,y))])
            jobs.append((key('events',req.code,'COMMON'),lambda:filings(c,cfg,corp,today())))
            # Successful historic responses cached for up to 24 hours. Force permits amendments to be refreshed.
            deadline=time.monotonic()+80
            blocked=None
            for k,fetch in jobs:
                old=payload(k)
                if not req.force and old['state']=='ok' and old['updated'] and old['updated'][:10]==str(today()):
                    results.append({'key':k,'state':'cached'}); continue
                if blocked:
                    results.append({'key':k,'state':'skipped','message':blocked}); continue
                if time.monotonic()>deadline:
                    results.append({'key':k,'state':'skipped','message':'요청 시간 제한 · 다시 수집하세요.'}); continue
                try:
                    data=fetch(); km.store(k,[data],merge=False); results.append({'key':k,'state':'ok'})
                except Exception as e:
                    msg=str(e) if isinstance(e,km.MarketError) else '공시 연결/응답 오류'
                    km.store(k,error=msg); results.append({'key':k,'state':'error','message':msg})
                    if isinstance(e,km.DartError) and e.fatal: blocked='후속 조회 중단 · '+msg
                time.sleep(.15)
        return {'results':results}
    finally: LOCK.release()

class DividendEvent(BaseModel):
    receipt:str=Field(pattern=RECEIPT)
    record_date:date|None=None
    ex_date:date|None=None
    pay_date:date|None=None
    dps:float|None=Field(default=None,ge=0,le=1e9,allow_inf_nan=False)
    status:Literal['예정','확정','취소']='예정'
    @model_validator(mode='after')
    def validate_dates(self):
        if not any((self.record_date,self.ex_date,self.pay_date)): raise ValueError('날짜 한 개 이상 필요')
        if self.record_date and self.ex_date and self.ex_date>self.record_date: raise ValueError('배당락일은 기준일 이후일 수 없습니다.')
        if self.record_date and self.pay_date and self.pay_date<self.record_date: raise ValueError('지급일은 기준일 이전일 수 없습니다.')
        if self.ex_date and self.pay_date and self.pay_date<self.ex_date: raise ValueError('지급일은 배당락일 이전일 수 없습니다.')
        if any(d and not 2015<=d.year<=today().year+2 for d in (self.record_date,self.ex_date,self.pay_date)): raise ValueError('날짜 범위 오류')
        return self

class ScheduleRequest(BaseModel):
    code:str=Field(pattern=r'^\d{6}$')
    events:list[DividendEvent]=Field(max_length=100)
    confirmed:bool=False
    expected_updated:str|None=Field(default=None,max_length=40)

@router.put('/api/kr-value/schedule',dependencies=[Depends(require_write)])
def update_schedule(req:ScheduleRequest):
    if DEMO: raise HTTPException(409,'데모에서는 일정 저장을 하지 않습니다.')
    if not req.confirmed: raise HTTPException(400,'공시 원문과 수동 입력 확인이 필요합니다.')
    if len({x.receipt for x in req.events})!=len(req.events): raise HTTPException(422,'중복 접수번호')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 작업 중입니다.')
    try:
        identity(req.code); k=key('schedule',req.code,'COMMON')
        if payload(k)['updated']!=req.expected_updated: raise HTTPException(409,'다른 화면에서 일정이 변경됐습니다. 다시 조회하세요.')
        km.store(k,[{'items':[x.model_dump(mode='json') for x in req.events],'origin':'manual_unverified'}],merge=False)
        return {'ok':True}
    finally: LOCK.release()

def parse_multiples(text,asof):
    """User-verified as-of per-share fundamentals. Not a point-in-time data provider."""
    try:
        reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')),strict=True)
        if reader.fieldnames!=['date','price','eps','bps']: raise ValueError('CSV 헤더: date,price,eps,bps')
        out=[]; seen=set()
        for row in reader:
            if len(out)>=400 or None in row or any(v is None for v in row.values()): raise ValueError('최대 400행 · 열 수 확인')
            d=date.fromisoformat(row['date']);
            if str(d)!=row['date'] or d>=asof or d.year<2015 or d.weekday()>=5 or d in seen: raise ValueError('완료 평일 날짜, 중복 및 범위 확인')
            seen.add(d)
            price=km.num(row['price']); eps=km.num(row['eps']); bps=km.num(row['bps'])
            if price is None or not 0.000001<=price<=1e9: raise ValueError('유효한 양수 주가 필요')
            if row['eps'].strip() and (eps is None or abs(eps)>1e9): raise ValueError('EPS 범위 오류')
            if row['bps'].strip() and (bps is None or not 0.000001<=bps<=1e9): raise ValueError('BPS 범위 오류')
            if eps is None and bps is None: raise ValueError('EPS 또는 BPS 필요')
            out.append({'date':str(d),'price':price,'eps':eps,'bps':bps,'per':ratio(price,eps),'pbr':ratio(price,bps)})
        if not out: raise ValueError('CSV 자료 없음')
        return sorted(out,key=lambda x:x['date'])
    except (csv.Error,OverflowError): raise ValueError('CSV 형식 오류') from None

class MultiplesRequest(BaseModel):
    code:str=Field(pattern=r'^\d{6}$')
    csv_text:str=Field(min_length=1,max_length=160000)
    source_note:str=Field(min_length=1,max_length=300)
    confirmed:bool=False
    expected_updated:str|None=Field(default=None,max_length=40)

@router.put('/api/kr-value/multiples',dependencies=[Depends(require_write)])
def update_multiples(req:MultiplesRequest):
    if DEMO: raise HTTPException(409,'데모에서는 자료를 등록하지 않습니다.')
    if not req.confirmed or not req.source_note.strip(): raise HTTPException(400,'시점별 자료 및 출처 확인이 필요합니다.')
    try: rows=parse_multiples(req.csv_text,today())
    except ValueError as e: raise HTTPException(422,str(e)) from None
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 작업 중입니다.')
    try:
        identity(req.code); k=key('multiples',req.code,'COMMON'); old=payload(k)
        if old['updated']!=req.expected_updated: raise HTTPException(409,'다른 화면에서 자료가 변경됐습니다. 다시 조회하세요.')
        # Explicit replace avoids silently mixing inconsistent denominator definitions.
        km.store(k,[{'items':rows,'source_note':req.source_note.strip(),'origin':'manual_unverified'}],merge=False)
        return {'ok':True,'count':len(rows)}
    finally: LOCK.release()

class Scenario(BaseModel):
    price:float=Field(ge=0.000001,le=1e9,allow_inf_nan=False)
    eps:float=Field(ge=-1e9,le=1e9,allow_inf_nan=False)
    bps:float|None=Field(default=None,ge=0.000001,le=1e9,allow_inf_nan=False)
    dps:float|None=Field(default=None,ge=0,le=1e9,allow_inf_nan=False)
    per:list[float]=Field(min_length=3,max_length=3)
    pbr:list[float]=Field(min_length=3,max_length=3)
    operating_profit:float|None=Field(default=None,ge=-1e18,le=1e18,allow_inf_nan=False)
    invested_capital:float|None=Field(default=None,ge=0.000001,le=1e18,allow_inf_nan=False)
    tax:float=Field(default=25,ge=0,le=100,allow_inf_nan=False)
    capital:float=Field(default=10000000,gt=0,le=1e14,allow_inf_nan=False)
    dividend_yield:float=Field(default=3,ge=0,le=50,allow_inf_nan=False)
    price_return:float=Field(default=3,gt=-100,le=100,allow_inf_nan=False)
    dividend_tax:float=Field(default=15.4,ge=0,le=100,allow_inf_nan=False)
    years:int=Field(default=10,ge=1,le=30)
    @model_validator(mode='after')
    def ordered(self):
        for values in (self.per,self.pbr):
            if any(not isfinite(x) or not 0<x<=1000 for x in values) or values!=sorted(values): raise ValueError('배수는 양수 오름차순이어야 합니다.')
        return self

@router.post('/api/kr-value/scenario',dependencies=[Depends(require_write)])
def scenario(x:Scenario):
    # Explicit annual ex-dividend price return plus constant beginning-of-year dividend yield.
    # Cash dividends received at year-end; fractional shares, cash interest/fees ignored.
    dy=x.dividend_yield/100*(1-x.dividend_tax/100); pr=x.price_return/100
    invested=x.capital; shares_value=x.capital; cash=0; points=[]
    for n in range(1,x.years+1):
        invested*=1+pr+dy; cash+=shares_value*dy; shares_value*=1+pr
        points.append({'year':n,'reinvest':round(invested,2),'cash_total':round(shares_value+cash,2)})
    return {'per':ratio(x.price,x.eps),'pbr':ratio(x.price,x.bps),'yield':ratio(x.dps,x.price,100),'payout':ratio(x.dps,x.eps,100),'roic':ratio(x.operating_profit*(1-x.tax/100) if x.operating_profit is not None else None,x.invested_capital,100),'per_prices':[round(x.eps*m,2) for m in x.per] if x.eps>0 else None,'pbr_prices':[round(x.bps*m,2) for m in x.pbr] if x.bps else None,'points':points}
