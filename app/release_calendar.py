"""Economic releases: official BLS calendar subscription and explicit user-confirmed events.
No guessed release dates, no automatic consensus estimates. All actual instants are UTC.
"""
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
import re, json, uuid, threading
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, Session, DEMO, now
from .admin import require_admin, require_write
from .collect import KST, today
from . import kr_market as km

router=APIRouter(tags=['economic-calendar'])
BLS_URL='https://www.bls.gov/schedule/news_release/bls.ics'
SOURCES={'BLS':'https://www.bls.gov/schedule/','BEA':'https://www.bea.gov/news/schedule','FED':'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm','BOK':'https://www.bok.or.kr/','KOSIS':'https://kosis.kr/','OECD':'https://www.oecd.org/'}
HOSTS={'bls.gov','bea.gov','federalreserve.gov','bok.or.kr','kosis.kr','oecd.org','kostat.go.kr','korea.kr'}
GUARD=threading.Lock()
CACHE='calendar:bls'

class CalendarEntry(Base):
    __tablename__='economic_calendar_manual'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    payload:Mapped[str]=mapped_column(Text)


def source_ok(v):
    try:
        u=urlsplit(v)
        return (u.scheme=='https' and not u.username and not u.password and u.port in (None,443)
                and any(u.hostname==h or (u.hostname or '').endswith('.'+h) for h in HOSTS)
                and not u.query and not u.fragment and not re.search(r'\s|[<>\\]',v))
    except ValueError:return False


def local_instant(dt,zone):
    """Reject ambiguous/nonexistent wall times rather than guess a DST fold."""
    tz=ZoneInfo(zone)
    options={a.astimezone(timezone.utc) for fold in (0,1) if (a:=dt.replace(tzinfo=tz,fold=fold)).astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)==dt}
    if len(options)!=1:raise ValueError('발표 시각이 서머타임 전환 구간입니다. 공식 UTC 시각을 입력하세요.')
    return options.pop()


def unescape_ical(s):
    return re.sub(r'\\([nN,;\\])',lambda m:' ' if m[1].lower()=='n' else m[1],s).strip()


