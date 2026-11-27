"""Private Telegram observations. Fixed destination, opt-in, no AI calls or trading.
Single worker deployment. A committed claim prevents replay after restarts or timeouts.
"""
from datetime import datetime, timedelta, timezone, date
from hashlib import sha256
from pathlib import Path
from dataclasses import dataclass
import logging
import re
import math
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from .admin import require_admin, require_write
from .collect import LOCK, KST
from .db import AlertDelivery, AlertState, Session, DEMO, now
from .settings import load_settings
from .alert_rules import parse_rules
from .dashboard_data import build_dashboard, iso
from . import kr_market as km, kr_value as kv, release_calendar as cal

router = APIRouter(tags=['notifications'])
LIMIT = 20
PER_TICK = 3
LOG = logging.getLogger(__name__)
HEADER = 'Macro Invest | 관측 리포트\n'
DISCLAIMER = '\n수집된 관측값 기준 · 실시간 시세 및 매매 지시 아님.'

class DeliveryError(Exception):
    pass

@dataclass(frozen=True)
class Notice:
    kind: str
    identity: str
    text: str


def configured(cfg):
    return bool(re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,128}', cfg.get('TELEGRAM_BOT_TOKEN','')) and re.fullmatch(r'[1-9][0-9]{0,18}', cfg.get('TELEGRAM_CHAT_ID','')))


def send_telegram(client, cfg, text):
    if not configured(cfg):
        raise DeliveryError('Telegram 토큰 및 개인 Chat ID 확인')
    if not 1 <= len(text) <= 2000 or len(text.encode('utf-16-le')) // 2 > 4096:
        raise DeliveryError('알림 길이 제한')
    # Never propagate exception text, raw response or secret-bearing URLs.
    try:
        response = client.post('https://api.telegram.org/bot'+cfg['TELEGRAM_BOT_TOKEN']+'/sendMessage',
                               json={'chat_id':int(cfg['TELEGRAM_CHAT_ID']), 'text':text, 'link_preview_options':{'is_disabled':True}})
        if response.status_code == 429:
            raise DeliveryError('Telegram 호출 제한 · 자동 재전송 안 함')
        if response.status_code in (401,403):
            raise DeliveryError('Telegram 인증/수신 권한 오류 · 봇 시작 및 설정 확인')
        if response.status_code == 400:
            raise DeliveryError('Telegram 요청 오류 · Chat ID 및 봇 대화 확인')
        response.raise_for_status()
        body = response.json()
        result = body.get('result') if isinstance(body,dict) else None
        if not (isinstance(body,dict) and body.get('ok') is True and isinstance(result,dict) and type(result.get('message_id')) is int and isinstance(result.get('chat'),dict) and result['chat'].get('id')==int(cfg['TELEGRAM_CHAT_ID']) and result['chat'].get('type')=='private'):
            raise DeliveryError('Telegram 성공 응답 확인 불가 · 자동 재전송 안 함')
    except DeliveryError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise DeliveryError('Telegram 연결/응답 오류 · 수신 여부 미확인, 자동 재전송 안 함') from None


def key_for(n):
    return sha256((n.kind+'\0'+n.identity).encode()).hexdigest()


def count_today(day):
    with Session() as db:
        return db.scalar(select(func.count()).select_from(AlertDelivery).where(AlertDelivery.day==day,AlertDelivery.kind!='test')) or 0


def claim(n, clock):
    key=key_for(n)
    try:
        with Session.begin() as db:
            if db.get(AlertDelivery,key): return None
            db.add(AlertDelivery(key=key,day=clock.astimezone(KST).date(),kind=n.kind,state='attempting',created=now(),finished=None,message='전송 시도 기록'))
        return key
    except IntegrityError:
        return None


def deliver(n,cfg,clock,client):
    key=claim(n,clock)
    if not key: return 'duplicate'
    state='sent'; message='Telegram 성공 응답 확인'
    try:
        send_telegram(client,cfg,n.text)
    except DeliveryError as e:
        state='failed'; message=str(e)
    except Exception:
        state='unknown'; message='전송 처리 오류 · 수신 여부 미확인'
    with Session.begin() as db:
        row=db.get(AlertDelivery,key);row.state=state;row.message=message;row.finished=now()
    return state


