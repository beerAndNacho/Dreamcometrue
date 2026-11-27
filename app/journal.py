"""Single-owner KRW investment journal. Explicit trades, cash flows and dated marks.
No broker integration, tax accounting or forward-filled valuations.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import update, select
from sqlalchemy.exc import IntegrityError
from .admin import require_admin, require_write
from .collect import today
from .db import Base, Session, DEMO, now, Obs, Status, MarketCache, MarketWatch
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Integer, Text, DateTime
from datetime import datetime
import json
import math
import re

router=APIRouter(tags=['journal'])
D=Decimal
ZERO=D(0)
ONE=D(1)
CENT=D('.01')
MAX_VALUE=D('1e15')
Money=Annotated[Decimal,Field(ge=0,le=MAX_VALUE,max_digits=21,decimal_places=6,allow_inf_nan=False)]
Quantity=Annotated[Decimal,Field(ge=0,le=D('1e9'),max_digits=15,decimal_places=6,allow_inf_nan=False)]
FX=Annotated[Decimal,Field(gt=0,le=D('100000'),max_digits=11,decimal_places=6,allow_inf_nan=False)]
Currency=Literal['KRW','USD','GBP']
Kind=Literal['deposit','withdraw','buy','sell','dividend','expense']
Symbol=Annotated[str,Field(max_length=40,pattern=r'^[A-Z0-9._-]*$')]

class JournalStore(Base):
    __tablename__='investment_journal'
    id:Mapped[int]=mapped_column(Integer,primary_key=True)
    revision:Mapped[int]=mapped_column(Integer)
    payload:Mapped[str]=mapped_column(Text)
    updated:Mapped[datetime]=mapped_column(DateTime)

class Entry(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id:str=Field(min_length=1,max_length=40,pattern=r'^[A-Za-z0-9_-]+$')
    day:date
    kind:Kind
    symbol:Symbol=''
    currency:Currency='KRW'
    quantity:Quantity=ZERO
    price:Money=ZERO
    amount:Money=ZERO
    fee:Money=ZERO
    fx:FX=ONE
    memo:str=Field(default='',max_length=300)

    @model_validator(mode='after')
    def validate_kind(self):
        if self.day>today() or self.day<date(1900,1,1):raise ValueError('거래일 범위 오류')
        if self.currency=='KRW' and self.fx!=ONE:raise ValueError('원화 환율은 1')
        if self.currency!='KRW' and 'fx' not in self.model_fields_set:raise ValueError('외화 환율 직접 입력 필요')
        if self.kind in ('buy','sell'):
            if not self.symbol or self.quantity<=0 or self.price<=0 or self.amount!=0:raise ValueError('종목, 수량, 체결가 필요')
            if self.kind=='sell' and self.fee>=self.quantity*self.price:raise ValueError('매도 비용이 매도 대금 이상')
        elif self.kind=='dividend':
            if not self.symbol or self.amount<=0 or self.quantity!=0 or self.price!=0 or self.fee>self.amount:raise ValueError('배당 총액 및 원천징수 확인')
        elif not (self.amount>0 and self.currency=='KRW' and self.fx==ONE and self.quantity==self.price==self.fee==0 and not self.symbol):
            raise ValueError('원화 입출금/기타 비용은 금액만 입력')
        return self

class Mark(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    day:date
    symbol:Symbol=Field(min_length=1)
    currency:Currency
    price:Money=Field(gt=0)
    fx:FX=ONE
    @model_validator(mode='after')
    def validate_mark(self):
        if not date(1900,1,1)<=self.day<=today():raise ValueError('평가일 범위 오류')
        if self.currency=='KRW' and self.fx!=ONE:raise ValueError('원화 환율은 1')
        if self.currency!='KRW' and 'fx' not in self.model_fields_set:raise ValueError('외화 환율 직접 입력 필요')
        return self

class Book(BaseModel):
    model_config=ConfigDict(extra='forbid')
    entries:list[Entry]=Field(default_factory=list,max_length=800)
    marks:list[Mark]=Field(default_factory=list,max_length=3000)
    valuation_days:list[date]=Field(default_factory=list,max_length=500)
    @model_validator(mode='after')
    def unique(self):
        if len({x.id for x in self.entries})!=len(self.entries):raise ValueError('거래 ID 중복')
        if len({(x.day,x.symbol,x.currency) for x in self.marks})!=len(self.marks):raise ValueError('평가 가격 중복')
        if len(set(self.valuation_days))!=len(self.valuation_days) or any(not date(1900,1,1)<=d<=today() for d in self.valuation_days):raise ValueError('평가일 중복 또는 범위 오류')
        if any(x.day not in self.valuation_days for x in self.marks):raise ValueError('평가일 미등록')
        return self

class SaveBook(Book):
    revision:int=Field(ge=0,le=2147483646)

class LedgerError(ValueError):pass

def won(v):
    if not v.is_finite() or abs(v)>MAX_VALUE:raise LedgerError('원화 계산 범위를 초과했습니다.')
    v=v.quantize(CENT,rounding=ROUND_HALF_UP)
    return v

def raw(v):return format(v,'f')
def pos_key(row):return (row.symbol,row.currency)

class Ledger:
    def __init__(self):
        self.positions={};self.cash=ZERO;self.contributed=ZERO;self.realized=ZERO;self.realized_fx=ZERO;self.dividend=ZERO;self.expense=ZERO;self.months=defaultdict(lambda:dict(deposit=ZERO,withdraw=ZERO,realized=ZERO,dividend=ZERO,expense=ZERO));self.flows=defaultdict(Decimal)

    def apply(self,x):
        month=self.months[x.day.strftime('%Y-%m')];key=pos_key(x)
        gross=won(x.amount if x.kind in ('deposit','withdraw','expense') else (x.amount if x.kind=='dividend' else x.quantity*x.price)*x.fx)
        fee=won(x.fee*x.fx)
        pnl=ZERO;fx_pnl=ZERO
        if x.kind=='deposit':
            if gross<=0:raise LedgerError('입금액이 원화 최소 단위 미만입니다.')
            self.cash+=gross;self.contributed+=gross;self.flows[x.day]+=gross;month['deposit']+=gross
        elif x.kind=='withdraw':
            if gross<=0:raise LedgerError('출금액이 원화 최소 단위 미만입니다.')
            self.cash-=gross;self.contributed-=gross;self.flows[x.day]-=gross;month['withdraw']+=gross
        elif x.kind=='expense':
            if gross<=0:raise LedgerError('비용이 원화 최소 단위 미만입니다.')
            self.cash-=gross;self.expense+=gross;month['expense']+=gross
        elif x.kind=='buy':
            if gross<=0:raise LedgerError('매수대금이 원화 최소 단위 미만입니다.')
            q,base,local=self.positions.get(key,(ZERO,ZERO,ZERO));cost=won(gross+fee)
            self.positions[key]=(q+x.quantity,won(base+cost),local+x.quantity*x.price+x.fee)
            self.cash-=cost
        elif x.kind=='sell':
            q,base,local=self.positions.get(key,(ZERO,ZERO,ZERO))
            if x.quantity>q:raise LedgerError('매도 수량이 보유 수량을 초과합니다. 거래 순서와 날짜를 확인하세요.')
            proceeds=won(gross-fee)
            if proceeds<=0:raise LedgerError('순매도대금이 0 이하입니다.')
            # Moving average: retain exact residual base cost, release all on full exit.
            part=base if x.quantity==q else won(base*x.quantity/q)
            local_part=local if x.quantity==q else local*x.quantity/q
            pnl=won(proceeds-part);fx_pnl=ZERO if x.currency=='KRW' else won(local_part*x.fx-part)
            if x.quantity==q:del self.positions[key]
            else:self.positions[key]=(q-x.quantity,base-part,local-local_part)
            self.cash+=proceeds;self.realized+=pnl;self.realized_fx+=fx_pnl;month['realized']+=pnl
        else:
            net=won(gross-fee);self.cash+=net;self.dividend+=net;month['dividend']+=net
        if self.cash<0:raise LedgerError('원화 현금 잔액이 부족합니다. 입금 또는 거래 순서를 확인하세요.')
        for v in (self.cash,self.contributed,self.realized,self.realized_fx,self.dividend,self.expense):won(v)
        return {'id':x.id,'cash':raw(self.cash),'realized':raw(pnl),'fx_component':raw(fx_pnl)}

    def valuation(self,marks):
        by_key={pos_key(m):m for m in marks};positions=[];missing=[];total=self.cash;unrealized=ZERO;fx_component=ZERO
        for key,(qty,base,local) in sorted(self.positions.items()):
            m=by_key.get(key);value=None;pnl=None;fx=None
            if m is None:missing.append('/'.join(key))
            else:
                value=won(qty*m.price*m.fx);pnl=won(value-base);fx=ZERO if key[1]=='KRW' else won(local*m.fx-base)
                total+=value;unrealized+=pnl;fx_component+=fx
            positions.append({'symbol':key[0],'currency':key[1],'quantity':raw(qty),'cost_krw':raw(base),'average_local':raw((local/qty).quantize(D('.000001'),rounding=ROUND_HALF_UP)),'price':raw(m.price) if m else None,'fx':raw(m.fx) if m else None,'value':raw(value) if value is not None else None,'unrealized':raw(pnl) if pnl is not None else None,'fx_component':raw(fx) if fx is not None else None})
        extra=sorted('/'.join(k) for k in by_key if k not in self.positions)
        complete=not missing
        return {'cash':raw(self.cash),'contributed':raw(self.contributed),'realized':raw(self.realized),'realized_fx':raw(self.realized_fx),'dividend':raw(self.dividend),'expense':raw(self.expense),'positions':positions,'missing':missing,'unused_marks':extra,'equity':raw(won(total)) if complete else None,'unrealized':raw(won(unrealized)) if complete else None,'unrealized_fx':raw(won(fx_component)) if complete else None,'total_profit':raw(won(total-self.contributed)) if complete else None}


def evaluate(book,asof):
    with localcontext() as ctx:
        ctx.prec=48
        return _evaluate(book,asof)

def _evaluate(book,asof):
    # Replay every entry (including entries after selected as-of) to validate edits atomically.
    ledger=Ledger();events=defaultdict(list);marks=defaultdict(list)
    for x in book.entries:events[x.day].append(x)
    for m in book.marks:marks[m.day].append(m)
    days=sorted(set(events)|set(book.valuation_days)|{asof});valuations=[];entries=[];current=None;months={};flows={}
    for day in days:
        for x in events[day]:
            result=ledger.apply(x)
            if day<=asof:entries.append(result)
        if day in book.valuation_days and day<=asof:
            result=ledger.valuation(marks[day]);valuations.append({'day':str(day),'equity':result['equity'],'cash':result['cash'],'missing':result['missing'],'unused_marks':result['unused_marks'],'total_profit':result['total_profit']})
        if day==asof:
            current=ledger.valuation(marks[day]);months={k:{a:raw(b) for a,b in v.items()} for k,v in sorted(ledger.months.items())};flows=dict(ledger.flows)
    return {'asof':str(asof),'current':current,'valuations':valuations,'entries_calculated':entries,'months':months,'performance':performance(valuations,flows)}


def performance(valuations,flows):
    # Mark-to-mark returns. On ending valuation date, assume external flows occur at start of day.
    # Do not bridge an unvalued external-flow day or incomplete recorded snapshot.
    index=ONE;peak=ONE;mdd=ZERO;points=[];reason=None;prev=None;intervals=0
    for row in valuations:
        d=date.fromisoformat(row['day']);v=D(row['equity']) if row['equity'] is not None else None
        if v is None:
            if prev is not None:reason='평가 가격 누락으로 전체 기간 수익률을 계산할 수 없습니다.'
            points.append({'day':row['day'],'index':None});continue
        if prev is None:
            if v<=0:
                points.append({'day':row['day'],'index':None});continue
            prev=(d,v);points.append({'day':row['day'],'index':100.0});continue
        old,oldv=prev
        if any(old<k<d and flow!=0 for k,flow in flows.items()):reason='평가일 사이 입출금이 있습니다. 입출금일의 종가 평가를 추가하세요.'
        denominator=oldv+flows.get(d,ZERO)
        if denominator<=0 or v<=0:reason='전액 출금/0원 평가 이후 연속 수익률은 보류합니다.'
        if reason is None:
            index*=v/denominator
            if not D('1e-12')<=index<=D('1e12'):reason='수익률 계산 범위 초과'
            else:
                peak=max(peak,index);mdd=min(mdd,index/peak-ONE);intervals+=1
        points.append({'day':row['day'],'index':float(index*100) if reason is None else None});prev=(d,v)
    if not intervals and reason is None:reason='완전한 평가 기록이 2개 이상 필요합니다.'
    return {'return_pct':float((index-ONE)*100) if reason is None else None,'mdd_pct':float(mdd*100) if reason is None else None,'points':points,'reason':reason,'intervals':intervals,'note':'기록된 평가일만 사용. 입출금은 해당 평가일 시작 시점 가정. 장중 변동과 누락일 낙폭은 반영되지 않는 근사 성과입니다.'}


def benchmarks(db,performance_data):
    pts=[p for p in performance_data['points'] if p['index'] is not None]
    if performance_data['reason'] or len(pts)<2:return []
    ds=[date.fromisoformat(p['day']) for p in pts];start,end=ds[0],ds[-1]
    # Exact calendar dates only, price indices (not total returns).
    fx=dict(db.execute(select(Obs.period,Obs.value).where(Obs.series=='usdkrw',Obs.period.in_([start,end]))).tuples().all())
    fa,fb=fx.get(start),fx.get(end)
    fx_valid=fa is not None and fb is not None and 0<fa<1e5 and 0<fb<1e5
    out=[]
    for sid,name in [('kospi','KOSPI (KRW)'),('sp500','S&P 500 (USD)'),('nasdaq','NASDAQ (USD)')]:
        vals=dict(db.execute(select(Obs.period,Obs.value).where(Obs.series==sid,Obs.period.in_([start,end]))).tuples().all());a=vals.get(start);b=vals.get(end)
        ret=None
        if a is not None and b is not None and 0<a<1e15 and 0<b<1e15:
            ret=(b/a-1)*100
        krw_ret=ret if sid=='kospi' else ((1+ret/100)*fb/fa-1)*100 if ret is not None and fx_valid else None
        out.append({'id':sid,'name':name,'start':str(start),'end':str(end),'return_pct':ret,'krw_return_pct':krw_ret,'note':'가격지수, 배당 제외. 원화 환산은 동일 달력일 ECOS 원/달러 매매기준율 사용. 동시각 관측은 아님.'})
    return out


def load(db):
    row=db.get(JournalStore,1)
    return (Book.model_validate_json(row.payload),row.revision) if row else (Book(),0)


def output(db,book,revision,asof):
    result=evaluate(book,asof);result['benchmarks']=benchmarks(db,result['performance'])
    return {'demo':DEMO,'revision':revision,'book':book.model_dump(mode='json'),**result}

@router.get('/journal',include_in_schema=False,dependencies=[Depends(require_admin)])
def page():return FileResponse(Path(__file__).parent/'static'/'journal.html',headers={'Cache-Control':'no-store'})

@router.get('/api/journal',dependencies=[Depends(require_admin)])
def get_book(asof:date|None=Query(None)):
    asof=asof or today()
    if not date(1900,1,1)<=asof<=today():raise HTTPException(400,'평가일 범위 확인')
    with Session() as db:
        book,revision=load(db)
        try:return output(db,book,revision,asof)
        except LedgerError as e:raise HTTPException(400,str(e)) from None

@router.put('/api/journal',dependencies=[Depends(require_write)])
def put_book(req:SaveBook):
    book=Book.model_validate(req.model_dump(exclude={'revision'}))
    try:
        # Validate every dated transaction and every valuation before changing storage.
        evaluate(book,today())
        with Session.begin() as db:
            if req.revision==0:
                db.add(JournalStore(id=1,revision=1,payload=book.model_dump_json(),updated=now()));db.flush()
            else:
                result=db.execute(update(JournalStore).where(JournalStore.id==1,JournalStore.revision==req.revision).values(revision=req.revision+1,payload=book.model_dump_json(),updated=now()))
                if result.rowcount!=1:raise HTTPException(409,'다른 화면에서 기록이 변경되었습니다. 다시 불러오세요.')
            return output(db,book,req.revision+1,today())
    except IntegrityError:raise HTTPException(409,'다른 화면에서 기록이 변경되었습니다. 다시 불러오세요.') from None
    except LedgerError as e:raise HTTPException(400,str(e)) from None

@router.get('/api/journal/export',dependencies=[Depends(require_admin)])
def export_book():
    with Session() as db:book,revision=load(db)
    return Response(json.dumps({'format':'macro-invest-journal-v1','demo':DEMO,'revision':revision,'book':book.model_dump(mode='json')},ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="macro-invest-journal.json"','Cache-Control':'no-store'})


class JournalExport(BaseModel):
    model_config=ConfigDict(extra='forbid')
    format:Literal['macro-invest-journal-v1']
    demo:bool=Field(strict=True)
    revision:int=Field(ge=0,le=2147483647)
    book:Book

class JournalImport(BaseModel):
    model_config=ConfigDict(extra='forbid')
    archive:JournalExport
    expected_revision:int=Field(ge=0,le=2147483646)
    confirmed:bool=Field(default=False,strict=True)

def inspect_import(req):
    if req.archive.demo!=DEMO:raise HTTPException(400,'데모와 운영 장부는 서로 가져올 수 없습니다.')
    with Session() as db:
        _,revision=load(db)
    if revision!=req.expected_revision:raise HTTPException(409,'다른 화면에서 기록이 변경되었습니다. 다시 불러오세요.')
    book=req.archive.book
    try:result=evaluate(book,today())
    except LedgerError as e:raise HTTPException(400,str(e)) from None
    return {'entries':len(book.entries),'marks':len(book.marks),'valuation_days':len(book.valuation_days),'cash':result['current']['cash'],'equity':result['current']['equity'],'expected_revision':revision}

@router.post('/api/journal/import/preview',dependencies=[Depends(require_write)])
def preview_import(req:JournalImport):return inspect_import(req)

@router.put('/api/journal/import',dependencies=[Depends(require_write)])
def import_book(req:JournalImport):
    if not req.confirmed:raise HTTPException(400,'기존 장부 전체 교체 확인이 필요합니다.')
    inspect_import(req)
    return put_book(SaveBook(**req.archive.book.model_dump(),revision=req.expected_revision))


@router.get('/api/journal/mark-hint',dependencies=[Depends(require_admin)])
def mark_hint(day:date,symbol:Symbol,currency:Currency):
    """Suggestions only: no API call, no implicit save, no carried-forward price."""
    if not date(1900,1,1)<=day<=today() or not symbol:raise HTTPException(400,'종목 및 평가일 확인')
    out={'day':str(day),'symbol':symbol,'currency':currency,'price':None,'fx':'1' if currency=='KRW' else None,'price_source':None,'fx_source':None,'note':'동일 달력일의 저장값만 제안합니다. 실제 가격·환율을 확인 후 직접 저장하세요.'}
    if DEMO or day>=today():
        out['note']='데모 또는 미완료 날짜에는 자동 제안하지 않습니다.'
        return out
    with Session() as db:
        if currency=='KRW' and re.fullmatch(r'\d{6}',symbol) and db.get(MarketWatch,symbol):
            cache=db.get(MarketCache,'price:'+symbol)
            if cache and cache.state=='ok':
                rows=json.loads(cache.payload)
                matches=[x.get('close') for x in rows if isinstance(x,dict) and x.get('date')==str(day)] if isinstance(rows,list) else []
                if len(matches)==1 and type(matches[0]) in (int,float) and math.isfinite(matches[0]) and 0<matches[0]<=1e12:
                    out.update(price=raw(D(str(matches[0])).quantize(D('.000001'))),price_source='KIS 저장 종가')
        elif currency=='USD':
            st=db.get(Status,'usdkrw');r=db.get(Obs,('usdkrw',day))
            if st and st.state=='ok' and r and math.isfinite(r.value) and 0<r.value<1e5:
                out.update(fx=raw(D(str(r.value)).quantize(D('.000001'))),fx_source='ECOS 원/달러 매매기준율')
    return out
