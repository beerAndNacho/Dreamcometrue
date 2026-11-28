"""Read-only public company observations. No target prices or trade scores."""
from datetime import date
import re
from .market_diagnosis import number
KIS='https://apiportal.koreainvestment.com/apiservice-category'
DART='https://opendart.fss.or.kr/'
RECEIPT='https://dart.fss.or.kr/dsaf001/main.do?rcpNo='
NOTE='공시·가격·수급의 관측일과 단위를 구분하세요. 주식분할·병합 및 주식 종류, 업종 특성은 별도 확인 필요. 예상 실적·저평가 판정·매매 신호 아님.'
def day(v):
    if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v):return None
    try:return date.fromisoformat(v)
    except ValueError:return None
def published(obj,year,scope,asof,demo):
    if not obj or obj.get('state')!=('demo' if demo else 'ok'):return None,None
    x=obj.get('data')
    if not x or x.get('year')!=year or (scope and x.get('scope')!=scope):return None,None
    if demo:return x,None
    rec=x.get('receipt')
    if not isinstance(rec,str) or not re.fullmatch(r'\d{14}',rec):return None,None
    d=day(rec[:4]+'-'+rec[4:6]+'-'+rec[6:8])
    return (x,d) if d and year<d.year and d<=asof else (None,None)
def usable_rows(obj,asof,demo,n):
    if not obj or obj.get('stale') or obj.get('state')!=('demo' if demo else 'ok'):return None
    rows=(obj.get('rows') or [])[-n:]
    if len(rows)!=n:return None
    dates=[day(r.get('date')) for r in rows]
    if any(d is None for d in dates) or not 1<=(asof-dates[-1]).days<=7:return None
    if any(not 0<(b-a).days<=10 for a,b in zip(dates,dates[1:])):return None
    return rows
def ratio(a,b,scale=1):
    if not number(a) or not number(b) or b<=0:return None
    v=a/b*scale
    return round(v,6) if number(v) else None
def metrics(asof,history,price,flow,scope='CFS',demo=False,busy=False):
    year=asof.year-1
    latest=next((r for r in history if r.get('year')==year),{})
    f,fd=published(latest.get('financial'),year,scope,asof,demo)
    d,dd=published(latest.get('dividend'),year,None,asof,demo)
    vals=(f or {}).get('values') or {};growth=(f or {}).get('growth') or {}
    fs=[RECEIPT+f['receipt']] if f and not demo else [DART]
    ds=[RECEIPT+d['receipt']] if d and not demo else [DART]
    basis=str(year)+' 사업연도 '+('연결' if scope=='CFS' else '별도')+(' · 공시 '+str(fd) if fd else '')
    dbasis=str(year)+' 사업연도 보통주'+(' · 공시 '+str(dd) if dd else '')
    groups=[]
    def item(key,name,value,unit,period,sources,note='',reason='유효 자료 부족'):
        valid=not busy and number(value)
        return {'id':'company_'+key,'name':name,'value':value if valid else None,'unit':unit,'period':str(period) if valid else None,'sources':sources,'note':note,'reason':None if valid else '수집 중' if busy else reason}
    def group(key,name,items):
        groups.append({'id':key,'name':name,'available':sum(x['value'] is not None for x in items),'total':len(items),'items':items})
    group('fundamentals','기업 실적·현금흐름',[
        item('revenue_growth','매출 전년비',growth.get('revenue'),'% YoY',basis,fs,'동일 공시의 전기 비교값.'),
        item('operating_growth','영업이익 전년비',growth.get('operating_profit'),'% YoY',basis,fs,'전기 양수 이익 기준.'),
        item('operating_margin','영업이익률',(f or {}).get('operating_margin'),'%',basis,fs),
        item('roe','ROE',(f or {}).get('roe'),'%',basis,fs,'평균 자기자본 기준.'),
        item('fcf','잉여현금흐름',(f or {}).get('fcf'),'원',basis,fs,'영업현금흐름에서 유형·무형자산 취득 유출 차감.'),
        item('eps','연간 기본 EPS',vals.get('eps'),'원',basis,fs,'직전 연간 실적. 예상 EPS 아님.')])
    p0=usable_rows(price,asof,demo,1);last=p0[-1] if p0 else {}
    pd=day(last.get('date'));p=last.get('close')
    if not number(p) or p<=0:p=None
    eps=vals.get('eps');dps=(d or {}).get('dps')
    if not number(dps) or dps<0:dps=None
    # Same-day publication time unknown: require earlier receipt date.
    per=ratio(p,eps) if f and pd and (demo or fd<pd) else None
    y=ratio(dps,p,100) if d and pd and (demo or dd<pd) else None
    group('valuation','가치·배당 참고',[
        item('close','최근 저장 종가',p,'원',pd,[KIS],'실시간 및 조정주가 아님.'),
        item('annual_multiple','종가 / 직전 연간 기본 EPS',per,'배',str(pd)+' · '+basis,[KIS]+fs,'TTM·예상 PER 아님. 분할·주식 종류 일치 미검증.','양수 EPS·유효 종가·가격일 전 공시 필요'),
        item('annual_dps','연간 보통주 DPS',dps,'원',dbasis,ds,'미래 배당 아님.'),
        item('historical_yield','과거 연간 DPS / 종가',y,'%',str(pd)+' · '+dbasis,[KIS]+ds,'미래 배당수익률 아님.','유효 종가·가격일 전 배당 공시 필요'),
        item('simple_payout','DPS / 기본 EPS',ratio(dps,eps,100),'%',basis+' · '+dbasis,fs+ds,'동일 사업연도 단순 주당 비율. 공시 배당성향과 다를 수 있음.')])
    flows=[];prices=[]
    for n in (5,20,60):
        part=usable_rows(flow,asof,demo,n)
        period=part[0]['date']+' ~ '+part[-1]['date'] if part else None
        for key,label in (('foreign','외국인'),('institution','기관'),('individual','개인')):
            a=[r.get(key) for r in part] if part else []
            v=sum(a) if a and all(number(x) for x in a) else None
            flows.append(item(key+'_'+str(n),label+' '+str(n)+'개 관측일 순매수 합계',round(v,6) if number(v) else None,'억원',period,[KIS],'저장 관측일 합계. 실제 거래일 전체 보장 안 함.'))
        part=usable_rows(price,asof,demo,n)
        period=part[0]['date']+' ~ '+part[-1]['date'] if part else None
        a=[r.get('close') for r in part] if part else []
        v=ratio(a[-1],a[0],100) if a and all(number(x) and x>0 for x in a) else None
        prices.append(item('price_change_'+str(n),str(n)+'개 관측일 첫 종가 대비 마지막 종가',round(v-100,6) if number(v) else None,'%',period,[KIS],'가격 변화만 비교. 배당·환율·분할/병합 조정 및 비용 미반영.'))
    group('flow','투자자 수급',flows);group('trend','종목 가격 추세',prices)
    return {'version':'company-observation-v1','asof':str(asof),'demo':demo,'scope':scope,'business_year':year,'busy':busy,'groups':groups,'note':NOTE}
