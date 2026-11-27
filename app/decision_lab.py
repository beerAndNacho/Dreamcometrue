"""Research-only price strategy and portfolio diagnostics on synchronized US sessions.
No forecasts, orders, personalized targets or changes to the macro allocation model.
"""
from math import sqrt, isfinite
from statistics import mean, stdev
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from . import global_market as g
from .admin import require_admin, require_write
from .collect import LOCK
from .db import DEMO

router=APIRouter(tags=['decision-lab'])
PERIODS=Literal[60,120,252,360]
NOTE='USD 수정주가 가격 연구. 배당·환율·세금 제외. 과거 성과와 가정은 미래 수익률이나 매매 권고가 아닙니다.'

def load_aligned(symbols,required):
    """Require each symbol to have the same complete latest session window, no forward-fill."""
    snapshots={s:g.state(s,True) for s in dict.fromkeys(symbols)}
    reasons={s:a['quality'] for s,a in snapshots.items() if a['quality']!='분석 가능'}
    for s,a in snapshots.items():
        if s not in reasons and len(a['rows'])<required:reasons[s]=f'{required}개 거래일 필요 (현재 {len(a["rows"])})'
    if reasons:return None,reasons
    dates=[r['date'] for r in next(iter(snapshots.values()))['rows'][-required:]]
    prices={}
    for s,a in snapshots.items():
        rows=a['rows'][-required:]
        if [r['date'] for r in rows]!=dates:reasons[s]='공통 거래일 불일치';continue
        vals=[r.get('close') for r in rows]
        if any(type(v) not in (int,float) or not isfinite(v) or not 1e-8<=v<=1e9 for v in vals):reasons[s]='유효하지 않은 종가';continue
        if any(not .01<=b/a<=100 for a,b in zip(vals,vals[1:])):reasons[s]='일간 가격 급변 · 원자료/수정주가 확인 필요';continue
        prices[s]=vals
    return (None,reasons) if reasons else ((dates,prices),{})

def returns(values):return [b/a-1 for a,b in zip(values,values[1:])]

def curve(rs):
    a=[100.0]
    for r in rs:
        v=a[-1]*(1+r)
        if not isfinite(v) or v<=0:raise ValueError('portfolio NAV out of range')
        a.append(v)
    return a

def metrics(values,periods=None):
    if any(not isfinite(v) or v<=0 for v in values):raise ValueError('NAV out of range')
    rs=returns(values); peak=values[0]; mdd=0.; underwater=longest=0
    for v in values:
        if v>=peak:peak=v;underwater=0
        else:underwater+=1;longest=max(longest,underwater);mdd=min(mdd,v/peak-1)
    n=periods if periods is not None else len(rs)
    vol=stdev(rs)*sqrt(252)*100 if len(rs)>1 else None
    cagr=((values[-1]/values[0])**(252/n)-1)*100 if n and values[-1]>0 else None
    return {'return_pct':(values[-1]/values[0]-1)*100,'annualized_pct':cagr,'volatility_pct':vol,'mdd_pct':mdd*100,'max_underwater_sessions':longest}

def simulate_strategy(closes,start,rule,fee_pct):
    """Signal from t-1 close; trade at t close; new position earns t->t+1 return.
    No lookahead, no terminal forced liquidation, cash earns zero. Start at common first close.
    """
    fee=fee_pct/100
    def target(i):
        if rule=='hold':return 1
        if i<0:raise ValueError('insufficient warm-up')
        if rule=='ma200':return int(closes[i]>mean(closes[i-199:i+1]))
        if rule=='ma60':return int(closes[i]>mean(closes[i-59:i+1]))
        if rule=='cross2060':return int(mean(closes[i-19:i+1])>mean(closes[i-59:i+1]))
        raise ValueError('unknown strategy')
    if start<200 or start>=len(closes)-1:raise ValueError('insufficient warm-up/evaluation')
    equity=100.; pos=0; path=[equity]; trades=0; turnover=0.; invested=0
    for i in range(start,len(closes)):
        if i>start:
            equity*=1+pos*(closes[i]/closes[i-1]-1)
            invested+=pos
        # Last observation is mark-to-market only: no unobservable future holding interval.
        if i<len(closes)-1:
            new=target(i-1)
            if new!=pos:
                equity*=1-fee*abs(new-pos);trades+=1;turnover+=abs(new-pos);pos=new
        # Keep the first pre-trade NAV as reference; fold entry costs into the next interval.
        if i>start:path.append(equity)
    result=metrics(path,len(closes)-1-start)
    return {'curve':path,**result,'trades':trades,'turnover_pct':turnover*100,'invested_pct':invested/(len(closes)-1-start)*100}

def cov(x,y):
    if len(x)!=len(y) or len(x)<2:return None
    mx,my=mean(x),mean(y)
    return sum((a-mx)*(b-my) for a,b in zip(x,y))/(len(x)-1)

def corr(x,y):
    a,b=cov(x,x),cov(y,y)
    return max(-1,min(1,cov(x,y)/sqrt(a*b))) if a is not None and b is not None and a>0 and b>0 else None

class StrategyRequest(BaseModel):
    symbol:str
    benchmark:str='VOO'
    periods:PERIODS=252
    fee_pct:float=Field(default=.1,ge=0,le=2,allow_inf_nan=False,strict=True)
    @model_validator(mode='after')
    def assets(self):
        if self.symbol not in g.ASSETS or self.benchmark not in g.ASSETS:raise ValueError('지원 종목 확인')
        return self

