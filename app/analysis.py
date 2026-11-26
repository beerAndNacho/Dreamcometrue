from datetime import date
from bisect import bisect_right
from statistics import mean
from math import isfinite
from .catalog import BY_ID

STALE={'D':7,'W':21,'M':75,'Q':190}
ALLOC={'회복':[70,20,10],'확장':[65,25,10],'둔화':[45,35,20],'하강':[30,40,30]}

def month_before(d,n):
    ix=d.year*12+d.month-1-n
    return date(ix//12,ix%12+1,1)

def transform(sid,rows):
    mode=BY_ID[sid].transform
    if mode=='level': return rows
    lookup=dict(rows); lag=12 if mode=='yoy' else 1; out=[]
    for d,v in rows:
        old=lookup.get(month_before(d,lag))
        if old is not None and (mode!='yoy' or old!=0): out.append((d,(v/old-1)*100 if mode=='yoy' else v-old))
    return out

def fresh(sid,rows,asof):
    return bool(rows) and 0<=(asof-rows[-1][0]).days<=STALE[BY_ID[sid].freq]

def cycle(rows):
    if not rows: return {'phase':'미확인','reason':'데이터 없음'}
    d,v=rows[-1]; old=dict(rows).get(month_before(d,3))
    if old is None: return {'phase':'미확인','reason':'3개월 전 관측치 없음'}
    change=v-old
    if abs(change)<0.01: phase='보합'
    elif change>0: phase='확장' if v>=100 else '회복'
    else: phase='둔화' if v>=100 else '하강'
    return {'phase':phase,'value':v,'change':change,'date':str(d),'reason':f'기준 100, 3개월 변화 {change:+.2f}pt'}

def cycle_fresh(sid,rows,asof):
    r=cycle(rows)
    if not fresh(sid,rows,asof): return {**r,'phase':'미확인','reason':'자료 없음 또는 관측 시점 오래됨'}
    return r

def risk(rows,asof,phase):
    required=['vix','hy','sp500']
    missing=[BY_ID[s].name for s in required if not fresh(s,rows.get(s,[]),asof)]
    sp=rows.get('sp500',[])
    if any(not isfinite(v) or v<=0 for _,v in sp[-200:]): missing.append('S&P 500 유효 가격')
    if any(not isfinite(v) or v<0 for k in ('vix','hy') for _,v in rows.get(k,[])[-1:]): missing.append('위험 지표 유효값')
    if len(sp)<200 and 'S&P 500 가격지수' not in missing: missing.append('S&P 500 200거래일 이력')
    if len(sp)>=200 and ((sp[-1][0]-sp[-200][0]).days>330 or any((b[0]-a[0]).days>10 for a,b in zip(sp[-200:-1],sp[-199:]))): missing.append('S&P 500 200거래일 이력 불연속')
    if phase not in ALLOC: missing.append('미국 OECD CLI 국면')
    if missing: return {'score':None,'allocation':None,'missing':missing,'checks':[]}
    v=rows['vix'][-1][1]; h=rows['hy'][-1][1]; p=sp[-1][1]; ma=mean(x[1] for x in sp[-200:]);
    checks=[{'label':'VIX','value':round(v,2),'points':30 if v>=30 else 15 if v>=20 else 0,'rule':'20 / 30 이상'},
            {'label':'하이일드 OAS','value':round(h,2),'points':30 if h>=6 else 15 if h>=4 else 0,'rule':'4 / 6%p 이상'},
            {'label':'S&P 500 vs 200일선','value':round((p/ma-1)*100,2),'points':20 if p<ma else 0,'rule':'200일선 하회'},
            {'label':'CLI 국면','value':phase,'points':20 if phase=='하강' else 10 if phase=='둔화' else 0,'rule':'둔화 / 하강'}]
    score=sum(c['points'] for c in checks)
    equity,bond,cash=ALLOC[phase]; reduction=20 if score>=70 else 10 if score>=40 else 0
    return {'score':score,'allocation':[equity-reduction,bond,cash+reduction],'checks':checks,'missing':[], 'label':'높음' if score>=70 else '주의' if score>=40 else '낮음'}

def backtest(cli,prices,lag=2,fee_bps=10):
    """Exploratory monthly total-return backtest using revised CLI; no live risk overlays."""
    if not 2<=lag<=12 or not isfinite(fee_bps) or not 0<=fee_bps<=100: raise ValueError('lag 2~12개월, 비용 0~100bp')
    cli=sorted(cli); ds=[d for d,v in cli]; prices=sorted(prices,key=lambda x:x[0]);
    if len(prices)<13: raise ValueError('최소 13개월 데이터가 필요합니다.')
    for i,(d,values) in enumerate(prices):
        if d.day!=1 or len(values)!=3 or any(not isfinite(v) or not 1e-12<=v<=1e15 for v in values): raise ValueError('YYYY-MM 및 양수 총수익 지수 3개 필요')
        if i and month_before(d,1)!=prices[i-1][0]: raise ValueError('월별 자료가 중복되거나 누락되었습니다.')
    equity=1.; bench=1.; previous_weights=None; result=[]; peak=1.; mdd=0.; bpeak=1.; bmdd=0.
    for i in range(1,len(prices)):
        d,vals=prices[i]; prev_date,prev=prices[i-1]; cutoff=month_before(prev_date,lag)
        j=bisect_right(ds,cutoff); c=cycle(cli[:j]); phase=c['phase']
        if not j or (cutoff-ds[j-1]).days>65 or phase not in ALLOC:
            if result: raise ValueError('백테스트 중 CLI 누락/보합: 기간 변경 필요')
            continue
        weights=[w/100 for w in ALLOC[phase]]
        turnover=sum(abs(a-b) for a,b in zip(weights,previous_weights))/2 if previous_weights is not None else 1.
        cost=turnover*fee_bps/10000; growth=[v/p for v,p in zip(vals,prev)]; gross=sum(w*g for w,g in zip(weights,growth))
        equity*=gross*(1-cost); bench*=growth[0]*(1-fee_bps/10000 if not result else 1)
        previous_weights=[w*g/gross for w,g in zip(weights,growth)]
        peak=max(peak,equity); mdd=min(mdd,equity/peak-1); bpeak=max(bpeak,bench); bmdd=min(bmdd,bench/bpeak-1)
        result.append({'date':str(d),'strategy':equity,'benchmark':bench,'phase':phase})
    if len(result)<12: raise ValueError('CLI와 겹치는 유효 수익 구간이 12개월 미만입니다.')
    n=len(result)
    if not all(isfinite(v) and v>0 for v in (equity,bench)): raise ValueError('수익률 계산 범위 초과')
    return {'months':n,'strategy_return':equity-1,'benchmark_return':bench-1,'cagr':equity**(12/n)-1,'benchmark_cagr':bench**(12/n)-1,'mdd':mdd,'benchmark_mdd':bmdd,'points':result,'warning':'수정된 CLI 사용·발표 지연 가정·CLI 단독 규칙. 실시간 위험도 오버레이 및 세금/환율 미반영. 미래정보 편향 제거 검증 아님.'}
