"""Single-instance operations: consistent DB snapshots and read-only diagnostics.
Backups contain private DB data, never settings files. Restore only with server stopped.
"""
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import threading
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select, text
from .admin import require_admin, require_write
from .catalog import BY_ID
from .analysis import STALE
from .collect import LOCK, KST, today, retry_indicator
from .db import Base, DEMO, engine, Session, Obs, Revision, Status, SeriesSource, Run
from .settings import load_settings, private_write, query_fingerprint

VERSION='5.0.0'
router=APIRouter(tags=['operations'])
LIMIT=128*1024*1024
RESERVE=128*1024*1024
ID=re.compile(r'^\d{8}T\d{6}Z-[a-f0-9]{12}$')
STARTED=time.monotonic()
class OpsError(RuntimeError): pass

def root():
    default=Path(os.getenv('SETTINGS_DIR','./data/settings')).parent/'backups'
    p=Path(os.getenv('BACKUP_DIR',str(default)))/('demo' if DEMO else 'live')
    p.mkdir(parents=True,exist_ok=True,mode=0o700)
    return p.resolve()

@contextmanager
def instance_lock():
    """Cooperating server/restore process guard, on the same backup volume."""
    path=root()/'.instance.lock'
    with path.open('a+b') as f:
        f.seek(0,os.SEEK_END)
        if not f.tell(): f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: raise OpsError('다른 서버/복원 작업이 실행 중입니다. 먼저 중지하세요.') from None
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def backend(): return engine.dialect.name

def db_path():
    p=engine.url.database
    if backend()!='sqlite' or not p or p==':memory:' or engine.url.query: raise OpsError('파일 기반 SQLite만 지원합니다.')
    return Path(p).resolve()

def utc(): return datetime.now(timezone.utc)
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def sqlite_read(path): return sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)

def sqlite_check(path,check_schema=True):
    with closing(sqlite_read(path)) as db:
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]: raise OpsError('SQLite 무결성 검사 실패')
        if check_schema:
            for table in Base.metadata.sorted_tables:
                # All identifiers are local ORM table names, never input.
                cols={x[1] for x in db.execute(f'PRAGMA table_info("{table.name}")')}
                if not set(table.columns.keys())<=cols: raise OpsError('백업 DB 스키마가 현재 프로그램과 호환되지 않습니다.')

def pg_run(args,db=False,timeout=120):
    binary=shutil.which(args[0])
    if not binary: raise OpsError('PostgreSQL 백업 도구(pg_dump/pg_restore)가 설치되지 않았습니다.')
    env=os.environ.copy()
    if db:
        u=engine.url
        # Avoid password in arguments or logs; use libpq environment.
        if not u.database or not u.username or not u.host or set(u.query)-{'sslmode'}: raise OpsError('지원하지 않는 PostgreSQL 접속 설정입니다.')
        for k in ('PGSERVICE','PGSERVICEFILE','PGHOSTADDR','PGPASSFILE','PGOPTIONS','PGDATABASE','PGUSER','PGPASSWORD','PGHOST','PGPORT','PGSSLMODE'): env.pop(k,None)
        env.update(PGHOST=u.host,PGPORT=str(u.port or 5432),PGUSER=u.username,PGPASSWORD=u.password or '',PGDATABASE=u.database,PGCONNECT_TIMEOUT='10')
        if u.query.get('sslmode'): env['PGSSLMODE']=str(u.query['sslmode'])
    try:
        subprocess.run([binary,*args[1:]],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=timeout)
    except (subprocess.SubprocessError,OSError): raise OpsError('PostgreSQL 백업/복원 도구 실행 실패. 접속 권한·도구 버전·디스크를 확인하세요.') from None

def ext(kind):
    if kind=='sqlite': return '.sqlite3'
    if kind=='postgresql': return '.dump'
    raise OpsError('지원하지 않는 데이터베이스입니다.')

