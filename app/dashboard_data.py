"""Shared read-only dashboard computation for live display and collection snapshots."""
from datetime import datetime, timezone
from sqlalchemy import select
from .db import Session, Status, Run, SeriesSource, DEMO, history
from .catalog import SERIES, metadata
from .analysis import transform, fresh, cycle_fresh, risk
from .macro_context import history_context, diagnostics, READ_LIMIT
from .settings import load_settings, query_fingerprint

def iso(v): return v.isoformat()+'Z' if v else None

def build_dashboard(asof, busy=False):
    output=[]; rows={}
    cfg=None
    if not DEMO:
        try: cfg=load_settings()
        except RuntimeError: pass
    with Session() as db:
        statuses={r.series:r for r in db.scalars(select(Status))}
        for s in SERIES:
            raw=history(db,s.id,limit=READ_LIMIT[s.freq]); r=transform(s.id,raw); rows[s.id]=r; st=statuses.get(s.id)
            last=r[-1] if r else None
            # Status must be okay as well as recent for live signals. Keep stale values visible.
            isfresh=fresh(s.id,r,asof) and bool(st and st.state in ('ok','demo'))
            identity=s.provider+':'+s.code+':'+s.item
            if s.provider=='KOSIS' and not DEMO and last:
                src=db.get(SeriesSource,s.id)
                identity=src.fingerprint if src else None
                isfresh=isfresh and bool(cfg and src and src.fingerprint==query_fingerprint(cfg[s.code]))
            output.append({**metadata(s),'value':last[1] if last else None,'period':str(last[0]) if last else None,'change':last[1]-r[-2][1] if len(r)>1 else None,'fresh':isfresh,'age_days':(asof-last[0]).days if last else None,'source_identity':identity,'previous_period':str(r[-2][0]) if len(r)>1 else None,'change_pct':((last[1]/r[-2][1]-1)*100) if len(r)>1 and s.group in ('주가지수','환율','원자재') and r[-2][1]>0 and last[1]>0 else None,'trend':[round(v,6) for _,v in r[-30:]],'state':st.state if st else 'pending','message':st.message if st else '수집 전','last_success':iso(st.success) if st else None,'history_context':history_context(s.id,r) if isfresh and not busy else None})
        last_run=db.scalar(select(Run).order_by(Run.id.desc()).limit(1))
    usable={x['id']:rows[x['id']] if x['fresh'] else [] for x in output}
    cycles={s.id:cycle_fresh(s.id,usable[s.id],asof) for s in SERIES if s.group=='경기선행'}
    model=risk(usable,asof,cycles['cli_us']['phase'])
    if busy:
        model={'score':None,'allocation':None,'missing':['수집 진행 중: 완료 후 다시 평가'],'checks':[]}
    return {'demo':DEMO,'asof':str(asof),'generated_at':datetime.now(timezone.utc).isoformat(), 'quality':{'fresh':sum(x['fresh'] for x in output),'total':len(output),'stale':sum(x['value'] is not None and not x['fresh'] for x in output),'missing':sum(x['value'] is None for x in output)},'indicators':output,'cycles':cycles,'model':model,'diagnostics':diagnostics(usable,asof,busy),'busy':busy,'last_run':{'state':last_run.state,'started':iso(last_run.started),'finished':iso(last_run.finished)} if last_run else None}