def parse_bls(text):
    if len(text)>2_000_000 or not text.lstrip('\ufeff\r\n ').startswith('BEGIN:VCALENDAR') or not text.rstrip().endswith('END:VCALENDAR'):
        raise ValueError('BLS 캘린더 형식 오류')
    lines=re.sub(r'\r?\n[ \t]','',text).splitlines(); raw=None; events={}
    for line in lines:
        if line=='BEGIN:VEVENT':
            if raw is not None:raise ValueError('VEVENT 중첩')
            raw={};continue
        if line=='END:VEVENT':
            if raw is None:raise ValueError('VEVENT 오류')
            x=raw;raw=None
            if x.get('STATUS',('', ''))[1].upper()=='CANCELLED':continue
            if 'RRULE' in x or 'RDATE' in x or 'RECURRENCE-ID' in x:raise ValueError('반복 일정 형식 미지원')
            if not all(k in x for k in ('UID','DTSTART','SUMMARY')):raise ValueError('필수 일정 필드 누락')
            uid=x['UID'][1]; summary=unescape_ical(x['SUMMARY'][1]); params,s=x['DTSTART']
            # All-day holidays are not timed releases; do not invent a midnight time.
            if re.fullmatch(r'\d{8}',s):continue
            if not uid or not summary or len(uid)>500 or len(summary)>300:raise ValueError('일정 제목/UID 오류')
            if s.endswith('Z'):stamp=datetime.strptime(s,'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            else:
                dt=datetime.strptime(s,'%Y%m%dT%H%M%S')
                tzmatch=re.search(r'(?:^|;)TZID="?([^;"\r\n]+)"?(?:;|$)',params,re.I)
                tz=tzmatch[1] if tzmatch else 'America/New_York' # BLS publishes Eastern Time.
                if tz in ('Eastern Standard Time','US/Eastern'):tz='America/New_York'
                if tz not in ('America/New_York','UTC','Etc/UTC'):raise ValueError('BLS 시간대 미지원')
                stamp=local_instant(dt,tz)
            eid='bls-'+__import__('hashlib').sha256(uid.encode()).hexdigest()[:24]
            row={'id':eid,'title':summary,'agency':'BLS','source':SOURCES['BLS'],'when':stamp.isoformat(),'state':'예정','origin':'BLS 구독','unit':'','actual':None,'previous':None,'expected':None,'result_source':None}
            if eid in events and events[eid]!=row:raise ValueError('BLS UID 충돌')
            events[eid]=row
        elif raw is not None and ':' in line:
            k,v=line.split(':',1); name=k.split(';',1)[0].upper()
            if name in ('UID','DTSTART','SUMMARY','STATUS','RRULE','RDATE','RECURRENCE-ID'):
                if name in raw:raise ValueError('중복 필드')
                raw[name]=(k,v)
    if raw is not None or not events or len(events)>5000:raise ValueError('유효 BLS 일정 없음')
    return sorted(events.values(),key=lambda x:x['when'])


def fetch_bls(client):
    with client.stream('GET',BLS_URL,headers={'Accept':'text/calendar','User-Agent':'MacroInvestPersonal/4.5'}) as r:
        r.raise_for_status();content=bytearray()
        for chunk in r.iter_bytes():
            content.extend(chunk)
            if len(content)>2_000_000:raise ValueError('BLS 크기 초과')
    return parse_bls(content.decode('utf-8-sig'))


class Entry(BaseModel):
    id:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    title:str=Field(min_length=1,max_length=160)
    agency:Literal['BLS','BEA','FED','BOK','KOSIS','OECD','기타']
    day:date
    time:str=Field(pattern=r'^\d{2}:\d{2}$')
    zone:Literal['Asia/Seoul','America/New_York','Europe/London','UTC']
    source:str=Field(max_length=500)
    state:Literal['예정','발표','취소']='예정'
    unit:str=Field(default='',max_length=40)
    actual:float|None=Field(default=None,allow_inf_nan=False,ge=-1e15,le=1e15)
    previous:float|None=Field(default=None,allow_inf_nan=False,ge=-1e15,le=1e15)
    expected:float|None=Field(default=None,allow_inf_nan=False,ge=-1e15,le=1e15)
    result_source:str|None=Field(default=None,max_length=500)
    confirm:bool=False
    @model_validator(mode='after')
    def check(self):
        if not self.confirm or not self.title.strip() or not source_ok(self.source):raise ValueError('공식 출처 및 확인 필요')
        if not date(2000,1,1)<=self.day<=today()+timedelta(days=730):raise ValueError('날짜 범위 오류')
        stamp=local_instant(datetime.combine(self.day,datetime.strptime(self.time,'%H:%M').time()),self.zone)
        if self.state=='발표' and (stamp>datetime.now(timezone.utc) or self.actual is None):raise ValueError('미래 발표 또는 실제값 누락')
        if self.actual is not None and (self.state!='발표' or not self.result_source or not source_ok(self.result_source)):raise ValueError('공식 결과 출처 및 발표 상태 필요')
        if any(v is not None for v in (self.actual,self.previous,self.expected)) and not self.unit.strip():raise ValueError('동일 단위 입력 필요')
        return self
    def row(self):
        stamp=local_instant(datetime.combine(self.day,datetime.strptime(self.time,'%H:%M').time()),self.zone)
        return {'id':self.id or uuid.uuid4().hex,'title':self.title.strip(),'agency':self.agency,'when':stamp.isoformat(),'source':self.source,'state':self.state,'origin':'사용자 확인','unit':self.unit,'actual':self.actual,'previous':self.previous,'expected':self.expected,'result_source':self.result_source if self.actual is not None else None}


def demo_entries():
    d=today();return [{'id':'demo-'+str(i),'title':name,'agency':agency,'when':datetime.combine(d+timedelta(days=offset),datetime.strptime('22:30','%H:%M').time(),KST).astimezone(timezone.utc).isoformat(),'source':SOURCES[agency],'state':'예정','origin':'가상 일정','unit':'','actual':None,'previous':None,'expected':None,'result_source':None} for i,(name,agency,offset) in enumerate([('미국 고용보고서','BLS',2),('미국 CPI','BLS',5),('미국 PCE','BEA',12),('FOMC 금리 결정','FED',16)])]


def calendar_data(days=30, past=7):
    with Session() as db:manual=[json.loads(x.payload) for x in db.scalars(select(CalendarEntry))]
    cache=km.cached(CACHE) if not DEMO else {'rows':demo_entries(),'state':'demo','updated':None,'message':'가상 일정'}
    clock=datetime.now(timezone.utc);start=today()-timedelta(days=past);end=today()+timedelta(days=days);out=[]
    for x in cache['rows']+manual:
        stamp=datetime.fromisoformat(x['when']); kst=stamp.astimezone(KST)
        if start<=kst.date()<=end:
            status=x['state'] if x['state']!='예정' else '시간 경과 · 결과 미확인' if stamp<=clock else '예정'
            out.append({**x,'kst':kst.isoformat(),'display_state':status,'surprise':x['actual']-x['expected'] if x['actual'] is not None and x['expected'] is not None else None})
    stale=cache['state'] not in ('ok','demo') or (cache['state']=='ok' and (not cache['updated'] or (clock-datetime.fromisoformat(cache['updated'].replace('Z','+00:00'))).days>8))
    return {'demo':DEMO,'events':sorted(out,key=lambda x:x['when']),'bls':{k:cache[k] for k in ('state','updated','message')},'bls_stale':stale,'sources':SOURCES,'asof':clock.isoformat()}


@router.get('/api/calendar',dependencies=[Depends(require_admin)])
def get_calendar(days:int=Query(30,ge=1,le=180),past:int=Query(7,ge=0,le=180)):
    return calendar_data(days,past)

@router.post('/api/calendar/bls',dependencies=[Depends(require_write)])
def refresh_bls():
    return _refresh_bls()

def refresh_if_due():
    if DEMO:return
    prior=km.cached(CACHE)
    if not prior['attempted'] or (datetime.now(timezone.utc)-datetime.fromisoformat(prior['attempted'].replace('Z','+00:00'))).total_seconds()>=86400:
        return _refresh_bls()

def _refresh_bls():
    if DEMO:raise HTTPException(409,'데모에서는 외부 일정을 조회하지 않습니다.')
    if not GUARD.acquire(blocking=False):raise HTTPException(409,'캘린더 갱신 중')
    try:
        previous=km.cached(CACHE);attempt=previous['attempted']
        if attempt and (datetime.now(timezone.utc)-datetime.fromisoformat(attempt.replace('Z','+00:00'))).total_seconds()<60:
            raise HTTPException(429,'최소 60초 후 재시도하세요.')
        try:
            with httpx.Client(timeout=15,follow_redirects=False,trust_env=False) as c:rows=fetch_bls(c)
            km.store(CACHE,rows,merge=False)
        except (httpx.HTTPError,ValueError,UnicodeError):
            km.store(CACHE,error='BLS 캘린더 조회 실패 · 이전 일정 유지',merge=False)
            raise HTTPException(502,'BLS 조회 실패 · 이전 일정 보존. 공식 페이지 확인 필요') from None
        return calendar_data()
    finally:GUARD.release()

@router.put('/api/calendar/event',dependencies=[Depends(require_write)])
def put_event(req:Entry):
    if DEMO:raise HTTPException(409,'데모 모드에서는 실제 일정을 저장하지 않습니다.')
    row=req.row()
    with GUARD,Session.begin() as db:
        if req.id and db.get(CalendarEntry,req.id) is None:raise HTTPException(404,'등록된 일정 없음')
        if not req.id and len(db.scalars(select(CalendarEntry.id).limit(1000)).all())>=1000:raise HTTPException(409,'최대 1000개 일정')
        db.merge(CalendarEntry(id=row['id'],payload=json.dumps(row,ensure_ascii=False,allow_nan=False)))
    return row

@router.delete('/api/calendar/event/{eid}',dependencies=[Depends(require_write)])
def delete_event(eid:str):
    if DEMO:raise HTTPException(409,'데모 모드에서는 실제 일정을 삭제하지 않습니다.')
    with GUARD,Session.begin() as db:
        x=db.get(CalendarEntry,eid)
        if not x:raise HTTPException(404,'등록된 일정 없음')
        db.delete(x)
    return {'state':'deleted'}