def item(id):
    if not ID.fullmatch(id): raise OpsError('백업 ID 형식 오류')
    p=root()/(id+'.json')
    try:
        if p.is_symlink() or p.stat().st_size>8192: raise ValueError()
        m=json.loads(p.read_text('utf-8'))
        if not isinstance(m,dict) or m.get('id')!=id or m.get('format')!='macro-db-v1' or m.get('demo') is not DEMO or m.get('backend')!=backend(): raise ValueError()
        file=root()/(id+ext(m['backend']))
        if file.is_symlink() or not file.is_file() or type(m.get('size')) is not int or not 0<m['size']<=LIMIT or file.stat().st_size!=m['size'] or not re.fullmatch(r'[a-f0-9]{64}',m.get('sha256','')): raise ValueError()
        if m.get('reason') not in ('manual','automatic','before-restore') or not re.fullmatch(r'\d+\.\d+\.\d+',m.get('version','')):raise ValueError()
        dt=datetime.fromisoformat(m['created']);day=datetime.strptime(m['day'],'%Y-%m-%d').date()
        if dt.tzinfo is None or dt.astimezone(KST).date()!=day:raise ValueError()
        # Do not relay arbitrary fields in metadata.
        return {k:m[k] for k in ('id','format','demo','backend','created','day','version','size','sha256','reason')},file
    except (OSError,ValueError,KeyError,TypeError): raise OpsError('백업 파일 또는 메타데이터가 없거나 손상되었습니다.') from None

def list_items():
    found=[]
    for path in sorted(root().glob('*.json'),reverse=True):
        if not ID.fullmatch(path.stem): continue
        try: found.append(item(path.stem)[0])
        except OpsError: pass
    return sorted(found,key=lambda x:(x['created'],x['id']),reverse=True)

def verify(id):
    m,p=item(id)
    try:
        if sha(p)!=m['sha256']: raise OpsError('백업 SHA-256 불일치')
        if backend()=='sqlite': sqlite_check(p)
        else: pg_run(['pg_restore','--list',str(p)])
        return m,p
    except (OSError,sqlite3.Error):raise OpsError('백업 파일 검사 실패') from None

def create_backup(reason='manual'):
    if reason not in ('manual','automatic','before-restore'): raise OpsError('백업 종류 오류')
    directory=root(); kind=backend(); suffix=ext(kind)
    if shutil.disk_usage(directory).free<RESERVE+LIMIT: raise OpsError('백업 공간이 부족합니다. 최소 256 MiB 여유가 필요합니다.')
    if reason=='manual' and sum(m['reason']!='automatic' for m in list_items())>=10: raise OpsError('수동 및 복원 직전 백업 10개 제한. 다운로드 후 서버에서 이전 백업을 정리하세요.')
    stamp=utc(); id=stamp.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:12]
    fd,name=tempfile.mkstemp(prefix='.backup-',dir=directory);os.close(fd);tmp=Path(name)
    final=directory/(id+suffix); metadata=directory/(id+'.json')
    try:
        if kind=='sqlite':
            deadline=time.monotonic()+120
            with closing(sqlite_read(db_path())) as src,closing(sqlite3.connect(tmp)) as dst:
                if src.execute('PRAGMA page_count').fetchone()[0]*src.execute('PRAGMA page_size').fetchone()[0]>LIMIT: raise OpsError('DB가 백업 크기 제한을 초과합니다.')
                def progress(status,remaining,total):
                    if time.monotonic()>deadline or tmp.stat().st_size>LIMIT: raise OpsError('SQLite 백업 시간/크기 제한 초과')
                src.backup(dst,pages=256,sleep=.05,progress=progress)
                dst.execute('PRAGMA journal_mode=DELETE')
            sqlite_check(tmp)
        else:
            with Session() as db:
                if db.scalar(text('SELECT pg_database_size(current_database())'))>LIMIT: raise OpsError('DB가 백업 크기 제한을 초과합니다.')
            pg_run(['pg_dump','--format=custom','--no-owner','--no-privileges','--file',str(tmp)],db=True)
            pg_run(['pg_restore','--list',str(tmp)])
        size=tmp.stat().st_size
        if not 0<size<=LIMIT: raise OpsError('백업 크기 제한 초과')
        m={'id':id,'format':'macro-db-v1','demo':DEMO,'backend':kind,'created':stamp.isoformat(),'day':stamp.astimezone(KST).date().isoformat(),'version':VERSION,'size':size,'sha256':sha(tmp),'reason':reason}
        with tmp.open('rb') as f: os.fsync(f.fileno())
        os.replace(tmp,final)
        private_write(metadata,json.dumps(m,ensure_ascii=False).encode())
        return m
    except OpsError: raise
    except (OSError,sqlite3.Error,ValueError): raise OpsError('DB 백업 실패 · 파일/권한/저장 공간을 확인하세요.') from None
    finally:
        tmp.unlink(missing_ok=True)
        if not metadata.exists(): final.unlink(missing_ok=True)

def prune_automatic(keep):
    # Only successful automatic backups are rotated; manual/pre-restore backups stay.
    for m in [x for x in list_items() if x['reason']=='automatic'][keep:]:
        try:
            _,p=item(m['id']);p.unlink();(root()/(m['id']+'.json')).unlink()
        except (OSError,OpsError): pass