class Position(BaseModel):
    symbol:str
    weight:float=Field(ge=0,le=100,allow_inf_nan=False,strict=True)
    shock_pct:float=Field(default=-20,ge=-100,le=100,allow_inf_nan=False,strict=True)

class PortfolioRequest(BaseModel):
    positions:list[Position]=Field(min_length=1,max_length=6)
    benchmark:str='VOO'
    periods:PERIODS=252
    loss_budget_pct:float=Field(default=10,ge=0,le=100,allow_inf_nan=False,strict=True)
    @model_validator(mode='after')
    def weights(self):
        names=[p.symbol for p in self.positions]
        if self.benchmark not in g.ASSETS or any(s not in g.ASSETS for s in names) or len(set(names))!=len(names):raise ValueError('지원 종목/중복 확인')
        total=sum(p.weight for p in self.positions)
        if total>100+1e-9 or total<=0:raise ValueError('비중 합계는 0% 초과 100% 이하')
        return self

def strategy_result(req,dates,prices):
    start=len(dates)-req.periods-1
    runs=[]
    for code,label,s in [('hold','선택 종목 장기보유',req.symbol),('ma60','60일선 전략',req.symbol),('ma200','200일선 전략',req.symbol),('cross2060','20/60일선 전략',req.symbol),('hold','비교 ETF 장기보유',req.benchmark)]:
        runs.append({'label':label,'symbol':s,'rule':code,**simulate_strategy(prices[s],start,code,req.fee_pct)})
    return {'status':'ok','dates':dates[start:],'strategies':runs,'symbol':req.symbol,'benchmark':req.benchmark,'fee_pct':req.fee_pct,'note':NOTE+' 신호는 직전 종가, 체결 가정은 다음 종가. 거래비용은 편도 적용, 현금 수익률 0%, 최종 청산 없음.'}

def portfolio_result(req,dates,prices):
    positions=[p for p in req.positions if p.weight>0]; names=[p.symbol for p in positions]
    rs={s:returns(prices[s]) for s in set(names+[req.benchmark])}
    combined=[sum(rs[p.symbol][i]*p.weight/100 for p in positions) for i in range(req.periods)]
    nav=curve(combined); bench=curve(rs[req.benchmark]); benchmark_var=cov(rs[req.benchmark],rs[req.benchmark]); stress=sum(p.weight*p.shock_pct/100 for p in positions)
    total=sum(p.weight for p in positions)
    return {'status':'ok','dates':dates,'curve':nav,'benchmark_curve':bench,'benchmark':req.benchmark,'metrics':metrics(nav),'benchmark_metrics':metrics(bench),'beta':cov(combined,rs[req.benchmark])/benchmark_var if benchmark_var and benchmark_var>0 else None,'cash_pct':max(0,100-total),'max_weight_pct':max(p.weight for p in positions),'stress_pct':stress,'loss_budget_pct':req.loss_budget_pct,'stress_exceeds_budget':stress<-req.loss_budget_pct,'correlation':{'symbols':names,'values':[[corr(rs[a],rs[b]) for b in names] for a in names]},'relative':[{'symbol':p.symbol,'weight':p.weight,'shock_pct':p.shock_pct,'return_pct':(prices[p.symbol][-1]/prices[p.symbol][0]-1)*100,'relative_pp':(prices[p.symbol][-1]/prices[p.symbol][0]-prices[req.benchmark][-1]/prices[req.benchmark][0])*100,'volatility_pct':stdev(rs[p.symbol])*sqrt(252)*100} for p in positions],'note':NOTE+' 목표 비중을 매 거래일 유지하는 가상 포트폴리오(거래비용 제외), 무이자 현금. 충격 가정은 확률·최대 가능 손실이 아닙니다. ETF 내부 구성종목·레버리지·업종 중복 미확인.'}

def with_data(symbols,required,fn):
    if not LOCK.acquire(blocking=False):return {'status':'withheld','reasons':{'시장':'수집 진행 중'},'demo':DEMO}
    try:
        aligned,reasons=load_aligned(symbols,required)
        if reasons:return {'status':'withheld','reasons':reasons,'demo':DEMO}
        try:return {**fn(*aligned),'demo':DEMO}
        except (ArithmeticError,ValueError):return {'status':'withheld','reasons':{'계산':'수치 범위 초과 · 원자료 확인 필요'},'demo':DEMO}
    finally:LOCK.release()

@router.get('/decision-lab',include_in_schema=False,dependencies=[Depends(require_admin)])
def page():return FileResponse(Path(__file__).parent/'static'/'decision-lab.html')

@router.post('/api/decision-lab/strategy',dependencies=[Depends(require_write)])
def strategy(req:StrategyRequest):
    return with_data([req.symbol,req.benchmark],req.periods+201,lambda dates,prices:strategy_result(req,dates,prices))

@router.post('/api/decision-lab/portfolio',dependencies=[Depends(require_write)])
def portfolio(req:PortfolioRequest):
    return with_data([p.symbol for p in req.positions if p.weight>0]+[req.benchmark],req.periods+1,lambda dates,prices:portfolio_result(req,dates,prices))
