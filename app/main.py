from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import threading, csv, io, math
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import update
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from .db import Session, Status, Run, Revision, SeriesSource, DEMO, init, history
from .catalog import SERIES, BY_ID, metadata
from .collect import collect, schedule, today, LOCK
from .analysis import transform, fresh, cycle_fresh, risk, backtest
from .demo import seed
from .admin import router as admin_router, require_write, require_admin, auth
from .settings import password, load_settings, query_fingerprint
from .hosting import HOSTED, HOSTS
from .portfolio import router as portfolio_router
from .middleware import BodyLimitMiddleware

@asynccontextmanager
async def lifespan(app):
    init()
    password()
    with Session.begin() as db:
        db.execute(update(Run).where(Run.state=='running').values(state='interrupted'))
    if DEMO: seed()
    stop=threading.Event(); t=threading.Thread(target=schedule,args=(stop,),daemon=True); t.start()
    yield
    stop.set()
    t.join(timeout=2)

app=FastAPI(title='Macro Invest',version='3.0.0',lifespan=lifespan)
STATIC=Path(__file__).parent/'static'
app.add_middleware(TrustedHostMiddleware,allowed_hosts=HOSTS, www_redirect=False)
app.include_router(admin_router)
app.include_router(portfolio_router)
app.add_middleware(BodyLimitMiddleware, max_bytes=1_000_000)
app.mount('/static',StaticFiles(directory=STATIC),name='static')

@app.middleware('http')
async def security_headers(request:Request, call_next):
    if HOSTED and not (request.method == 'GET' and request.url.path == '/healthz'):
        # Render terminates HTTPS at its ingress. Protect every route, including assets.
        try:
            require_admin(await auth(request))
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={'detail':exc.detail},
                                headers={**(exc.headers or {}), 'Cache-Control':'no-store'})
    if request.method in ('POST','PUT','PATCH'):
        length=request.headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length)>1_000_000):
            return JSONResponse(status_code=413,content={'detail':'요청 크기 제한 초과'})
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    if HOSTED: response.headers['Strict-Transport-Security']='max-age=31536000'
    if request.url.path not in ('/docs','/redoc','/docs/oauth2-redirect'):
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if HOSTED or request.url.path.startswith('/api/') or request.url.path.startswith('/admin'):
        response.headers['Cache-Control']='no-store'
    return response

@app.exception_handler(RequestValidationError)
async def invalid_request(request,exc):
    return JSONResponse(status_code=422,content={'detail':'요청 형식 또는 입력값 범위를 확인하세요.'})

@app.get('/healthz', include_in_schema=False)
def healthz():
    try:
        with Session() as db: db.execute(text('SELECT 1'))
        return {'status':'ok'}
    except Exception:
        return JSONResponse(status_code=503,content={'status':'unavailable'})

@app.get('/')
def index(): return FileResponse(STATIC/'index.html')

def iso(v): return v.isoformat()+'Z' if v else None

@app.get('/api/dashboard')
def dashboard():
    asof=today(); output=[]; rows={}
    cfg=None
    if not DEMO:
        try: cfg=load_settings()
        except RuntimeError: pass
    with Session() as db:
        statuses={r.series:r for r in db.scalars(select(Status))}
        for s in SERIES:
            raw=history(db,s.id,limit=420); r=transform(s.id,raw); rows[s.id]=r; st=statuses.get(s.id)
            last=r[-1] if r else None
            # Status must be okay as well as recent for live signals. Keep stale values visible.
            isfresh=fresh(s.id,r,asof) and bool(st and st.state in ('ok','demo'))
            if s.provider=='KOSIS' and not DEMO and last:
                src=db.get(SeriesSource,s.id)
                isfresh=isfresh and bool(cfg and src and src.fingerprint==query_fingerprint(cfg[s.code]))
            output.append({**metadata(s),'value':last[1] if last else None,'period':str(last[0]) if last else None,'change':last[1]-r[-2][1] if len(r)>1 else None,'fresh':isfresh,'age_days':(asof-last[0]).days if last else None,'trend':[round(v,6) for _,v in r[-30:]],'state':st.state if st else 'pending','message':st.message if st else '수집 전','last_success':iso(st.success) if st else None})
        last_run=db.scalar(select(Run).order_by(Run.id.desc()).limit(1))
    usable={x['id']:rows[x['id']] if x['fresh'] else [] for x in output}
    cycles={k:cycle_fresh(k,usable[k],asof) for k in ('cli_us','cli_kr','cli_uk','kr_leading','kr_coincident')}
    model=risk(usable,asof,cycles['cli_us']['phase'])
    return {'demo':DEMO,'asof':str(asof),'generated_at':datetime.now(timezone.utc).isoformat(), 'quality':{'fresh':sum(x['fresh'] for x in output),'total':len(output),'stale':sum(x['value'] is not None and not x['fresh'] for x in output),'missing':sum(x['value'] is None for x in output)},'indicators':output,'cycles':cycles,'model':model,'busy':LOCK.locked(),'last_run':{'state':last_run.state,'started':iso(last_run.started),'finished':iso(last_run.finished)} if last_run else None}

@app.post('/api/collect',status_code=202,dependencies=[Depends(require_write)])
def trigger():
    if DEMO: raise HTTPException(409,'데모 모드에서는 수집하지 않습니다.')
    if not LOCK.acquire(blocking=False): return {'state':'busy'}
    try: threading.Thread(target=collect,kwargs={'reserved':True},daemon=True).start()
    except Exception:
        LOCK.release(); raise
    return {'state':'started'}