def run_state():
    p=root()/'last-attempt.json'
    try:
        if p.is_symlink() or p.stat().st_size>4096:return None
        s=json.loads(p.read_text('utf-8'))
        if not isinstance(s,dict):return None
        return {k:s.get(k) for k in ('day','at','state','message')}
    except (OSError,ValueError):return None

def record_attempt(state,message):
    private_write(root()/'last-attempt.json',json.dumps({'day':str(today()),'at':utc().isoformat(),'state':state,'message':message},ensure_ascii=False).encode())

def auto_once():
    if DEMO:return
    cfg=load_settings();local=datetime.now(KST)
    if cfg['BACKUP_ENABLED']!='1' or local.hour<int(cfg['BACKUP_HOUR_KST']): return
    if (run_state() or {}).get('day')==str(local.date()): return
    if not LOCK.acquire(blocking=False):return
    try:
        try:
            # Durable attempt before doing work; no tight retry loop after a crash.
            record_attempt('running','자동 백업 실행 중')
            create_backup('automatic');prune_automatic(int(cfg['BACKUP_KEEP']));record_attempt('ok','자동 백업 성공')
        except Exception:
            record_attempt('error','자동 백업 실패 · 디스크/DB 및 백업 도구를 확인하세요.')
    finally: LOCK.release()

def schedule_backups(stop):
    if DEMO:return
    while not stop.is_set():
        try:auto_once()
        except Exception:pass
        stop.wait(60)

@router.get('/operations',include_in_schema=False,dependencies=[Depends(require_admin)])
def page():return FileResponse(Path(__file__).parent/'static'/'operations.html',headers={'Cache-Control':'no-store'})

@router.get('/api/operations',dependencies=[Depends(require_admin)])
def status():
    cfg=load_settings();space=shutil.disk_usage(root()); db_bytes=None;peak_rss=None
    try:
        import resource,sys
        peak_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    except (ImportError,AttributeError):pass
    if backend()=='sqlite':
        try:
            p=db_path();db_bytes=p.stat().st_size;wal=Path(str(p)+'-wal')
            if wal.exists():db_bytes+=wal.stat().st_size
        except (OSError,OpsError):pass
    with Session() as db:
        if backend()=='postgresql':db_bytes=db.scalar(text('SELECT pg_database_size(current_database())'))
        counts={t.name:db.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}
        runs=db.scalars(select(Run).order_by(Run.id.desc()).limit(5)).all()
        statuses={r.series:r for r in db.scalars(select(Status))}
        indicators=[{'id':sid,'name':s.name,'provider':s.provider,'state':statuses[sid].state if sid in statuses else 'pending','success':statuses[sid].success.isoformat()+'Z' if sid in statuses and statuses[sid].success else None} for sid,s in BY_ID.items()]
    return {'version':VERSION,'demo':DEMO,'backend':backend(),'busy':LOCK.locked(),'uptime_seconds':int(time.monotonic()-STARTED),'disk_free':space.free,'db_bytes':db_bytes,'process_cpu_seconds':round(time.process_time(),1),'peak_rss_bytes':peak_rss,'disk_total':space.total,'counts':counts,'backups':list_items(),'automatic':{k:cfg[k] for k in ('BACKUP_ENABLED','BACKUP_HOUR_KST','BACKUP_KEEP')},'last_attempt':run_state(),'backup_limit':LIMIT,'indicators':indicators,'postgres_tools':bool(shutil.which('pg_dump') and shutil.which('pg_restore')),'runs':[{'state':r.state,'started':r.started.isoformat()+'Z','finished':r.finished.isoformat()+'Z' if r.finished else None} for r in runs]}

@router.post('/api/operations/backup',dependencies=[Depends(require_write)])
def backup_now():
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:return create_backup()
    except OpsError as e:raise HTTPException(400,str(e)) from None
    finally:LOCK.release()

@router.post('/api/operations/backups/{id}/verify',dependencies=[Depends(require_write)])
def verify_now(id:str):
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:return {'ok':True,'backup':verify(id)[0]}
    except OpsError as e:raise HTTPException(400,str(e)) from None
    finally:LOCK.release()