def interrupt_alerts():
    with Session.begin() as db:
        db.execute(update(AlertDelivery).where(AlertDelivery.state=='attempting').values(state='unknown',finished=now(),message='서버 중단 · 수신 여부 미확인, 재전송 안 함'))


def valid_row_pair(obj,day,field):
    rows=obj.get('rows',[])
    if obj.get('state')!='ok' or obj.get('stale') or len(rows)<2: return None
    a,b=rows[-2:]; x,y=a.get(field),b.get(field)
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (x,y)):return None
    try: d1,d2=date.fromisoformat(a['date']),date.fromisoformat(b['date'])
    except (ValueError,KeyError,TypeError): return None
    if not (0<(d2-d1).days<=10 and 1<=(day-d2).days<=7):return None
    return x,y,b['date']


def crossed(prev,current,side,threshold):
    return prev<threshold<=current if side=='above' else prev>threshold>=current


def report_ready(cfg,clock,d):
    # If collection is scheduled by report time, wait for that day's attempt to finish.
    if cfg.get('AUTO_COLLECT')!='1' or int(cfg['COLLECT_HOUR_KST'])>clock.hour:return True
    last=d.get('last_run')
    if not last or not last.get('started') or not last.get('finished') or last.get('state')=='running':return False
    started=datetime.fromisoformat(last['started'].replace('Z','+00:00')).astimezone(KST)
    return started.date()==clock.date() and started.hour>=int(cfg['COLLECT_HOUR_KST'])


def daily_text(d):
    indicators={x['id']:x for x in d['indicators']}; q=d['quality']; model=d['model']
    lines=[HEADER.rstrip(),f"{d['asof']} KST | 아침 투자 요약",f"신선한 지표 {q['fresh']}/{q['total']} · 오래됨/오류 {q['stale']} · 미수집 {q['missing']}"]
    for sid in ('sp500','nasdaq','kospi','kosdaq','usdkrw','vix','hy','core_pce','us10y','kr_rate'):
        r=indicators.get(sid)
        if r and r['fresh'] and r['value'] is not None:
            lines.append(f"{r['name']}: {r['value']:,.2f} {r['unit']} ({r['period']})")
    for sid,name in [('cli_us','미국'),('cli_kr','한국'),('cli_uk','영국')]:
        c=d['cycles'].get(sid,{})
        lines.append(f"{name} CLI: {c.get('phase','미확인')} ({c.get('date') or '관측일 미확인'})")
    if model['score'] is None: lines.append('위험도·배분 판단 보류')
    else: lines.append(f"위험 {model['score']}/100 · 모형 주식/채권/현금 {'/'.join(map(str,model['allocation']))}% (검증되지 않은 가설)")
    for market in km.MARKETS:
        try:
            obj=km.market_state(market);last=obj.get('latest')
            if obj.get('state')=='ok' and not obj.get('stale') and last:
                lines.append(f"{market} 외국인 {last['foreign']:+,.2f} / 기관 {last['institution']:+,.2f}억원 ({last['date']})")
        except (KeyError,TypeError,ValueError):pass
    lines.append('출처: FRED / OECD / ECOS / KIS · 앱에서 원자료 및 날짜 확인')
    return '\n'.join(lines)+DISCLAIMER


def risk_notice(d,day):
    m=d['model']; band=m.get('label') if m.get('score') is not None else None
    with Session.begin() as db:
        old=db.get(AlertState,'risk'); raw=old.value if old else ''
        try:
            day_str,prior=raw.split('|'); recent=0<=(day-date.fromisoformat(day_str)).days<=2
        except ValueError: prior='';recent=False
        if band not in ('낮음','주의','높음') or not recent or prior not in ('낮음','주의','높음') or band==prior:
            db.merge(AlertState(key='risk',value=f'{day}|{band or "missing"}'))
            return None
    periods=', '.join(f"{x['id']} {x['period']}" for x in d['indicators'] if x['id'] in ('vix','hy','sp500','cli_us'))
    return Notice('risk',f'{day}:{prior}:{band}',f"{HEADER}위험 구간 변경: {prior} → {band}\n점수 {m['score']}/100 (하락 확률 아님)\n{periods}"+DISCLAIMER)


