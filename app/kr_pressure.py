"""Read-only Korean market pressure observatory (4.3).
KIS completed-date observations and explicitly user-imported KRX CSVs are isolated.
No position inference, trade execution or changes to the macro risk model.
"""
import csv
import io
from datetime import date, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from . import kr_market as km
from .admin import require_admin, require_write
from .collect import LOCK, today
from .settings import load_settings
from .db import DEMO

router = APIRouter(tags=['kr-pressure'])
KRX = 'https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd'
KIS = km.SOURCES['KIS']
PROGRAM = {'KOSPI': 'K', 'KOSDAQ': 'Q'}
FIELDS = {
    'program': ('arbitrage_qty', 'nonarbitrage_qty', 'total_qty'),
    'short': ('short_qty', 'volume', 'short_pct'),
    'loan': ('new_qty', 'returned_qty', 'balance_qty'),
    'futures': ('foreign_contracts', 'institution_contracts', 'individual_contracts'),
    'short_balance': ('balance_qty', 'balance_pct'),
}
LAST = -1000.0


def integer(v, nonnegative=False):
    n = km.num(v)
    if n is None or abs(n) > 10**14 or n != int(n) or (nonnegative and n < 0):
        return None
    return int(n)


def ratio(v):
    n = km.num(v)
    return n if n is not None and 0 <= n <= 100 else None


def program_row(row):
    a = integer(row.get('arbt_smtm_ntby_qty'))
    b = integer(row.get('nabt_smtn_ntby_qty'))
    # Official example has distinct smtm/smtn spellings. Do not infer monetary units.
    if a is None or b is None:
        return None
    return {'arbitrage_qty': a, 'nonarbitrage_qty': b, 'total_qty': a+b}


def short_row(row):
    q = integer(row.get('ssts_cntg_qty'), True)
    v = integer(row.get('acml_vol'), True)
    if q is None or v is None or q > v or v <= 0:
        return None
    # Calculate volume share from same-row quantities, not amount or cumulative ratios.
    return {'short_qty': q, 'volume': v, 'short_pct': round(100*q/v, 4)}


def loan_row(row):
    a, b, c = (integer(row.get(k), True) for k in ('new_stcn','rdmp_stcn','rmnd_stcn'))
    if None in (a, b, c):
        return None
    return {'new_qty': a, 'returned_qty': b, 'balance_qty': c}


def fetch_program(client, cfg, market, cutoff):
    start = cutoff-timedelta(days=95)
    body = km.kis_get(client, cfg, 'comp-program-trade-daily', 'FHPPG04600001', {
        'FID_COND_MRKT_DIV_CODE': 'J', 'FID_MRKT_CLS_CODE': PROGRAM[market],
        'FID_INPUT_DATE_1': start.strftime('%Y%m%d'), 'FID_INPUT_DATE_2': cutoff.strftime('%Y%m%d')})
    return km.clean_rows(body.get('output'), cutoff, program_row)


def fetch_short(client, cfg, code, cutoff):
    body = km.kis_get(client, cfg, 'daily-short-sale', 'FHPST04830000', {
        'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': code,
        'FID_INPUT_DATE_1': (cutoff-timedelta(days=95)).strftime('%Y%m%d'),
        'FID_INPUT_DATE_2': cutoff.strftime('%Y%m%d')})
    return km.clean_rows(body.get('output2'), cutoff, short_row)


def fetch_loan(client, cfg, code, cutoff):
    body = km.kis_get(client, cfg, 'daily-loan-trans', 'HHPST074500C0', {
        'MRKT_DIV_CLS_CODE': '3', 'MKSC_SHRN_ISCD': code,
        'START_DATE': (cutoff-timedelta(days=95)).strftime('%Y%m%d'),
        'END_DATE': cutoff.strftime('%Y%m%d')})
    raw = body.get('output1')
    if not isinstance(raw, list) or len(raw)>2000 or any(not isinstance(r,dict) for r in raw):
        raise km.MarketError('대차 일별 응답 형식 오류')
    return km.clean_rows([{**r, 'stck_bsop_date':r.get('bsop_date')} for r in raw], cutoff, loan_row)


def key(kind, market, code):
    suffix = market if kind=='program' else 'KOSPI200' if kind=='futures' else code
    return 'pressure:'+kind+':'+suffix