@router.get('/api/operations/backups/{id}/download',dependencies=[Depends(require_admin)])
def download(id:str,metadata:bool=False):
    # Open before releasing lock: rotation cannot invalidate an opened file on POSIX.
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:
        m,p=item(id)
        if metadata:
            from fastapi.responses import JSONResponse
            return JSONResponse(m,headers={'Content-Disposition':f'attachment; filename="{id}.json"','Cache-Control':'no-store'})
        if sha(p)!=m['sha256']:raise OpsError('백업 SHA-256 불일치')
        f=p.open('rb')
    except (OpsError,OSError):raise HTTPException(404,'다운로드 가능한 검증된 백업이 없습니다.') from None
    finally:LOCK.release()
    def stream():
        try:
            yield from iter(lambda:f.read(1024*1024),b'')
        finally:f.close()
    return StreamingResponse(stream(),media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="{p.name}"','Content-Length':str(m['size']),'Cache-Control':'no-store'})

@router.get('/api/operations/integrity',dependencies=[Depends(require_admin)])
def integrity():
    """Read-only bounded findings. Unknown series / invalid values / source drift."""
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:
        cfg=load_settings();asof=today();issues=[];checked=0
        with Session() as db:
            grouped=db.execute(select(Obs.series,func.count(),func.max(Obs.period)).group_by(Obs.series)).all()
            counts={sid:n for sid,n,_ in grouped};latest={sid:d for sid,_,d in grouped}
            statuses={s.series:s for s in db.scalars(select(Status))}
            for sid,count in counts.items():
                if sid not in BY_ID:issues.append({'series':sid,'issue':'정의되지 않은 계열','count':count})
            for sid,s in BY_ID.items():
                if not counts.get(sid):issues.append({'series':sid,'issue':'관측값 없음','count':0})
                elif (asof-latest[sid]).days>STALE[s.freq]:issues.append({'series':sid,'issue':'최근 원자료 관측일 오래됨','count':(asof-latest[sid]).days})
                if sid in statuses and statuses[sid].state in ('error','not_configured'):issues.append({'series':sid,'issue':'최근 수집 실패 또는 설정 없음','count':None})
                if s.provider=='KOSIS' and counts.get(sid) and not DEMO:
                    src=db.get(SeriesSource,sid)
                    if not src or src.fingerprint!=query_fingerprint(cfg[s.code]):issues.append({'series':sid,'issue':'KOSIS 출처 불일치','count':counts[sid]})
            for model in (Obs,Revision):
                bad={}
                for sid,day,value in db.execute(select(model.series,model.period,model.value).execution_options(yield_per=2000)):
                    checked+=1
                    if day>asof or not math.isfinite(value):bad[sid]=bad.get(sid,0)+1
                for sid,count in bad.items():issues.append({'series':sid,'issue':model.__tablename__+' 미래 날짜 또는 비유한 수치','count':count})
        return {'checked':checked,'issues':issues,'note':'읽기 전용 검사 · 경제지표 관측값/수정이력의 기본 형식과 KOSIS 출처 확인. 거래일 완전성 및 원자료 정확성은 별도 확인 필요.'}
    finally:LOCK.release()

@router.post('/api/operations/retry/{sid}',status_code=202,dependencies=[Depends(require_write)])
def retry(sid:str):
    if sid not in BY_ID:raise HTTPException(404,'알 수 없는 경제지표')
    if DEMO:raise HTTPException(409,'데모에서는 실제 수집을 하지 않습니다.')
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:threading.Thread(target=retry_indicator,args=(sid,),daemon=True).start()
    except Exception:
        LOCK.release();raise HTTPException(503,'수집 작업을 시작하지 못했습니다.') from None
    return {'state':'started','series':sid,'note':'선택한 지표만 전체 이력을 다시 수집합니다. 예약 전체 수집 기록/일별 스냅샷은 변경하지 않습니다.'}

@router.delete('/api/operations/backups/{id}',dependencies=[Depends(require_write)])
def remove_backup(id:str,confirm:bool=False):
    if not confirm:raise HTTPException(400,'백업 삭제 확인 필요')
    if not LOCK.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:
        m,p=item(id)
        # Keep at least one other hash-verified snapshot. Never assume metadata alone is proof.
        alternatives=[x for x in list_items() if x['id']!=id]
        valid=False
        for candidate in alternatives:
            try:verify(candidate['id']);valid=True;break
            except OpsError:pass
        if not valid:raise OpsError('다른 검증된 백업이 없습니다. 마지막 백업은 삭제할 수 없습니다.')
        p.unlink();(root()/(id+'.json')).unlink()
        return {'state':'deleted','id':id}
    except (OpsError,OSError) as e:raise HTTPException(400,str(e) if isinstance(e,OpsError) else '백업 파일 삭제 실패') from None
    finally:LOCK.release()