def record_risk(n,day):
    band=n.identity.rsplit(':',1)[-1]
    with Session.begin() as db:
        db.merge(AlertState(key='risk',value=f'{day}|{band}'))


def flow_notices(day):
    for market in km.MARKETS:
        obj=km.market_state(market)
        for field,label in [('foreign','외국인'),('institution','기관')]:
            pair=valid_row_pair(obj,day,field)
            if not pair:continue
            a,b,period=pair
            if a*b<0:
                side='순매수' if b>0 else '순매도'
                yield Notice('flow',f'{market}:{field}:{period}',f'{HEADER}{market} {label} {side} 전환\n{period} {b:+,.2f}억원 (이전 {a:+,.2f}억원)\nKIS 일별 수급'+DISCLAIMER)


def price_notices(cfg,day):
    watch={w['code'] for w in km.watches()}; cache={}
    for code,side,threshold in parse_rules(cfg.get('ALERT_PRICE_RULES','')):
        if code not in watch:continue
        if code not in cache:cache[code]=km.watch_data(code)[0]
        pair=valid_row_pair(cache[code],day,'close')
        if pair:
            a,b,period=pair; target=float(threshold)
            if a>0 and b>0 and crossed(a,b,side,target):
                yield Notice('price',f'{code}:{side}:{threshold}:{period}',f'{HEADER}종목 {code} 종가 조건 도달\n{period} {b:,.2f}원 · 기준 {target:,.2f}원 {"이상" if side=="above" else "이하"}\nKIS 일별 시세'+DISCLAIMER)


def calendar_notices(clock):
    data=cal.calendar_data(days=1,past=0)
    for e in data['events']:
        if e['state']!='예정' or (e['origin']=='BLS 구독' and data['bls_stale']):continue
        dt=datetime.fromisoformat(e['when'])
        if not timedelta(0)<dt-clock<=timedelta(hours=1):continue
        # User-defined title may include private notes: only send official agency and timestamp.
        title=e['title'] if e['origin']=='BLS 구독' else e['agency']+' 등록 일정 (제목은 앱에서 확인)'
        yield Notice('calendar',f"{e['id']}:{dt.astimezone(timezone.utc).isoformat()}",f"{HEADER}경제지표 발표 예정 (1시간 이내)\n{title[:180]}\n{dt.astimezone(KST):%Y-%m-%d %H:%M} KST\n{e['origin']} · 일정 변경 가능"+DISCLAIMER)


def dividend_notices(day):
    for w in km.watches():
        code=w['code']; obj=kv.payload(kv.key('schedule',code,'COMMON'))
        if obj['state']!='ok' or not obj['data']:continue
        for e in obj['data']['items']:
            if e['status'] not in ('예정','확정'):continue
            for field,label in [('record_date','기준일'),('ex_date','배당락일'),('pay_date','지급일')]:
                raw=e.get(field)
                if raw and date.fromisoformat(raw)==day+timedelta(days=1):
                    yield Notice('dividend',f"{code}:{e['receipt']}:{field}:{raw}",f"{HEADER}종목 {code} 배당 {label} 하루 전\n{raw} · {e['status']} (사용자 등록 공시 확인 필요)\n공시 https://dart.fss.or.kr/dsaf001/main.do?rcpNo={e['receipt']}"+DISCLAIMER)


