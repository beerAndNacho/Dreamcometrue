"""Read-only KIS market data and OpenDART financial statements, separate from macro scoring.
External responses and credentials are never returned verbatim. All prices are completed-date observations.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib, io, json, re, time, zipfile
from xml.etree import ElementTree
from pathlib import Path
from typing import Literal
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from .db import DEMO, Session, MarketCache, MarketWatch, now
from .settings import load_settings
from .admin import require_write, require_admin
from .collect import LOCK, today

router = APIRouter(tags=['kr-market'])
KIS='https://openapi.koreainvestment.com:9443'
DART='https://opendart.fss.or.kr/api'
MARKETS={'KOSPI':('0001','KSP'),'KOSDAQ':('1001','KSQ')}
FLOW_FIELDS={'foreign':'frgn_ntby_tr_pbmn','institution':'orgn_ntby_tr_pbmn','individual':'prsn_ntby_tr_pbmn','other':'etc_ntby_tr_pbmn','fund':'fund_ntby_tr_pbmn','securities':'scrt_ntby_tr_pbmn','trust':'ivtr_ntby_tr_pbmn'}
CORE=('foreign','institution','individual')
REPORTS={'11013':'1분기','11012':'반기','11014':'3분기','11011':'사업보고서'}
SOURCES={'KIS':'https://apiportal.koreainvestment.com/apiservice-category','DART':'https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020'}
TOKEN={}
CORPS={}
class MarketError(Exception): pass

DART_STATUS={
    '010':'등록되지 않은 인증키입니다. OpenDART 인증키 관리에서 확인하세요.',
    '011':'사용 중지된 인증키입니다. OpenDART에서 키 상태를 확인하세요.',
    '012':'접속 IP가 허용되지 않았습니다. 서버의 외부 IP 및 인증키 IP 제한을 확인하세요.',
    '013':'조회 자료가 없습니다. 사업연도·보고서·연결/별도 구분과 공시 여부를 확인하세요.',
    '014':'공시 파일이 존재하지 않습니다.',
    '020':'요청 한도를 초과했습니다. 이용현황과 한도 초기화를 확인하세요.',
    '021':'조회 가능 회사 수를 초과했습니다.',
    '100':'잘못된 요청 필드입니다. 기업 고유번호·보고서·조회 조건을 확인하세요.',
    '101':'부적절한 접근입니다. OpenDART 이용 권한을 확인하세요.',
    '800':'OpenDART 시스템 점검 중입니다.',
    '900':'OpenDART에서 정의되지 않은 오류가 발생했습니다.',
    '901':'사용자 계정의 개인정보 보유기간이 만료되었습니다. OpenDART에 문의하세요.',
}
DART_FATAL={'010','011','012','020','101','800','900','901','network','http','format','missing_key','unknown'}
DART_LABELS={'company':'기업개황','corpCode':'기업 고유번호 목록','fnlttSinglAcntAll':'재무제표','alotMatter':'배당','list':'공시 검색'}

class DartError(MarketError):
    def __init__(self,code,stage):
        self.code=code if isinstance(code,str) and code in DART_STATUS.keys()|DART_FATAL else 'unknown'
        self.fatal=self.code in DART_FATAL
        label=DART_LABELS.get(stage,'공시')
        message=DART_STATUS.get(self.code,{
            'missing_key':'관리자에서 인증키를 저장하세요.',
            'network':'서버에서 OpenDART에 연결하지 못했습니다. DNS·방화벽·TLS·시간 초과를 확인하세요.',
            'http':'OpenDART HTTP 요청에 실패했습니다. 서비스 상태·접속 제한을 확인하세요.',
            'format':'OpenDART 응답 형식이 올바르지 않습니다.',
            'unknown':'알 수 없는 OpenDART 상태입니다. 공식 이용현황을 확인하세요.',
        }.get(self.code,'조회 실패'))
        super().__init__(f'OpenDART {label} [{self.code}] · {message}')

def dart_key(cfg,stage):
    key=cfg.get('DART_API_KEY','').strip()
    if not key: raise DartError('missing_key',stage)
    return key

def dart_status(body,stage,allow_no_data=False):
    if not isinstance(body,dict): raise DartError('format',stage)
    code=body.get('status')
    if code=='000' or (allow_no_data and code=='013'): return body
    raise DartError(code,stage)

def dart_zip_error(raw):
    # corpCode.xml returns ZIP on success, but may return an XML/JSON error instead.
    # Parse only small error envelopes; never expose remote message/URL/credentials.
    if len(raw)>65536 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise DartError('format','corpCode')
    try:
        body=json.loads(raw)
    except (ValueError,UnicodeError):
        try:
            root=ElementTree.fromstring(raw)
            body={'status':root.findtext('status')}
        except ElementTree.ParseError: raise DartError('format','corpCode') from None
    if isinstance(body,dict) and isinstance(body.get('status'),str) and body['status'] in DART_STATUS:
        dart_status(body,'corpCode')
    raise DartError('format','corpCode')

def num(v):
    if v is None or isinstance(v,bool): return None
    try:
        d=Decimal(str(v).strip().replace(',',''))
        if not d.is_finite() or abs(d)>Decimal('1e18'): return None
        return float(d)
    except (InvalidOperation,ValueError): return None

def day_value(v):
    try:
        if not isinstance(v,str) or not re.fullmatch(r'\d{8}',v): return None
        return datetime.strptime(v,'%Y%m%d').date()
    except ValueError: return None

def checked_json(response,provider):
    try:
        response.raise_for_status(); body=response.json()
    except (httpx.HTTPError,ValueError): raise MarketError(provider+' 연결 또는 응답 오류') from None
    if not isinstance(body,dict): raise MarketError(provider+' 응답 형식 오류')
    return body

def kis_token(client,cfg):
    key,secret=cfg['KIS_APP_KEY'],cfg['KIS_APP_SECRET']
    if not key or not secret: raise MarketError('KIS App Key와 App Secret 등록 필요')
    fingerprint=hashlib.sha256((key+'\0'+secret).encode()).hexdigest()
    clock=time.monotonic()
    if TOKEN.get('identity')==fingerprint:
        if TOKEN.get('expires',0)>clock+60: return TOKEN['value']
        if clock-TOKEN.get('attempt',-1000)<60: raise MarketError('KIS 토큰 재발급 대기 (최소 60초)')
    TOKEN.clear(); TOKEN.update(identity=fingerprint,attempt=clock)
    body=checked_json(client.post(KIS+'/oauth2/tokenP',json={'grant_type':'client_credentials','appkey':key,'appsecret':secret}), 'KIS 인증')
    token=body.get('access_token'); ttl=num(body.get('expires_in'))
    if not isinstance(token,str) or not token or ttl is None or ttl<=60: raise MarketError('KIS 토큰 발급 실패 · 실전 키 확인')
    TOKEN.update(value=token,expires=clock+min(ttl,86400))
    return token

def kis_get(client,cfg,path,tr,params):
    token=kis_token(client,cfg)
    body=checked_json(client.get(KIS+'/uapi/domestic-stock/v1/quotations/'+path,params=params,headers={'authorization':'Bearer '+token,'appkey':cfg['KIS_APP_KEY'],'appsecret':cfg['KIS_APP_SECRET'],'tr_id':tr,'custtype':'P'}),'KIS')
    if str(body.get('rt_cd'))!='0': raise MarketError('KIS 조회 실패 · 권한, 종목/시장 코드, 호출 제한 확인')
    return body

def clean_rows(raw,cutoff,parse):
    if not isinstance(raw,list) or len(raw)>2000: raise MarketError('일별 자료 응답 형식 오류')
    rows={}
    for row in raw:
        if not isinstance(row,dict): raise MarketError('일별 자료 행 형식 오류')
        d=day_value(row.get('stck_bsop_date'))
        if d is None or d>cutoff or d.weekday()>=5: continue
        parsed=parse(row)
        if parsed is None: continue
        k=str(d); entry={'date':k,**parsed}
        if k in rows and rows[k]!=entry: raise MarketError('중복 일자의 값 불일치')
        rows[k]=entry
    if not rows: raise MarketError('유효한 직전 거래일 자료 없음')
    return [rows[k] for k in sorted(rows)]

def parse_flow(row):
    values={k:num(row.get(f)) for k,f in FLOW_FIELDS.items()}
    if any(values[k] is None for k in CORE): return None
    # KIS *_tr_pbmn = million KRW; normalized to KRW 100 million (억원).
    return {**{k:None if v is None else v/100 for k,v in values.items()},'index':num(row.get('bstp_nmix_prpr')),'change_pct':num(row.get('bstp_nmix_prdy_ctrt'))}

def previous_weekday(d):
    while d.weekday()>=5: d-=timedelta(days=1)
    return d

def fetch_flow(client,cfg,market,cutoff,target=60):
    code,kind=MARKETS[market]; all_rows={}; end=previous_weekday(cutoff)
    def parse_market(row):
        r=parse_flow(row)
        return r if r and r['index'] is not None and r['index']>0 else None
    for page in range(4):
        d=end.strftime('%Y%m%d')
        try:
            result=kis_get(client,cfg,'inquire-investor-daily-by-market','FHPTJ04040000',{'FID_COND_MRKT_DIV_CODE':'U','FID_INPUT_ISCD':code,'FID_INPUT_DATE_1':d,'FID_INPUT_ISCD_1':kind,'FID_INPUT_DATE_2':d,'FID_INPUT_ISCD_2':code})
            rows=clean_rows(result.get('output'),end,parse_market)
        except MarketError:
            if not all_rows: raise
            break # usable recent batch retained; missing older observations stay missing
        all_rows.update({r['date']:r for r in rows})
        if len(all_rows)>=target: break
        end=previous_weekday(date.fromisoformat(rows[0]['date'])-timedelta(days=1))
        time.sleep(.3)
    return [all_rows[k] for k in sorted(all_rows)]

def parse_price(row):
    close=num(row.get('stck_clpr'))
    if close is None or close<=0: return None
    volume=num(row.get('acml_vol')); amount=num(row.get('acml_tr_pbmn'))
    return {'close':close,'volume':volume if volume is not None and volume>=0 else None,'amount':amount if amount is not None and amount>=0 else None}

def fetch_price(client,cfg,code,cutoff):
    result=kis_get(client,cfg,'inquire-daily-itemchartprice','FHKST03010100',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':code,'FID_INPUT_DATE_1':(cutoff-timedelta(days=135)).strftime('%Y%m%d'),'FID_INPUT_DATE_2':cutoff.strftime('%Y%m%d'),'FID_PERIOD_DIV_CODE':'D','FID_ORG_ADJ_PRC':'1'})
    header=result.get('output1',{})
    if not isinstance(header,dict): raise MarketError('종목 응답 형식 오류')
    returned=header.get('stck_shrn_iscd')
    if returned and str(returned)!=code: raise MarketError('종목 코드 불일치')
    return clean_rows(result.get('output2'),cutoff,parse_price)

def fetch_stock_flow(client,cfg,code,cutoff):
    result=kis_get(client,cfg,'investor-trade-by-stock-daily','FHPTJ04160001',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':code,'FID_INPUT_DATE_1':cutoff.strftime('%Y%m%d'),'FID_ORG_ADJ_PRC':'','FID_ETC_CLS_CODE':''})
    return clean_rows(result.get('output2'),cutoff,lambda r:parse_flow(r) if (num(r.get('stck_clpr')) or 0)>0 else None)

def dart_get(client,cfg,endpoint,params,allow_no_data=False):
    key=dart_key(cfg,endpoint)
    try:
        response=client.get(DART+'/'+endpoint+'.json',params={**params,'crtfc_key':key})
        response.raise_for_status()
    except httpx.HTTPStatusError: raise DartError('http',endpoint) from None
    except httpx.RequestError: raise DartError('network',endpoint) from None
    try: body=response.json()
    except ValueError: raise DartError('format',endpoint) from None
    return dart_status(body,endpoint,allow_no_data)

def resolve_corp(client,cfg,code,force=False):
    key=dart_key(cfg,'corpCode')
    clock=time.monotonic(); identity=hashlib.sha256(key.encode()).hexdigest()
    if force or CORPS.get('identity')!=identity or CORPS.get('expires',0)<=clock:
        # Official corpCode.zip contains CORPCODE.xml. Bound both compressed and decompressed sizes.
        buf=io.BytesIO()
        try:
            with client.stream('GET',DART+'/corpCode.xml',params={'crtfc_key':key}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    buf.write(chunk)
                    if buf.tell()>20_000_000: raise MarketError('DART 기업목록 크기 제한 초과')
        except httpx.HTTPStatusError: raise DartError('http','corpCode') from None
        except httpx.RequestError: raise DartError('network','corpCode') from None
        if not zipfile.is_zipfile(buf): dart_zip_error(buf.getvalue())
        try:
            with zipfile.ZipFile(buf) as z:
                candidates=[x for x in z.infolist() if x.filename.upper()=='CORPCODE.XML']
                if len(candidates)!=1 or candidates[0].file_size>60_000_000: raise MarketError('DART 기업목록 형식 오류')
                with z.open(candidates[0]) as f:
                    content=f.read(60_000_001)
                    if len(content)>60_000_000 or b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper(): raise MarketError('DART 기업목록 형식 오류')
                    root=ElementTree.fromstring(content)
        except (zipfile.BadZipFile,ElementTree.ParseError,RuntimeError,NotImplementedError): raise DartError('format','corpCode') from None
        mapping={}
        for r in root.findall('list'):
            ticker=(r.findtext('stock_code') or '').strip(); corp=(r.findtext('corp_code') or '').strip()
            if re.fullmatch(r'\d{6}',ticker) and re.fullmatch(r'\d{8}',corp): mapping.setdefault(ticker,set()).add(corp)
        CORPS.clear(); CORPS.update(identity=identity,expires=clock+86400,mapping=mapping)
    found=CORPS['mapping'].get(code,set())
    if len(found)!=1: raise MarketError('기업 고유번호 자동 확인 불가 · 직접 입력하세요.')
    return next(iter(found))

def fetch_financials(client,cfg,code,corp,year,report,scope):
    if not corp: corp=resolve_corp(client,cfg,code)
    company=dart_get(client,cfg,'company',{'corp_code':corp})
    if str(company.get('stock_code') or '').strip()!=code: raise MarketError('DART 기업 고유번호와 종목 코드 불일치 · 종목코드 6자리와 고유번호 8자리를 확인하세요.')
    body=dart_get(client,cfg,'fnlttSinglAcntAll',{'corp_code':corp,'bsns_year':str(year),'reprt_code':report,'fs_div':scope})
    raw=body.get('list')
    if not isinstance(raw,list) or not raw or len(raw)>10000: raise MarketError('재무제표 응답 없음')
    ids={'revenue':('ifrs-full_Revenue','ifrs_Revenue'),'operating_profit':('dart_OperatingIncomeLoss',),'net_income':('ifrs-full_ProfitLoss','ifrs_ProfitLoss'),'eps':('ifrs-full_BasicEarningsLossPerShare','ifrs_BasicEarningsLossPerShare')}
    values={}; previous={}; labels={}; receipts=set()
    for row in raw:
        if not isinstance(row,dict) or row.get('sj_div') not in ('IS','CIS') or row.get('currency')!='KRW': continue
        if str(row.get('bsns_year'))!=str(year) or str(row.get('reprt_code'))!=report or row.get('corp_code')!=corp: continue
        for k,aliases in ids.items():
            if row.get('account_id') not in aliases: continue
            # Annual/quarterly period amount, not cumulative amount. Avoid aggregating repeated statements.
            value=num(row.get('thstrm_amount'))
            if value is None: continue
            values.setdefault(k,set()).add(value); labels[k]=str(row.get('account_nm',''))[:150]
            prev=num(row.get('frmtrm_amount' if report=='11011' else 'frmtrm_q_amount'))
            if prev is not None: previous.setdefault(k,set()).add(prev)
            receipt=str(row.get('rcept_no',''))
            if re.fullmatch(r'\d{14}',receipt): receipts.add(receipt)
    clean={k:next(iter(values[k])) if len(values.get(k,set()))==1 else None for k in ids}
    if not any(v is not None for v in clean.values()): raise MarketError('표준 KRW 손익계산서 계정 확인 불가')
    yoy={k:round((v/next(iter(previous[k]))-1)*100,2) if v is not None and len(previous.get(k,set()))==1 and next(iter(previous[k]))>0 else None for k,v in clean.items()}
    margin=round(clean['operating_profit']/clean['revenue']*100,2) if clean['revenue'] and clean['revenue']>0 and clean['operating_profit'] is not None else None
    return {'yoy':yoy,'operating_margin':margin,'code':code,'corp_code':corp,'name':str(company.get('stock_name') or company.get('corp_name') or code)[:100],'year':year,'report':report,'report_name':REPORTS[report],'scope':scope,'values':clean,'labels':labels,'receipt':next(iter(receipts)) if len(receipts)==1 else None,'note':'공시 실제 실적: 사업보고서는 연간, 분기/반기는 해당 3개월 당기 금액. 예상 EPS/TTM 아님. 연결/별도 구분 확인. 중복 상충/미확인 계정은 공란.'}

def cached(key):
    with Session() as db:
        row=db.get(MarketCache,key)
        if not row: return {'state':'pending','rows':[],'updated':None,'attempted':None,'message':'수집 전'}
        return {'state':row.state,'rows':json.loads(row.payload),'updated':row.updated.isoformat()+'Z' if row.updated else None,'attempted':row.attempted.isoformat()+'Z','message':row.message}

def store(key,rows=None,error=None,merge=True):
    with Session.begin() as db:
        old=db.get(MarketCache,key); previous=json.loads(old.payload) if old else []
        if rows is not None:
            if merge:
                by={r['date']:r for r in previous}; by.update({r['date']:r for r in rows}); rows=[by[k] for k in sorted(by)][-400:]
            db.merge(MarketCache(key=key,payload=json.dumps(rows,ensure_ascii=False,allow_nan=False),state='ok',message=f'{len(rows)}개 관측 저장',updated=now(),attempted=now()))
        else:
            db.merge(MarketCache(key=key,payload=json.dumps(previous,ensure_ascii=False),state='error',message=error or '수집 실패',updated=old.updated if old else None,attempted=now()))

def watches():
    with Session() as db: return [{'code':r.code,'name':r.name,'corp_code':r.corp_code} for r in db.scalars(select(MarketWatch).order_by(MarketWatch.code))]

def summarize(rows):
    out={}; last=rows[-1] if rows else None
    for n in (1,5,20,60):
        part=rows[-n:]
        contiguous=len(part)==n and all((date.fromisoformat(b['date'])-date.fromisoformat(a['date'])).days<=10 for a,b in zip(part,part[1:]))
        out[str(n)]={k:round(sum(r[k] for r in part),2) if contiguous and all(r.get(k) is not None for r in part) else None for k in CORE}
    streak=0
    if last and last.get('foreign'):
        sign=last['foreign']>0
        prev_date=None
        for r in reversed(rows):
            d=date.fromisoformat(r['date'])
            if prev_date and (prev_date-d).days>10: break
            prev_date=d
            if not r.get('foreign') or (r['foreign']>0)!=sign: break
            streak+=1
        streak=streak if sign else -streak
    return {'latest':last,'totals':out,'foreign_streak':streak}

def demo_rows(market):
    # Deterministic synthetic data; no external request, never stored in live cache.
    d=today()-timedelta(days=1); days=[]
    while len(days)<75:
        if d.weekday()<5: days.append(d)
        d-=timedelta(days=1)
    factor=1 if market=='KOSPI' else .23
    return [{'date':str(day),'foreign':round(((i*173)%2300-1200)*factor,2),'institution':round(((i*211)%1700-700)*factor,2),'individual':round(-(((i*173)%2300-1200)+((i*211)%1700-700))*factor,2),'other':0,'fund':round(((i*79)%400-200)*factor,2),'securities':None,'trust':None,'index':round((2700+i*3.8+((i*37)%55))*factor,2),'change_pct':None} for i,day in enumerate(reversed(days))]

def market_state(market):
    if DEMO: obj={'state':'demo','rows':demo_rows(market),'updated':None,'attempted':None,'message':'가상 자료'}
    else: obj=cached('flow:'+market)
    obj['stale']=not obj['rows'] or (today()-date.fromisoformat(obj['rows'][-1]['date'])).days>7 or obj['state']=='error'
    return {**obj,**summarize(obj['rows'])}

def collect_market(cfg=None,code=None,markets=True,include_watch=True):
    """Caller must hold global LOCK. Optional data sources never change macro scoring."""
    cfg=cfg or load_settings()
    if DEMO: return {'state':'demo','results':[]}
    cutoff=previous_weekday(today()-timedelta(days=1)); jobs=[]
    if markets: jobs.extend(('flow:'+m,lambda c,m=m:fetch_flow(c,cfg,m,cutoff,target=1 if len(cached('flow:'+m)['rows'])>=60 else 60)) for m in MARKETS)
    codes=[code] if code else [r['code'] for r in watches()] if include_watch else []
    for ticker in codes:
        jobs.append(('price:'+ticker,lambda c,t=ticker:fetch_price(c,cfg,t,cutoff)))
        jobs.append(('stockflow:'+ticker,lambda c,t=ticker:fetch_stock_flow(c,cfg,t,cutoff)))
    results=[]
    with httpx.Client(timeout=httpx.Timeout(15,connect=6),follow_redirects=False) as client:
        for key,fn in jobs:
            try:
                rows=fn(client); store(key,rows); results.append({'key':key,'ok':True,'count':len(rows)})
            except Exception as e:
                msg=str(e) if isinstance(e,MarketError) else 'API 호출 또는 처리 실패'
                store(key,error=msg); results.append({'key':key,'ok':False,'message':msg})
            time.sleep(.3)
    return {'state':'ok' if results and all(x['ok'] for x in results) else 'partial' if any(x['ok'] for x in results) else 'error','results':results}

@router.get('/kr-market',include_in_schema=False)
def market_page(): return FileResponse(Path(__file__).parent/'static'/'kr-market.html')

@router.get('/api/kr-market')
def market_dashboard():
    cfg=load_settings()
    return {'demo':DEMO,'asof':str(today()),'busy':LOCK.locked(),'configured':bool(cfg['KIS_APP_KEY'] and cfg['KIS_APP_SECRET']),'dart_configured':bool(cfg['DART_API_KEY']),'markets':{m:market_state(m) for m in MARKETS},'source':SOURCES['KIS'],'unit':'억원','cutoff':str(today()-timedelta(days=1))}

def watch_data(code):
    if DEMO:
        src=demo_rows('KOSPI'); scale=0.025+(int(code)%17)/1000
        price=[{'date':r['date'],'close':round(r['index']*(10+int(code)%50),0),'volume':None,'amount':None} for r in src]
        flow=[{**r,**{k:round(r[k]*scale,2) if r[k] is not None else None for k in FLOW_FIELDS},'index':None,'change_pct':None} for r in src]
        def obj(rows): return {'state':'demo','rows':rows,'updated':None,'attempted':None,'message':'가상 자료','stale':False}
        return obj(price),obj(flow)
    result=[]
    for key in ('price:'+code,'stockflow:'+code):
        st=cached(key);st['stale']=not st['rows'] or st['state']!='ok' or (today()-date.fromisoformat(st['rows'][-1]['date'])).days>7
        result.append(st)
    return tuple(result)

@router.get('/api/kr-market/watch',dependencies=[Depends(require_admin)])
def watchlist():
    items=[]
    for r in watches():
        price,flow=watch_data(r['code']);items.append({**r,'price':price,'flow':flow})
    return {'items':items,'demo':DEMO}

class Watch(BaseModel):
    code:str=Field(pattern=r'^\d{6}$')
    name:str=Field(min_length=1,max_length=80)
    corp_code:str=Field(default='',pattern=r'^(\d{8})?$')

@router.put('/api/kr-market/watch',dependencies=[Depends(require_write)])
def upsert_watch(req:Watch):
    if not req.name.strip(): raise HTTPException(422,'종목명을 입력하세요.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'수집 중입니다.')
    try:
        with Session.begin() as db:
            old=db.get(MarketWatch,req.code)
            if not old and len(list(db.scalars(select(MarketWatch.code))))>=20: raise HTTPException(400,'관심종목은 최대 20개입니다.')
            db.merge(MarketWatch(code=req.code,name=req.name.strip(),corp_code=req.corp_code))
        return {'ok':True}
    finally: LOCK.release()

@router.delete('/api/kr-market/watch/{code}',dependencies=[Depends(require_write)])
def delete_watch(code:str):
    if not re.fullmatch(r'\d{6}',code): raise HTTPException(422,'종목코드 형식 오류')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'수집 중입니다.')
    try:
        with Session.begin() as db:
            old=db.get(MarketWatch,code)
            if old: db.delete(old)
        return {'ok':True}
    finally: LOCK.release()

@router.post('/api/kr-market/refresh',dependencies=[Depends(require_write)])
def refresh(code:str|None=Query(None,pattern=r'^\d{6}$')):
    if DEMO: raise HTTPException(409,'데모에서는 실제 API 호출을 하지 않습니다.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 수집 작업 중입니다.')
    try:
        cfg=load_settings()
        if not (cfg['KIS_APP_KEY'] and cfg['KIS_APP_SECRET']): raise HTTPException(400,'관리자에서 KIS 키를 먼저 등록하세요.')
        if code and not any(r['code']==code for r in watches()): raise HTTPException(404,'관심종목에 먼저 등록하세요.')
        return collect_market(cfg,code,markets=not bool(code),include_watch=False)
    finally: LOCK.release()

@router.get('/api/kr-market/export/{market}',dependencies=[Depends(require_admin)])
def export(market:Literal['KOSPI','KOSDAQ']):
    import csv,io
    obj=market_state(market); file=io.StringIO(); w=csv.writer(file); fields=['date',*CORE,'other','fund','securities','trust','index','change_pct']; w.writerow(fields)
    w.writerows([[r.get(k) for k in fields] for r in obj['rows']]); return StreamingResponse(iter([file.getvalue()]),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename={market}-{"demo" if DEMO else "KIS"}-flow-eok.csv'})

class FinanceRequest(BaseModel):
    code:str=Field(pattern=r'^\d{6}$')
    corp_code:str=Field(default='',pattern=r'^(\d{8})?$')
    year:int=Field(ge=2015,le=2100)
    report:Literal['11013','11012','11014','11011']
    scope:Literal['CFS','OFS']='CFS'

@router.post('/api/kr-market/financials',dependencies=[Depends(require_write)])
def financials(req:FinanceRequest):
    if DEMO: raise HTTPException(409,'데모에서는 실제 공시 조회를 하지 않습니다.')
    if req.year>today().year: raise HTTPException(422,'미래 사업연도는 조회할 수 없습니다.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 작업 중입니다.')
    try:
        with httpx.Client(timeout=httpx.Timeout(15,connect=6),follow_redirects=False) as client:
            try: return fetch_financials(client,load_settings(),req.code,req.corp_code,req.year,req.report,req.scope)
            except MarketError as e: raise HTTPException(400,str(e)) from None
            except Exception: raise HTTPException(502,'공시 연결/응답 오류') from None
    finally: LOCK.release()
