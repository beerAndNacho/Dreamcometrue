import math,random
from datetime import timedelta
from sqlalchemy import select
from .db import Session, Obs, Status, write_series, now
from .catalog import SERIES
from .collect import today
from .analysis import month_before

def seed():
    with Session.begin() as db:
        existing=set(db.scalars(select(Obs.series).distinct()))
        rng=random.Random(71); end=today(); base=end.replace(day=1)
        anchors={'vix':19,'hy':3.6,'sp500':5400,'nasdaq':17500,'kospi':2900,'kosdaq':850,'usdkrw':1350,'kr_rate':2.75,'fed':4.5,'unrate':4.1,'us10y':4.1,'us2y':3.9,'us3m':4.3,'curve':0.2,'curve3m':-0.2,'kr10y':3.4,'kr3y':3.1,'oil':72,'claims':240000,'nfci':-0.3}
        for i,s in enumerate(SERIES):
            if s.freq=='M': dates=[month_before(base,n) for n in range(121,0,-1)]
            elif s.freq=='Q': dates=[month_before(base,n) for n in range(120,0,-1) if month_before(base,n).month in (1,4,7,10)]
            elif s.freq=='W': dates=[end-timedelta(days=n*7) for n in range(260,0,-1)]
            else: dates=[end-timedelta(days=n) for n in range(740,0,-1) if (end-timedelta(days=n)).weekday()<5]
            if s.id in existing: continue
            vals={}
            for j,d in enumerate(dates):
                if s.group=='경기선행': val=100+1.8*math.sin(j/8+i/2)
                elif s.transform in ('yoy','diff'): val=100*(1.0025**j)*(1+0.01*math.sin(j/6+i)); val*=1000 if s.id=='payroll' else 1
                else:
                    a=anchors.get(s.id,100); val=a+max(abs(a)*.07,.1)*math.sin(j/18+i)+max(abs(a)*.01,.01)*rng.random()
                vals[d]=round(val,5)
            write_series(db,s.id,vals); db.merge(Status(series=s.id,state='demo',message='가상 데이터 — 투자에 사용 금지',attempted=now(),success=now()))