def run_once(clock=None):
    if DEMO or not LOCK.acquire(blocking=False):return []
    try:
        cfg=load_settings()
        if cfg.get('ALERT_ENABLED')!='1' or not configured(cfg):return []
        clock=(clock or datetime.now(timezone.utc)).astimezone(KST);day=clock.date()
        attempts=count_today(day)
        if attempts>=LIMIT:return []
        # Cooldown after provider failures; a successful explicit test can reopen the circuit.
        with Session() as db:
            last=db.scalar(select(AlertDelivery).order_by(AlertDelivery.created.desc()).limit(1))
        if last and last.state in ('failed','unknown','attempting') and now()-last.created<timedelta(minutes=15):return []
        pending=[]
        # Keep each optional source isolated; a broken one must not block the others.
        if cfg['ALERT_CALENDAR']=='1':
            try:pending.extend(calendar_notices(clock))
            except Exception:LOG.warning('Calendar alert evaluation failed')
        if cfg['ALERT_DAILY']=='1' or cfg['ALERT_RISK']=='1':
            try:
                d=build_dashboard(day)
                if cfg['ALERT_DAILY']=='1' and clock.hour>=int(cfg['ALERT_HOUR_KST']) and report_ready(cfg,clock,d):
                    pending.append(Notice('daily',str(day),daily_text(d)))
                if cfg['ALERT_RISK']=='1':
                    n=risk_notice(d,day)
                    if n:pending.append(n)
            except Exception:LOG.warning('Macro alert evaluation failed')
        for setting,fn in [('ALERT_FLOW',lambda:flow_notices(day)),('ALERT_DIVIDEND',lambda:dividend_notices(day)),('ALERT_PRICE_RULES',lambda:price_notices(cfg,day))]:
            if cfg.get(setting) not in ('','0',None):
                try:pending.extend(fn())
                except Exception:LOG.warning('Optional alert evaluation failed')
        results=[]
        with Session() as db:
            existing=set(db.scalars(select(AlertDelivery.key).where(AlertDelivery.day>=day-timedelta(days=7))))
        with httpx.Client(timeout=httpx.Timeout(8,connect=4),follow_redirects=False,trust_env=False) as client:
            for n in pending:
                if len(results)>=PER_TICK or attempts>=LIMIT:break
                if key_for(n) in existing:
                    if n.kind=='risk':record_risk(n,day)
                    continue
                state=deliver(n,cfg,clock,client)
                if n.kind=='risk':record_risk(n,day)
                if state!='duplicate':
                    results.append(state); attempts+=1
                if state in ('failed','unknown'):break # avoid repeated calls when provider is unavailable
        return results
    finally:LOCK.release()


def schedule_alerts(stop):
    if DEMO:return
    while not stop.is_set():
        try:run_once()
        except Exception:LOG.warning('Notification check failed')
        stop.wait(60)


@router.get('/notifications',include_in_schema=False,dependencies=[Depends(require_admin)])
def page():return FileResponse(Path(__file__).parent/'static'/'notifications.html',headers={'Cache-Control':'no-store'})

@router.get('/api/notifications',dependencies=[Depends(require_admin)])
def status():
    cfg=load_settings();day=datetime.now(KST).date()
    with Session() as db: rows=db.scalars(select(AlertDelivery).order_by(AlertDelivery.created.desc()).limit(100)).all()
    return {'demo':DEMO,'configured':configured(cfg),'enabled':cfg['ALERT_ENABLED']=='1','today_attempts':count_today(day),'limit':LIMIT,'history':[{'kind':r.kind,'state':r.state,'at':iso(r.created),'message':r.message} for r in rows]}

class TestRequest(BaseModel):
    confirm:bool=False

@router.post('/api/notifications/test',dependencies=[Depends(require_write)])
def test_delivery(req:TestRequest):
    if DEMO:raise HTTPException(409,'데모에서는 Telegram에 전송하지 않습니다.')
    if not req.confirm:raise HTTPException(400,'테스트 메시지 전송 확인이 필요합니다.')
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'수집/다른 작업 종료 후 실행하세요.')
    try:
        cfg=load_settings()
        if not configured(cfg):raise HTTPException(400,'Telegram 토큰 및 개인 Chat ID 등록 필요')
        with Session() as db:
            last=db.scalar(select(AlertDelivery).where(AlertDelivery.kind=='test').order_by(AlertDelivery.created.desc()).limit(1))
        if last and now()-last.created<timedelta(seconds=60):raise HTTPException(429,'테스트 전송은 60초 간격입니다.')
        clock=datetime.now(timezone.utc)
        n=Notice('test',clock.strftime('%Y%m%d%H%M'),HEADER+'Telegram 연결 테스트 · 실제 투자 자료 미포함')
        with httpx.Client(timeout=httpx.Timeout(8,connect=4),follow_redirects=False,trust_env=False) as client:
            state=deliver(n,cfg,clock,client)
        return {'state':state,'note':'실제 수신 여부를 Telegram 앱에서 확인하세요.'}
    finally:LOCK.release()
