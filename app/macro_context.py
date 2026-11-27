"""Calendar-aware historical context and date-aligned, non-scored diagnostics."""
from bisect import bisect_right
from datetime import timedelta
from math import isfinite
from statistics import mean
from .analysis import month_before, fresh
from .catalog import BY_ID, source_url

# Longest expected daily gap includes ordinary holiday closures; this is not
# an exchange-calendar completeness guarantee. Monthly/quarterly dates exact.
TOLERANCE = {'D': 10, 'W': 8, 'M': 0, 'Q': 0}
MAX_GAP = {'D': 10, 'W': 14, 'M': 35, 'Q': 95}
MIN_FIVE_YEAR = {'D': 1100, 'W': 240, 'M': 60, 'Q': 20}
READ_LIMIT = {'D': 1900, 'W': 290, 'M': 84, 'Q': 32}


def history_context(sid, rows):
    """All deltas use the displayed unit. Rate changes are percentage points,
    not relative percent. Historical comparisons are current-vintage, not
    point-in-time observations or predictions."""
    if not rows:
        return None
    s = BY_ID[sid]
    end, value = rows[-1]
    dates = [d for d, _ in rows]
    out = {'frequency': s.freq, 'observation_count': len(rows),
           'previous_period': str(rows[-2][0]) if len(rows)>1 else None,
           'previous_value': rows[-2][1] if len(rows)>1 else None,
           'windows': {}, 'five_year': None}
    for months in (1, 3, 6, 12):
        if s.freq=='Q' and months==1:
            continue
        target = month_before(end, months)
        if s.freq in ('D','W'):
            # Keep the day of month, clipping to the target month's last day.
            last_day=(month_before(target,-1)-timedelta(days=1)).day
            target=target.replace(day=min(end.day,last_day))
        index=bisect_right(dates,target)-1
        if index<0 or (target-dates[index]).days>TOLERANCE[s.freq]:
            continue
        d, old=rows[index]
        if not isfinite(old) or not isfinite(value):
            continue
        entry={'period':str(d),'value':old,'delta':round(value-old,8)}
        if s.group in ('주가지수','환율','원자재') and old>0 and value>0:
            entry['relative_pct']=round((value/old-1)*100,6)
        out['windows'][str(months)+'m']=entry
    # Exactly five calendar years, require coverage and no large internal gaps.
    cutoff=month_before(end,60)
    if s.freq in ('D','W'):
        last_day=(month_before(cutoff,-1)-timedelta(days=1)).day
        cutoff=cutoff.replace(day=min(end.day,last_day))
    # Prior on/before cutoff verifies start coverage; exclude it if outside window.
    anchor=bisect_right(dates,cutoff)-1
    start=anchor if anchor>=0 and dates[anchor]==cutoff else anchor+1
    part=rows[start:-1]
    if (anchor>=0 and (cutoff-dates[anchor]).days<=TOLERANCE[s.freq]
            and len(part)>=MIN_FIVE_YEAR[s.freq]
            and all(isfinite(v) for _,v in part)
            and all(0<(b[0]-a[0]).days<=MAX_GAP[s.freq] for a,b in zip(rows[anchor:-1],rows[anchor+1:]))):
        vals=[v for _,v in part]
        # Midrank percentile relative to previous observations, excludes latest.
        pct=(sum(v<value for v in vals)+0.5*sum(v==value for v in vals))/len(vals)*100
        out['five_year']={'from':str(part[0][0]),'to':str(part[-1][0]),'count':len(vals),
                          'mean':round(mean(vals),8),'percentile':round(pct,2)}
    return out


def diagnostics(usable, asof, busy=False):
    """Read-only, no interpolation. Latest primary anchor must match source dates."""
    specs=[
        ('net_liquidity_proxy','미국 순유동성 참고치 (연준 자산-TGA-ON RRP)',
         '십억 달러',('fed_assets','tga','rrp'),lambda a,b,c:(a-b)/1000-c,14,
         '동일 수요일 관측값, 연준 자산·TGA 백만달러를 십억달러로 환산. 실제 위험자산 유입액 아님.'),
        ('sofr_iorb_gap','미국 SOFR-IORB 금리차',
         '%p',('sofr','iorb'),lambda a,b:a-b,7,
         '동일 날짜 익일 담보부 조달금리에서 준비금 이자율 차감. 정책·분기말 요인 영향, 단독 위험판정 아님.')
    ]
    out=[]
    for sid,name,unit,ids,formula,max_age,note in specs:
        item={'id':sid,'name':name,'unit':unit,'value':None,'period':None,
              'sources':[source_url(BY_ID[k]) for k in ids],
              'components':list(ids),'note':note,'state':'판단 보류'}
        if not busy and all(fresh(k,usable.get(k,[]),asof) for k in ids):
            d=usable[ids[0]][-1][0]
            maps=[dict(usable[k]) for k in ids]
            if 0<=(asof-d).days<=max_age and all(d in m and isfinite(m[d]) for m in maps):
                values=[m[d] for m in maps]
                if sid!='net_liquidity_proxy' or all(v>=0 for v in values):
                    v=formula(*values)
                    if isfinite(v):
                        item.update(value=round(v,8),period=str(d),state='관측 가능')
        out.append(item)
    return out
