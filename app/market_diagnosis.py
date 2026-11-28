"""Experimental transparent directional checks, not probability or allocation."""
from math import isfinite
from .analysis import STALE
from .catalog import BY_ID

# (series, window, field, threshold, adverse direction, description)
# Thresholds other than existing VIX/HY and Sahm are explicit exploratory rules.
RULES = (
 ('growth','미국 경기·고용',(
 ('sahm',None,'value',.5,1,'Sahm 지표 0.5%p 이상'),
 ('ip',None,'value',0,-1,'산업생산 전년비 음수'),
 ('claims4w','3m','delta',0,1,'실업수당 4주 평균 3개월 증가'))),
 ('inflation','물가·금리',(
 ('core_pce',None,'value',2,1,'근원 PCE 전년비 2% 이상 (참고 기준)'),
 ('real10y','3m','delta',0,1,'10년 실질금리 3개월 상승'),
 ('core_cpi','3m','delta',0,1,'근원 CPI 상승률 3개월 확대'))),
 ('liquidity','유동성',(
 ('reserves','3m','relative_pct',0,-1,'준비금 3개월 감소'),
 ('m2',None,'value',0,-1,'M2 전년비 감소'),
 ('business_loans',None,'value',0,-1,'기업 대출 전년비 감소'))),
 ('credit','신용·금융 스트레스',(
 ('hy',None,'value',4,1,'하이일드 OAS 4%p 이상'),
 ('nfci',None,'value',0,1,'금융여건 지수 양수'),
 ('creditcard_delinquency','6m','delta',0,1,'신용카드 연체율 6개월 상승'))),
 ('trend','주식시장 추세',(
 ('sp500','3m','relative_pct',0,-1,'S&P 500 3개월 가격 하락'),
 ('nasdaq100','3m','relative_pct',0,-1,'나스닥100 3개월 가격 하락'),
 ('vix',None,'value',20,1,'VIX 20 이상'))),
 ('korea','한국 경기·시장',(
 ('kr_ip',None,'value',0,-1,'산업생산 전년비 감소'),
 ('kr_exports',None,'value',0,-1,'상품 수출 전년비 감소'),
 ('kospi','3m','relative_pct',0,-1,'코스피 3개월 가격 하락'))),
 ('semiconductor','반도체·전자부품',(
 ('semi_ip',None,'value',0,-1,'미국 반도체·전자부품 생산 전년비 감소'),
 ('electronics_orders','12m','relative_pct',0,-1,'기타 전자부품 주문 전년비 감소'),
 ('kr_ic_exports','12m','relative_pct',0,-1,'한국 집적회로 수출 전년비 감소'),
 ('sox','3m','relative_pct',0,-1,'SOX 3개월 가격 하락 (업황 자체 아님)'))),
)

def number(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and isfinite(v)

def quality(indicators,busy=False):
    providers={}
    for x in indicators:
        p=providers.setdefault(x['provider'],dict(total=0,fresh=0,stale=0,missing=0,error=0,not_configured=0,pending=0,demo=0))
        p['total']+=1
        valid=number(x.get('value'))
        if x.get('fresh') and valid:p['fresh']+=1
        elif valid:p['stale']+=1
        else:p['missing']+=1
        if x.get('state') in ('error','not_configured','pending','demo'):p[x['state']]+=1
    return {'busy':busy,'providers':providers,'attention':[{'id':x['id'],'name':x['name'],'provider':x['provider'],'state':x.get('state'),'period':x.get('period'),'age_days':x.get('age_days'),'allowed_age_days':STALE[BY_ID[x['id']].freq]} for x in indicators if not x.get('fresh') or not number(x.get('value'))]}

def diagnosis(indicators,busy=False):
    lookup={x['id']:x for x in indicators}; result=[]
    for key,label,rules in RULES:
        checks=[]
        for sid,window,field,threshold,direction,rule in rules:
            x=lookup.get(sid,{})
            ctx=x.get('history_context') or {}
            w=(ctx.get('windows') or {}).get(window,{}) if window else {}
            value=w.get(field) if window else x.get('value')
            valid=not busy and x.get('fresh') and number(value) and bool(x.get('period')) and (not window or bool(w.get('period')))
            adverse=bool(value>=threshold if direction==1 and threshold!=0 else value>threshold if direction==1 else value<threshold) if valid else None
            checks.append({'id':sid,'rule':rule,'window':window,'value':value if valid else None,'unit':'%' if field=='relative_pct' else '%p' if field=='delta' and x.get('unit','').startswith('%') else x.get('unit'),'period':x.get('period') if valid else None,'previous_period':w.get('period') if valid and window else None,'adverse':adverse})
        count=sum(c['adverse'] is not None for c in checks)
        adverse=sum(c['adverse'] is True for c in checks)
        # Strict full coverage for a category conclusion; no denominator drift.
        state='판단 보류' if count!=len(checks) else '경계' if adverse==len(checks) else '혼조' if adverse else '경계 신호 없음'
        result.append({'id':key,'name':label,'state':state,'available':count,'total':len(checks),'adverse_count':adverse,'checks':checks})
    return {'version':'experimental-v1','domains':result,'note':'탐색적 규칙 · 경계 신호 개수는 하락 확률이나 종합 위험 점수가 아님 · 전체 자료 충족 시만 분야 판단 · 기존 배분 모델과 분리'}