def demo_rows(kind, market):
    rows=[]
    for i, r in enumerate(km.demo_rows(market)[-60:]):
        a=(i*193 % 2500-1300)*1000; b=(i*337 % 5000-2300)*1000
        values={
            'program': {'arbitrage_qty':a,'nonarbitrage_qty':b,'total_qty':a+b},
            'short': {'short_qty':15000+i*400,'volume':1000000,'short_pct':round((15000+i*400)/10000,4)},
            'loan': {'new_qty':20000+i*700,'returned_qty':18000+i*550,'balance_qty':2100000+i*2000},
            'futures': {'foreign_contracts':a//1000,'institution_contracts':b//1000,'individual_contracts':-(a+b)//1000},
            'short_balance': {'balance_qty':150000+i*900,'balance_pct':round(.25+i*.005,3)},
        }[kind]
        rows.append({'date':r['date'],**values})
    return rows


def state(kind, market, code):
    imported=kind in ('futures','short_balance')
    if DEMO:
        obj={'state':'demo','rows':demo_rows(kind,market),'updated':None,'attempted':None,'message':'가상 자료'}
    elif code or kind in ('program','futures'):
        obj=km.cached(key(kind,market,code))
    else:
        obj={'state':'pending','rows':[],'updated':None,'attempted':None,'message':'관심종목 등록 필요'}
    rows=obj['rows']; latest=rows[-1] if rows else None
    return {**obj,'latest':latest,'stale':not latest or obj['state'] not in ('ok','demo') or not 1<=(today()-date.fromisoformat(latest['date'])).days<=7,
            'source': KRX if imported else KIS, 'origin':'manual_csv' if imported else 'kis_api', 'demo':DEMO}


def comparison(spot, futures, market):
    """No adding KRW, share quantities, or contracts. Compare direction only on common latest date."""
    a,b=spot.get('latest'),futures.get('latest')
    if market!='KOSPI': return {'state':'unavailable','reason':'코스피200 선물은 코스닥 현물과 직접 비교하지 않습니다.'}
    if spot.get('stale',True) or futures.get('stale',True) or not a or not b or a['date']!=b['date']:
        return {'state':'unavailable','reason':'신선한 동일 관측일의 코스피 현물 및 코스피200 선물 자료 필요'}
    x,y=a.get('foreign'),b.get('foreign_contracts')
    if km.num(x) is None or km.num(y) is None:
        return {'state':'unavailable','reason':'외국인 값 누락'}
    label='동반 순매수' if x>0 and y>0 else '동반 순매도' if x<0 and y<0 else '방향 엇갈림' if x*y<0 else '중립/0 포함'
    return {'state':'ok','date':a['date'],'spot_foreign_eok':x,'futures_foreign_contracts':y,'label':label,
            'note':'코스피 전체 현물과 코스피200 선물은 모집단 및 단위가 다릅니다. 방향만 비교하며 헤지/롤오버 또는 실제 보유 포지션을 판정하지 않습니다.'}


def summarize(kind, rows, n):
    part=rows[-n:]
    if len(part)!=n or any(not 0<(date.fromisoformat(b['date'])-date.fromisoformat(a['date'])).days<=10 for a,b in zip(part,part[1:])):
        return None
    if kind in ('program','futures'):
        return {k:sum(r[k] for r in part) for k in FIELDS[kind]}
    if kind=='short':
        q=sum(r['short_qty'] for r in part); v=sum(r['volume'] for r in part)
        return {'short_qty':q,'volume':v,'short_pct':round(100*q/v,4) if v else None}
    if kind=='loan':
        return {'new_qty':sum(r['new_qty'] for r in part),'returned_qty':sum(r['returned_qty'] for r in part),'balance_qty':part[-1]['balance_qty'],'balance_change':part[-1]['balance_qty']-part[0]['balance_qty']}
    return {'balance_qty':part[-1]['balance_qty'],'balance_pct':part[-1]['balance_pct'],'balance_change':part[-1]['balance_qty']-part[0]['balance_qty']}


def parse_import(kind, content, cutoff):
    fields=FIELDS[kind]; reader=csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    if reader.fieldnames!=['date',*fields]: raise ValueError('CSV 헤더: '+','.join(('date',*fields)))
    by={}
    for row in reader:
        if None in row or len(by)>=400: raise ValueError('행 형식 또는 최대 400개 관측일 제한')
        d=date.fromisoformat(row['date'])
        if str(d)!=row['date'] or d>cutoff or d.weekday()>=5 or d<date(2000,1,1): raise ValueError('완료된 평일의 YYYY-MM-DD 날짜 필요')
        vals={k: ratio(row[k]) if k=='balance_pct' else integer(row[k],kind=='short_balance') for k in fields}
        if None in vals.values() or str(d) in by: raise ValueError('잘못된 값 또는 중복 일자')
        by[str(d)]={'date':str(d),**vals}
    if not by: raise ValueError('CSV 자료 없음')
    return [by[k] for k in sorted(by)]


class ImportRequest(BaseModel):
    kind: Literal['futures','short_balance']
    code: str = Field(default='',pattern=r'^(\d{6})?$')
    csv: str = Field(min_length=1,max_length=200_000)
    confirmed: bool = False


def check_code(code):
    if code and code not in {r['code'] for r in km.watches()}:
        raise HTTPException(404,'관심종목에 먼저 등록하세요.')


@router.get('/kr-pressure',include_in_schema=False,dependencies=[Depends(require_admin)])
def page():
    return FileResponse(Path(__file__).parent/'static'/'kr-pressure.html',headers={'Cache-Control':'no-store'})


@router.get('/api/kr-pressure',dependencies=[Depends(require_admin)])
def dashboard(market:Literal['KOSPI','KOSDAQ']='KOSPI',code:str=Query('',pattern=r'^(\d{6})?$'),period:int=Query(20,ge=1,le=60)):
    if period not in (1,5,20,60): raise HTTPException(422,'관측일 수는 1, 5, 20, 60 중 선택하세요.')
    check_code(code)
    kinds=['program','futures']+(['short','loan','short_balance'] if code else [])
    data={k:state(k,market,code) for k in kinds}
    for k,v in data.items():
        v['summary']=None if v['stale'] else summarize(k,v['rows'],period)
        v['rows']=v['rows'][-60:]
    spot=km.market_state(market)
    return {'market':market,'code':code,'period':period,'demo':DEMO,'asof':str(today()),'watchlist':km.watches(),'data':data,
            'comparison':comparison(spot,data['futures'],market),'busy':LOCK.locked(),
            'note':'기간은 실제 저장된 관측일 수입니다. 휴장/누락 완전성은 보장하지 않으며 공급처별 관측일을 확인하세요.'}


@router.post('/api/kr-pressure/refresh',dependencies=[Depends(require_write)])
def refresh(market:Literal['KOSPI','KOSDAQ']='KOSPI',code:str=Query('',pattern=r'^(\d{6})?$')):
    global LAST
    if DEMO: raise HTTPException(409,'데모에서는 외부 조회하지 않습니다.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 수집 작업 중입니다.')
    try:
        check_code(code)
        cfg=load_settings()
        if not cfg['KIS_APP_KEY'] or not cfg['KIS_APP_SECRET']: raise HTTPException(400,'관리자에서 KIS 키를 등록하세요.')
        clock=monotonic()
        if clock-LAST<5: raise HTTPException(429,'호출 간격 제한: 5초 후 다시 시도하세요.')
        LAST=clock
        cutoff=km.previous_weekday(today()-timedelta(days=1)); jobs=[('program',fetch_program,market)]
        if code: jobs.extend([('short',fetch_short,code),('loan',fetch_loan,code)])
        results=[]
        with httpx.Client(timeout=httpx.Timeout(15,connect=6),follow_redirects=False) as client:
            for kind,fn,arg in jobs:
                try:
                    rows=fn(client,cfg,arg,cutoff); km.store(key(kind,market,code),rows)
                    results.append({'kind':kind,'ok':True,'count':len(rows)})
                except Exception as exc:
                    msg=str(exc) if isinstance(exc,km.MarketError) else 'API 호출 또는 처리 실패'
                    km.store(key(kind,market,code),error=msg); results.append({'kind':kind,'ok':False,'message':msg})
                sleep(.35)
        return {'results':results}
    finally: LOCK.release()


@router.post('/api/kr-pressure/import',dependencies=[Depends(require_write)])
def import_csv(req:ImportRequest):
    if DEMO: raise HTTPException(409,'데모에서는 실제 CSV를 등록하지 않습니다.')
    if not req.confirmed: raise HTTPException(400,'출처·상품·단위와 기존 동일 일자 갱신을 확인하세요.')
    if req.kind=='short_balance' and not req.code: raise HTTPException(400,'공매도 잔고 종목 선택 필요')
    if req.kind=='futures' and req.code: raise HTTPException(400,'코스피200 선물 시장 자료에는 종목코드를 지정하지 않습니다.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 작업 중입니다.')
    try:
        check_code(req.code)
        try: rows=parse_import(req.kind,req.csv,today()-timedelta(days=1))
        except (ValueError,TypeError,csv.Error) as exc:
            raise HTTPException(400,'CSV 형식, 날짜, 단위, 중복 및 값 범위를 확인하세요.') from None
        km.store(key(req.kind,'KOSPI',req.code),rows)
        return {'ok':True,'count':len(rows),'first':rows[0]['date'],'last':rows[-1]['date'],'origin':'manual_csv'}
    finally: LOCK.release()
