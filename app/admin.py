"""Local admin API. Basic authentication + same-origin write header.
Hosted mode requires an HTTPS ingress and strong administrator authentication.
"""
from datetime import timedelta
import secrets
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from pathlib import Path
from sqlalchemy import select, delete
from .db import DEMO, Session, Status, Run, Obs, Revision, SeriesSource, now
from .catalog import BY_ID, SERIES
from .settings import password, load_settings, public_settings, save_settings, DEFAULTS, QUERIES, clean_query
from .collect import LOCK, today
from .hosting import HOSTED, PUBLIC_ORIGIN
from . import providers as p

router=APIRouter(tags=['admin'])
auth=HTTPBasic(auto_error=False)

def require_admin(credentials:HTTPBasicCredentials|None=Depends(auth)):
    expected=password().encode('utf-8')
    if credentials is None or not (secrets.compare_digest(credentials.username.encode(),'admin'.encode()) and secrets.compare_digest(credentials.password.encode(),expected)):
        raise HTTPException(401,'관리자 인증 필요',headers={'WWW-Authenticate':'Basic realm="Macro Invest Admin", charset="UTF-8"','Cache-Control':'no-store'})
    return 'admin'

def require_write(request:Request, user=Depends(require_admin)):
    if request.headers.get('X-Macro-Admin')!='1': raise HTTPException(403,'관리자 페이지에서 실행하세요.')
    origin=request.headers.get('origin')
    if origin and origin!=(PUBLIC_ORIGIN if HOSTED else str(request.base_url).rstrip('/')): raise HTTPException(403,'다른 사이트에서의 요청을 차단했습니다.')
    return user

@router.get('/admin',include_in_schema=False,dependencies=[Depends(require_admin)])
def admin_page():
    return FileResponse(Path(__file__).parent/'templates'/'admin.html',headers={'Cache-Control':'no-store'})

@router.get('/api/admin/settings',dependencies=[Depends(require_admin)])
def get_settings(): return public_settings()

class SettingsUpdate(BaseModel):
    values:dict[str,str]=Field(max_length=len(DEFAULTS))
    reset_changed_kosis:bool=False

@router.put('/api/admin/settings',dependencies=[Depends(require_write)])
def put_settings(req:SettingsUpdate):
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'수집/연결 검사 종료 후 저장하세요.')
    try:
        if any(len(v)>8000 for v in req.values.values()): raise HTTPException(400,'설정값이 너무 깁니다.')
        try:
            before=load_settings()
            changed_queries={k for k in QUERIES if k in req.values and before[k]!=clean_query(req.values[k])}
            changed_ids=[s.id for s in SERIES if s.code in changed_queries]
            if changed_ids and not DEMO:
                with Session() as db:
                    has_data=db.scalar(select(Obs.series).where(Obs.series.in_(changed_ids)).limit(1)) is not None
                if has_data and not req.reset_changed_kosis:
                    raise HTTPException(409,'KOSIS 통계표 변경 시 해당 지표의 기존 관측값 및 수정 이력 삭제 확인이 필요합니다.')
            after=save_settings(req.values)
        except ValueError as e:
            # Never include raw key/URL input in errors (date.fromisoformat etc.).
            raise HTTPException(400,'설정 형식 오류: API 키, 조회 시작일, KOSIS 항목, 수집 시간을 확인하세요.') from None
        changed={k for k in DEFAULTS if before[k]!=after[k]}
        if not DEMO and changed:
            with Session.begin() as db:
                if changed_ids and req.reset_changed_kosis:
                    db.execute(delete(Obs).where(Obs.series.in_(changed_ids)))
                    db.execute(delete(Revision).where(Revision.series.in_(changed_ids)))
                    db.execute(delete(SeriesSource).where(SeriesSource.series.in_(changed_ids)))
                for s in SERIES:
                    relevant={s.provider+'_API_KEY','HISTORY_START'}
                    if s.provider=='KOSIS': relevant.add(s.code)
                    if changed & relevant:
                        old=db.get(Status,s.id)
                        db.merge(Status(series=s.id,state='pending',message='설정 변경 · 재수집 필요',attempted=now(),success=old.success if old else None))
        return public_settings()
    finally: LOCK.release()