@app.get('/api/series/{sid}')
def series(sid:str,limit:int=Query(600,ge=1,le=20000)):
    if sid not in BY_ID: raise HTTPException(404,'알 수 없는 지표')
    with Session() as db: rows=transform(sid,history(db,sid,limit=limit+12))[-limit:]
    return {'series':metadata(BY_ID[sid]),'demo':DEMO,'points':[{'date':str(d),'value':v} for d,v in rows]}

@app.get('/api/export/{sid}')
def export(sid:str):
    if sid not in BY_ID: raise HTTPException(404,'알 수 없는 지표')
    with Session() as db: rows=transform(sid,history(db,sid))
    out=io.StringIO(); w=csv.writer(out); w.writerow(['date','value','unit','series','synthetic'])
    for d,v in rows: w.writerow([d,v,BY_ID[sid].unit,sid,DEMO])
    return StreamingResponse(iter([out.getvalue()]),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename={sid}.csv'})

@app.get('/api/revisions/{sid}')
def revisions(sid:str,limit:int=Query(100,ge=1,le=1000)):
    if sid not in BY_ID: raise HTTPException(404,'알 수 없는 지표')
    with Session() as db: r=db.scalars(select(Revision).where(Revision.series==sid).order_by(Revision.seen.desc(),Revision.id.desc()).limit(limit)).all()
    return {'demo':DEMO,'note':'수집 이후 최초 관측/변경 이력 (원자료 값), 실제 발표일 아님','revisions':[{'date':str(x.period),'value':x.value,'seen_at':iso(x.seen)} for x in r]}

class TestRequest(BaseModel):
    csv: str=Field(max_length=400_000)
    lag:int=Field(default=2,ge=2,le=12)
    fee_bps:float=Field(default=10,ge=0,le=100)
@app.post('/api/backtest')
def run_test(req:TestRequest):
    try:
        reader=csv.DictReader(io.StringIO(req.csv.lstrip('\ufeff')))
        if not reader.fieldnames or not {'month','stock','bond','cash'}.issubset(reader.fieldnames): raise ValueError('헤더: month,stock,bond,cash')
        prices=[]
        for row in reader:
            d=date.fromisoformat(row['month']+'-01'); vals=[float(row[k]) for k in ('stock','bond','cash')]
            if not all(math.isfinite(v) for v in vals): raise ValueError('유한한 숫자만 허용')
            if d>=today().replace(day=1): raise ValueError('완료된 과거 월만 허용')
            prices.append((d,vals))
        with Session() as db: cli=history(db,'cli_us')
        return {**backtest(cli,prices,req.lag,req.fee_bps),'demo':DEMO}
    except (ValueError,KeyError,TypeError,OverflowError,ZeroDivisionError,csv.Error) as e: raise HTTPException(400,'CSV 형식, 월별 연속성, 양수 총수익지수 및 계산 범위를 확인하세요. '+(str(e)[:180] if isinstance(e,ValueError) and not isinstance(e,UnicodeError) else '')) from None


@app.get('/api/report')
def daily_report():
    """Live snapshot, not AI forecasts or a historical point-in-time report."""
    d=dashboard(); by_id={x['id']:x for x in d['indicators']}
    title=f"Macro Invest | {d['asof']} KST 투자 관측 리포트"
    lines=[title,'='*48, '가상 데이터 — 실제 투자 금지' if d['demo'] else '최근 수집값 기준 (실시간 시세 아님)',
           f"신선한 지표 {d['quality']['fresh']}/{d['quality']['total']} | 오래됨/오류 {d['quality']['stale']} | 미수집 {d['quality']['missing']}", '', '시장 및 경제지표']
    for sid in ('kospi','kosdaq','sp500','nasdaq','usdkrw','vix','hy','us10y','us2y','curve','fed','kr_rate','core_pce','cpi','unrate','claims','m2','nfci'):
        x=by_id[sid]; v=x['value']; diff=x['change']
        lines.append(f"- {x['name']}: {v:,.2f} {x['unit']}" if v is not None else f"- {x['name']}: 미수집")
        if v is not None: lines[-1]+=f" | {x['period']} | 직전 대비 {diff:+,.2f}" if diff is not None else f" | {x['period']}"
        lines[-1]+=f" | {'확인' if x['fresh'] else '신호 사용 불가'} | {x['url']}"
    lines.extend(['','경기 국면 (100 기준, 3개월 방향)'])
    for sid,c in d['cycles'].items(): lines.append(f"- {by_id[sid]['name']}: {c['phase']} / {c['reason']}")
    m=d['model']; lines.extend(['','미국 시장 위험도 및 모델 자산배분'])
    if m['score'] is None: lines.append('판단 보류: '+', '.join(m['missing']))
    else:
        lines.append(f"위험 점수 {m['score']}/100 ({m['label']}) | 주식/채권/현금: {' / '.join(map(str,m['allocation']))}%")
        for c in m['checks']: lines.append(f"- {c['label']}: {c['value']} ({c['points']}점)")
    lines.extend(['','위험 점수는 하락 확률이 아닙니다. 모델은 검증되지 않은 가설이며 매매 지시가 아닙니다.', '경제지표는 수정될 수 있습니다. 이용 및 재배포는 원 제공처의 라이선스를 따릅니다.'])
    return PlainTextResponse('\n'.join(lines),headers={'Content-Disposition':f'attachment; filename=macro-report-{d["asof"]}.txt'})
