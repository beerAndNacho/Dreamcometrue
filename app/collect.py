import os, threading, time, math, logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import select
from .db import Session, Status, Run, Obs, SeriesSource, DEMO, init, now, write_series, history
from .catalog import SERIES
from . import providers as p
from .settings import load_settings, query_fingerprint

# HTTPX INFO logging can expose keys embedded in URLs.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
LOCK=threading.Lock()
KST=ZoneInfo('Asia/Seoul')
def today(): return datetime.now(KST).date()

def collect_indicators(cfg,start,end,selected,full=True):
    """Caller holds LOCK; shared validation for full and targeted collection."""
    oecd_result=None; counts={'ok':0,'error':0,'not_configured':0}
    with httpx.Client(timeout=httpx.Timeout(30,connect=10),follow_redirects=False,headers={'User-Agent':'MacroInvestPersonal/2.0'}) as client:
        for s in selected:
            try:
                request_start=start
                with Session() as db:
                    previous=db.get(Status,s.id)
                    latest=history(db,s.id,limit=1)
                    source=db.get(SeriesSource,s.id)
                    if s.provider=='KOSIS' and latest and (not source or source.fingerprint!=query_fingerprint(cfg[s.code])):
                        raise p.DataError('KOSIS 출처 확인 불가/변경됨: 관리자에서 해당 이력 초기화 후 수집')
                    # On ordinary scheduled days, fetch an overlap window for daily series.
                    # First run, stale history, settings change and weekly/manual runs use full history.
                    if not full and s.freq=='D' and previous and previous.state=='ok' and latest:
                        request_start=max(start,min(end-timedelta(days=45),latest[-1][0]-timedelta(days=7)))
                if s.provider=='OECD':
                    if oecd_result is None:
                        try: oecd_result=p.oecd(client,start,end)
                        except Exception as e: oecd_result={x.id:e for x in SERIES if x.provider=='OECD'}
                    data=oecd_result[s.id]
                    if isinstance(data,Exception): raise data
                else: data={'FRED':p.fred,'ECOS':p.ecos,'KOSIS':p.kosis}[s.provider](client,s,request_start,end,cfg)
                data={d:v for d,v in data.items() if start<=d<=end and math.isfinite(v)}
                if not data: raise p.DataError('조회 기간 내 데이터 없음')
                with Session.begin() as db:
                    changed=write_series(db,s.id,data)
                    if s.provider=='KOSIS': db.merge(SeriesSource(series=s.id,fingerprint=query_fingerprint(cfg[s.code])))
                    db.merge(Status(series=s.id,state='ok',message=f'{len(data):,}건 조회 / {changed:,}건 신규·수정',attempted=now(),success=now()))
                counts['ok']+=1
            except Exception as e:
                state='not_configured' if isinstance(e,p.NotConfigured) else 'error'
                # Never expose URLs/API keys from transport exceptions.
                msg=str(e) if isinstance(e,(p.NotConfigured,p.DataError)) else type(e).__name__
                with Session.begin() as db:
                    old=db.get(Status,s.id)
                    db.merge(Status(series=s.id,state=state,message=msg,attempted=now(),success=old.success if old else None))
                counts[state]+=1
            if s.provider=='FRED' and cfg['FRED_API_KEY']: time.sleep(0.6)
    return counts

def retry_indicator(sid):
    """Caller reserved LOCK; does not change the full daily collection schedule."""
    try:
        from .catalog import BY_ID
        if DEMO: return
        cfg=load_settings(); start=date.fromisoformat(cfg['HISTORY_START']); end=today()
        if start>end: raise ValueError('HISTORY_START is in the future')
        return collect_indicators(cfg,start,end,[BY_ID[sid]],full=True)
    except Exception:
        logging.getLogger(__name__).warning('Targeted collection failed before completion')
        try:
            with Session.begin() as db:
                old=db.get(Status,sid)
                db.merge(Status(series=sid,state='error',message='개별 재수집 실행 실패 · 서버 설정과 DB 상태 확인',attempted=now(),success=old.success if old else None))
        except Exception:pass
    finally:LOCK.release()

def collect(reserved=False, full=True):
    if DEMO:
        if reserved: LOCK.release()
        return {'state':'demo'}
    if not reserved and not LOCK.acquire(blocking=False): return {'state':'busy'}
    try:
        with Session.begin() as db:
            run=Run(started=now(),state='running'); db.add(run); db.flush(); rid=run.id
        try:
            cfg=load_settings(); start=date.fromisoformat(cfg['HISTORY_START']); end=today()
            if start>end: raise ValueError('HISTORY_START is in the future')
            counts=collect_indicators(cfg,start,end,SERIES,full=full)
            if cfg.get('KIS_APP_KEY') and cfg.get('KIS_APP_SECRET'):
                try:
                    from .kr_market import collect_market
                    collect_market(cfg)
                except Exception:
                    logging.getLogger(__name__).warning('Optional Korean market collection failed; macro data preserved')
            if cfg.get('KIS_APP_KEY') and cfg.get('KIS_APP_SECRET') and cfg.get('GLOBAL_AUTO_SYMBOLS'):
                try:
                    from .global_market import collect_configured
                    collect_configured(cfg)
                except Exception:
                    logging.getLogger(__name__).warning('Optional overseas collection failed; macro data preserved')
            if cfg.get('BLS_AUTO_REFRESH')=='1':
                try:
                    from .release_calendar import refresh_if_due
                    refresh_if_due()
                except Exception:
                    logging.getLogger(__name__).warning('Optional BLS calendar refresh failed; prior calendar retained')
            state='ok' if counts['ok']==len(SERIES) else 'partial' if counts['ok'] else 'error'
            return {'state':state,**counts}
        except Exception:
            state='error'; raise
        finally:
            with Session.begin() as db:
                run=db.get(Run,rid); run.state=state; run.finished=now()
            if state in ('ok','partial'):
                try:
                    from .dashboard_data import build_dashboard
                    from .briefing import save_daily_snapshot
                    day=today(); save_daily_snapshot(day,build_dashboard(day))
                except Exception:
                    logging.getLogger(__name__).warning('Daily snapshot failed; collection data preserved')
    finally: LOCK.release()

def schedule(stop):
    if DEMO: return
    while not stop.is_set():
        try:
            cfg=load_settings()
            hour=max(0,min(23,int(cfg['COLLECT_HOUR_KST'])))
        except Exception:
            stop.wait(60); continue
        if cfg['AUTO_COLLECT']!='1':
            stop.wait(15); continue
        try:
            with Session() as db: last=db.scalar(select(Run).order_by(Run.id.desc()).limit(1))
        except Exception:
            stop.wait(60); continue
        local=datetime.now(KST)
        last_date=last.started.replace(tzinfo=ZoneInfo('UTC')).astimezone(KST).date() if last else None
        if local.hour>=hour and (last_date!=local.date() or (last and last.started.replace(tzinfo=ZoneInfo('UTC')).astimezone(KST).hour<hour)):
            try: collect(full=local.weekday()==6)
            except Exception: pass  # Run status reflects failure; retry manually or next day.
        stop.wait(60)

if __name__=='__main__':
    init(); print(collect())