SAMPLES={'FRED':['cpi'],'ECOS':['kr_rate','kr10y','kr3y','usdkrw','kospi','kosdaq'],'OECD':['cli_us','cli_kr','cli_uk'],'KOSIS':['kr_leading','kr_coincident']}

@router.post('/api/admin/test/{provider}',dependencies=[Depends(require_write)])
def test_connection(provider:str):
    if provider not in SAMPLES: raise HTTPException(404,'지원하지 않는 제공처')
    if DEMO: raise HTTPException(409,'데모에서는 외부 API 호출을 하지 않습니다. 실데이터 모드에서 검사하세요.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:
        cfg=load_settings(); end=today(); start=end-timedelta(days=240); results=[]
        with httpx.Client(timeout=httpx.Timeout(12,connect=6),follow_redirects=False,headers={'User-Agent':'MacroInvestPersonal/2.0'}) as client:
            batch=None
            if provider=='OECD':
                try: batch=p.oecd(client,start,end)
                except Exception as e: batch={sid:e for sid in SAMPLES[provider]}
            for sid in SAMPLES[provider]:
                try:
                    series=BY_ID[sid]
                    data=batch[sid] if batch is not None else {'FRED':p.fred,'ECOS':p.ecos,'KOSIS':p.kosis}[provider](client,series,start,end,cfg)
                    if isinstance(data,Exception): raise data
                    if not data: raise p.DataError('데이터 없음')
                    last=max(data)
                    results.append({'series':series.name,'state':'ok','count':len(data),'period':str(last),'value':data[last]})
                except Exception as e:
                    # ECOS diagnostics contain only locally defined messages.
                    if isinstance(e, (p.EcosKeyError, p.EcosResponseError)):
                        msg = str(e)
                    elif isinstance(e, p.NotConfigured):
                        msg = 'API 키 또는 통계표 설정을 확인하세요.'
                    else:
                        msg = 'API 연결/응답 실패 · 키, 항목, 네트워크 및 호출 제한을 확인하세요.'
                    results.append({'series':BY_ID[sid].name,'state':'error','message':msg})
        return {'provider':provider,'ok':all(r['state']=='ok' for r in results),'results':results,'note':'표본 응답 검사이며 원자료 값(변환 전)입니다. 전체 이력 및 지표의 정확성은 별도 확인이 필요합니다.'}
    finally: LOCK.release()

@router.get('/api/admin/status',dependencies=[Depends(require_admin)])
def status():
    def iso(v): return v.isoformat()+'Z' if v else None
    with Session() as db:
        states={x.series:x for x in db.scalars(select(Status))}
        runs=db.scalars(select(Run).order_by(Run.id.desc()).limit(30)).all()
        return {'demo':DEMO,'busy':LOCK.locked(),'series':[{'id':s.id,'name':s.name,'provider':s.provider,'state':states[s.id].state if s.id in states else 'pending','message':states[s.id].message if s.id in states else '수집 전','attempted':iso(states[s.id].attempted) if s.id in states else None,'success':iso(states[s.id].success) if s.id in states else None} for s in SERIES],'runs':[{'id':x.id,'state':x.state,'started':iso(x.started),'finished':iso(x.finished)} for x in runs]}


@router.post('/api/admin/reset-kosis/{sid}',dependencies=[Depends(require_write)])
def reset_kosis(sid:str, confirm:bool=False):
    if sid not in ('kr_leading','kr_coincident'): raise HTTPException(404,'KOSIS 지표만 초기화 가능')
    if DEMO: raise HTTPException(409,'데모에서는 실행하지 않습니다.')
    if not confirm: raise HTTPException(400,'삭제 확인이 필요합니다.')
    if not LOCK.acquire(blocking=False): raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:
        with Session.begin() as db:
            db.execute(delete(Obs).where(Obs.series==sid))
            db.execute(delete(Revision).where(Revision.series==sid))
            db.execute(delete(SeriesSource).where(SeriesSource.series==sid))
            db.merge(Status(series=sid,state='pending',message='이력 초기화 · 재수집 필요',attempted=now(),success=None))
        return {'state':'reset','series':sid}
    finally: LOCK.release()
