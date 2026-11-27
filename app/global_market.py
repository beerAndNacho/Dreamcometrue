"""Completed-date overseas prices and explainable price-based diagnostics.
No orders, predictive claims, or changes to the established macro allocation model.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from math import isfinite
from functools import lru_cache
import exchange_calendars as xc
from statistics import mean, pstdev
import time
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field
from . import kr_market as km
from .admin import require_admin, require_write
from .collect import LOCK, today
from .db import DEMO, Session, history
from .dashboard_data import build_dashboard
from .settings import load_settings

router=APIRouter(tags=['global-market'])
# US-listed instruments. Geographic and sector ETFs are proxies, not local indices or fund flows.
ASSETS={}
for symbol,name,exchange,group in [
 ('NVDA','엔비디아','NAS','기업'),('AMD','AMD','NAS','기업'),('GOOGL','알파벳 A','NAS','기업'),('MSFT','마이크로소프트','NAS','기업'),('AAPL','애플','NAS','기업'),('AMZN','아마존','NAS','기업'),('AVGO','브로드컴','NAS','기업'),('META','메타','NAS','기업'),
 ('QQQ','나스닥100 ETF','NAS','대표 ETF'),('SOXX','반도체 ETF','NAS','대표 ETF'),('VOO','S&P500 ETF','AMS','대표 ETF'),('SCHD','배당주 ETF','AMS','대표 ETF'),('IWM','러셀2000 ETF','AMS','대표 ETF'),('DIA','다우 ETF','AMS','대표 ETF'),('IEF','미국 중기국채 ETF','NAS','대표 ETF'),
 ('EWU','영국 주식 ETF','AMS','국가 ETF'),('EWJ','일본 주식 ETF','AMS','국가 ETF'),('EWG','독일 주식 ETF','AMS','국가 ETF'),('FXI','중국 대형주 ETF','AMS','국가 ETF'),('EWH','홍콩 주식 ETF','AMS','국가 ETF'),('EWY','한국 주식 ETF','AMS','국가 ETF'),
 ('XLK','기술','AMS','미국 섹터 ETF'),('XLF','금융','AMS','미국 섹터 ETF'),('XLE','에너지','AMS','미국 섹터 ETF'),('XLV','헬스케어','AMS','미국 섹터 ETF'),('XLI','산업재','AMS','미국 섹터 ETF'),('XLP','필수소비재','AMS','미국 섹터 ETF'),('XLY','경기소비재','AMS','미국 섹터 ETF'),('XLU','유틸리티','AMS','미국 섹터 ETF'),('XLRE','부동산','AMS','미국 섹터 ETF'),('XLC','커뮤니케이션','AMS','미국 섹터 ETF'),('XLB','소재','AMS','미국 섹터 ETF'),
 ('GLD','금 ETF','AMS','원자재 ETF'),('SLV','은 ETF','AMS','원자재 ETF'),('USO','원유 ETF','AMS','원자재 ETF'),('UNG','천연가스 ETF','AMS','원자재 ETF'),('CPER','구리 ETF','AMS','원자재 ETF')]:
    ASSETS[symbol]={'symbol':symbol,'name':name,'exchange':exchange,'group':group,'currency':'USD','source':'KIS','adjusted':True}
INDEX={'sp500':'S&P500','nasdaq':'나스닥 종합','kospi':'코스피','kosdaq':'코스닥'}
SOURCE='https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/overseas_stock/dailyprice'
LAST=0.0

def normalize_auto_symbols(value):
    symbols=[s.strip().upper() for s in value.split(',') if s.strip()]
    if len(symbols)>3 or len(set(symbols))!=len(symbols) or any(s not in ASSETS for s in symbols):raise ValueError('자동 수집 종목은 지원 종목 최대 3개')
    return ','.join(symbols)

def cutoff():
    # Conservatively exclude the current New York calendar day, even after the close.
    return datetime.now(ZoneInfo('America/New_York')).date()-timedelta(days=1)

@lru_cache(maxsize=1)
def us_calendar():
    # XNYS is a proxy for common US listing sessions, not an exchange-specific halt feed.
    return xc.get_calendar('XNYS',start='1990-01-01',end='2050-12-31')

def sessions(start,end):
    if start>end:return []
    return [str(d.date()) for d in us_calendar().sessions_in_range(start.isoformat(),end.isoformat())]

def expected_window(end,count):
    cal=us_calendar()
    last=cal.date_to_session(end.isoformat(),direction='previous')
    return [str(d.date()) for d in cal.sessions_window(last,-count)]

def parse_prices(raw,end):
    if not isinstance(raw,list) or len(raw)>2000: raise km.MarketError('해외 일봉 응답 형식 오류')
    result={}
    for r in raw:
        if not isinstance(r,dict): raise km.MarketError('해외 일봉 행 형식 오류')
        d=km.day_value(r.get('xymd'))
        if d is None or d>end or d.weekday()>=5: continue
        o,h,l,c,v=(km.num(r.get(k)) for k in ('open','high','low','clos','tvol'))
        if any(x is None or x<=0 or x>1e9 for x in (o,h,l,c)) or not l<=min(o,c)<=max(o,c)<=h or v is None or v<0: raise km.MarketError('해외 일봉 가격/거래량 검증 실패')
        row={'date':str(d),'open':o,'high':h,'low':l,'close':c,'volume':v}
        if str(d) in result and result[str(d)]!=row: raise km.MarketError('해외 일봉 중복 날짜 상충')
        result[str(d)]=row
    if not result: raise km.MarketError('유효한 해외 일봉 없음')
    ordered=[result[d] for d in sorted(result)]
    allowed=set(sessions(date.fromisoformat(ordered[0]['date']),end))
    if any(r['date'] not in allowed for r in ordered):raise km.MarketError('미국 거래일 달력과 일봉 날짜 불일치')
    return ordered

def fetch_prices(client,cfg,symbol,end,target=600,deadline=None):
    asset=ASSETS[symbol]; token=km.kis_token(client,cfg); all_rows={}; cursor=end
    deadline=deadline if deadline is not None else time.monotonic()+75
    for page in range(12):
        if time.monotonic()>deadline:raise km.MarketError('해외 일봉 조회 시간 한도 초과')
        if page: time.sleep(.25)
        body=km.checked_json(client.get(km.KIS+'/uapi/overseas-price/v1/quotations/dailyprice',params={'AUTH':'','EXCD':asset['exchange'],'SYMB':symbol,'GUBN':'0','BYMD':cursor.strftime('%Y%m%d'),'MODP':'1'},headers={'authorization':'Bearer '+token,'appkey':cfg['KIS_APP_KEY'],'appsecret':cfg['KIS_APP_SECRET'],'tr_id':'HHDFS76240000','custtype':'P'}),'KIS 해외주식')
        if str(body.get('rt_cd'))!='0': raise km.MarketError('KIS 해외주식 조회 실패 · 해외시세 이용권한/거래소 코드 확인')
        identity=body.get('output1')
        if isinstance(identity,dict) and identity.get('rsym') and str(identity['rsym']).strip().upper()!=('D'+asset['exchange']+symbol):
            # Real-time symbol codes can vary by feed. Check suffix only when code has the documented exchange prefix.
            rsym=str(identity['rsym']).strip().upper()
            if rsym.startswith(('DNAS','DAMS','DNYS')) and rsym[1:]!=asset['exchange']+symbol:raise km.MarketError('해외 일봉 종목 식별 불일치')
        raw=body.get('output2')
        if raw==[] and all_rows: break
        rows=parse_prices(raw,cursor)
        for row in rows:
            d=row['date']
            if d in all_rows and all_rows[d]!=row: raise km.MarketError('해외 일봉 페이지 날짜 상충')
            all_rows[d]=row
        if len(all_rows)>=target: break
        next_cursor=date.fromisoformat(rows[0]['date'])-timedelta(days=1)
        if next_cursor>=cursor: raise km.MarketError('해외 일봉 페이지 진행 오류')
        cursor=next_cursor
    # Replace the entire adjusted-price window, not merge different adjustment vintages.
    return [all_rows[d] for d in sorted(all_rows)][-target:]

def ema(values,n):
    if len(values)<n:return []
    result=[mean(values[:n])]; alpha=2/(n+1)
    for v in values[n:]:result.append(alpha*v+(1-alpha)*result[-1])
    return result

def rsi(values,n=14):
    if len(values)<n+1:return None
    changes=[b-a for a,b in zip(values,values[1:])]
    up=mean([max(0,x) for x in changes[:n]]); down=mean([max(0,-x) for x in changes[:n]])
    for x in changes[n:]:up=(up*(n-1)+max(0,x))/n;down=(down*(n-1)+max(0,-x))/n
    return 50 if up==down==0 else 100 if down==0 else 100-100/(1+up/down)

def technical(rows):
    c=[r['close'] for r in rows]; n=len(c)
    if not c:return {}
    ma={str(k):mean(c[-k:]) if n>=k else None for k in (5,20,60,120,200)}
    changes={str(k):(c[-1]/c[-k-1]-1)*100 if n>k else None for k in (1,5,20,60,252)}
    mid=ma['20']; sd=pstdev(c[-20:]) if n>=20 else None
    e12,e26=ema(c,12),ema(c,26); mac=[a-b for a,b in zip(e12[-len(e26):],e26)] if e26 else []
    sig=ema(mac,9)
    tr=[]
    for prev,row in zip(rows,rows[1:]):
        if row.get('high') is None or row.get('low') is None:tr=[];break
        tr.append(max(row['high']-row['low'],abs(row['high']-prev['close']),abs(row['low']-prev['close'])))
    atr=mean(tr[:14]) if len(tr)>=14 else None
    if atr is not None:
        for x in tr[14:]:atr=(atr*13+x)/14
    cross=None
    if n>=201:
        prior=mean(c[-201:-1]); curr=ma['200']
        cross='상향 돌파' if c[-2]<=prior and c[-1]>curr else '하향 이탈' if c[-2]>=prior and c[-1]<curr else '교차 없음'
    return {'close':c[-1],'returns':changes,'ma':ma,'ma200_gap':(c[-1]/ma['200']-1)*100 if ma['200'] else None,'rsi':rsi(c),'macd':mac[-1] if mac else None,'signal':sig[-1] if sig else None,'bb_upper':mid+2*sd if sd is not None else None,'bb_lower':mid-2*sd if sd is not None else None,'atr':atr,'cross200':cross,'high252':max(c[-252:]) if n>=252 else None,'low252':min(c[-252:]) if n>=252 else None,'volume_ratio':rows[-1]['volume']/mean([r['volume'] for r in rows[-21:-1]]) if n>=21 and all(r.get('volume') is not None for r in rows[-21:]) and mean([r['volume'] for r in rows[-21:-1]])>0 else None}

def quality(rows,end,state):
    if not rows:return '자료 없음'
    if state not in ('ok','demo'):return '수집 오류/미완료'
    try:
        dates=[r['date'] for r in rows]
        expected=expected_window(end,len(rows))
        if dates[-1]!=expected[-1]:return '최근 완료 거래일 자료 확인 필요'
        if dates!=expected:return '거래일 누락/중복 또는 달력 불일치'
    except (ValueError,IndexError,KeyError,TypeError):return '거래일 검증 불가'
    if len(rows)<200:return '200개 관측 미만'
    return '분석 가능'

def diagnostics(metrics,status,busy):
    if status!='분석 가능' or busy:return {'state':'판단 보류','reasons':[status if not busy else '수집 진행 중']}
    gap=metrics['ma200_gap']; short=metrics['ma']['20']; medium=metrics['ma']['60']; last=metrics['close']; strength=metrics['rsi']
    conditions=[{'label':'200일선 상회','met':gap>0},{'label':'20일선 > 60일선','met':short>medium},{'label':'MACD > 신호선','met':metrics['macd']>metrics['signal']},{'label':'RSI 30~70','met':30<=strength<=70}]
    state='상승 추세 조건 일치' if all(x['met'] for x in conditions) else '하락 추세 주의' if gap<0 and short<medium else '혼조 / 추가 확인'
    return {'state':state,'conditions':conditions,'reasons':['가격 추세 조건만 평가. 매수·매도 신호 또는 적정가 판단 아님.']}

def demo_rows(symbol,end):
    import math
    dates=expected_window(end,600)
    scale=sum(ord(x) for x in symbol)%80+30
    return [{'date':str(d),'close':round(scale+i*.12+4*math.sin(i/12+scale),4),'open':round(scale+i*.12+4*math.sin(i/12+scale),4),'high':round(scale+i*.12+4*math.sin(i/12+scale)+1,4),'low':round(scale+i*.12+4*math.sin(i/12+scale)-1,4),'volume':100000+i*100} for i,d in enumerate(dates)]

def state(symbol,full=False):
    end=cutoff(); data={'state':'demo','rows':demo_rows(symbol,end),'message':'가상 자료'} if DEMO else km.cached('global:adjusted:'+symbol)
    rows=data['rows']; metrics=technical(rows); q=quality(rows,end,data['state'])
    return {**ASSETS[symbol],'date':rows[-1]['date'] if rows else None,'count':len(rows),'quality':q,'metrics':metrics,'diagnostics':diagnostics(metrics,q,LOCK.locked()),'message':data['message'],**({'rows':rows} if full else {})}

@router.get('/global-market',include_in_schema=False,dependencies=[Depends(require_admin)])
def page():return FileResponse(Path(__file__).parent/'static'/'global-market.html')

@router.get('/api/global-market',dependencies=[Depends(require_admin)])
def overview():
    d=build_dashboard(today(),busy=LOCK.locked()); indices=[x for x in d['indicators'] if x['id'] in INDEX]
    cfg=load_settings()
    return {'demo':DEMO,'auto_symbols':cfg.get('GLOBAL_AUTO_SYMBOLS',''),'auto_enabled':cfg.get('AUTO_COLLECT')=='1','configured':bool(cfg.get('KIS_APP_KEY') and cfg.get('KIS_APP_SECRET')),'cutoff':str(cutoff()),'assets':[state(s) for s in ASSETS],'indices':indices,'macro':d['model'],'source':SOURCE,'note':'ETF는 달러 표시 가격 대용 지표이며 실제 지수, 자금 유입 또는 배당 포함 총수익률이 아닙니다. 미국 공통 거래일 기준 검사; 거래소별 특별 휴장·종목 거래정지는 별도 확인.'}

@router.get('/api/global-market/{symbol}',dependencies=[Depends(require_admin)])
def detail(symbol:str):
    if symbol not in ASSETS:raise HTTPException(404,'지원 종목 아님')
    return {'demo':DEMO,**state(symbol,True)}

class Refresh(BaseModel):
    symbols:list[str]=Field(min_length=1,max_length=3)

def collect_selected(cfg,symbols,skip_current=False):
    """Caller holds LOCK. Keep optional collection independent of macro success."""
    global LAST
    if DEMO:return []
    if not cfg.get('KIS_APP_KEY') or not cfg.get('KIS_APP_SECRET'):raise km.MarketError('관리자 KIS 실전 키 등록 필요')
    LAST=time.monotonic(); result=[]; deadline=LAST+75; end=cutoff()
    with httpx.Client(timeout=httpx.Timeout(8,connect=4),follow_redirects=False) as client:
        for s in symbols:
            if time.monotonic()>deadline:result.append({'symbol':s,'state':'skipped','message':'요청 시간 한도'});continue
            try:
                data=km.cached('global:adjusted:'+s);old=data['rows']
                if skip_current and quality(old,end,data['state']) in ('분석 가능','200개 관측 미만'):
                    result.append({'symbol':s,'state':'current','count':len(old)});continue
                rows=fetch_prices(client,cfg,s,end,deadline=deadline)
                # Do not replace previous data with a short, stale or gapped response.
                q=quality(rows,end,'ok')
                if q not in ('분석 가능','200개 관측 미만'):raise km.MarketError(q)
                if len(rows)<len(old):raise km.MarketError('이전보다 짧은 일봉 응답 · 기존 이력 보존')
                km.store('global:adjusted:'+s,rows,merge=False);result.append({'symbol':s,'state':'ok','count':len(rows),'quality':q})
            except Exception as e:
                message=str(e) if type(e) is km.MarketError else '해외 시세 연결/응답 오류'
                km.store('global:adjusted:'+s,error=message);result.append({'symbol':s,'state':'error','message':message})
    return result

def collect_configured(cfg):
    symbols=normalize_auto_symbols(cfg.get('GLOBAL_AUTO_SYMBOLS',''))
    if not symbols or DEMO:return []
    return collect_selected(cfg,symbols.split(','),skip_current=True)

@router.post('/api/global-market/refresh',dependencies=[Depends(require_write)])
def refresh(req:Refresh):
    if DEMO:raise HTTPException(409,'데모 외부 수집 금지')
    if len(set(req.symbols))!=len(req.symbols) or any(s not in ASSETS for s in req.symbols):raise HTTPException(422,'지원 종목/중복 확인')
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'다른 수집 진행 중')
    try:
        if time.monotonic()-LAST<5:raise HTTPException(429,'5초 후 다시 시도')
        cfg=load_settings()
        if not cfg['KIS_APP_KEY'] or not cfg['KIS_APP_SECRET']:raise HTTPException(400,'관리자 KIS 실전 키 등록 필요')
        return {'results':collect_selected(cfg,req.symbols)}
    finally:LOCK.release()
