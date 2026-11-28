"""Read-only semiconductor/other electronic components observations.

Keep nominal trade, physical output, price indices and stock price separate.
Never combine HS8542 and its subset HS854232 or infer memory contract prices.
"""
from .catalog import BY_ID, source_url
from .analysis import month_before, fresh
from .market_diagnosis import number

GROUPS=(
 ('demand','수요·수출',(
  ('electronics_orders','12m'),('electronics_backlog','12m'),('kr_ic_exports','12m'),('kr_memory_exports','12m'))),
 ('production','생산·출하',(('semi_ip',None),('electronics_shipments','12m'))),
 ('inventory','재고',(('electronics_inventory','12m'),('inventory_ratio_yoy',None))),
 ('prices','수출입·생산자 물가',(('semi_import_price',None),('semi_export_price',None),('semi_ppi',None),('electronics_import_price',None))),
 ('market','주식시장 (업황과 별도)',(('sox','3m'),('nasdaq100','3m'))),
)

def ratio_yoy(usable,asof,busy=False):
    ids=('electronics_inventory','electronics_shipments')
    item={'id':'inventory_ratio_yoy','name':'미국 기타 전자부품 재고/월간 출하 비율 전년차','unit':'개월 상당 차이','value':None,'period':None,'previous_period':None,'current_ratio':None,'previous_ratio':None,'components':list(ids),'sources':[source_url(BY_ID[k]) for k in ids],'note':'동일 월 재고액/출하액과 12개월 전 동일 월 비율 차이. 명목 금액이며 메모리 재고일수 아님.'}
    if busy or not all(fresh(k,usable.get(k,[]),asof) for k in ids):return item
    a,b=(dict(usable[k]) for k in ids)
    # Use the latest inventory observation only, not a silent fallback.
    day=max(a);old=month_before(day,12)
    if day not in b or old not in a or old not in b:return item
    vals=[a[day],b[day],a[old],b[old]]
    if not all(number(v) for v in vals) or min(vals)<0 or not vals[1] or not vals[3]:return item
    cur=vals[0]/vals[1];prev=vals[2]/vals[3]
    if number(cur) and number(prev) and number(cur-prev):
        item.update(value=round(cur-prev,8),period=str(day),previous_period=str(old),current_ratio=round(cur,8),previous_ratio=round(prev,8))
    return item

def semiconductor(indicators,usable,asof,busy=False):
    lookup={x['id']:x for x in indicators}
    ratio=ratio_yoy(usable,asof,busy)
    groups=[]
    for gid,name,specs in GROUPS:
        items=[]
        for sid,window in specs:
            if sid==ratio['id']:
                items.append(dict(ratio,available=ratio['value'] is not None));continue
            x=lookup.get(sid,{})
            w=((x.get('history_context') or {}).get('windows') or {}).get(window,{}) if window else {}
            v=w.get('relative_pct') if window else x.get('value')
            valid=not busy and x.get('fresh') and bool(x.get('period')) and number(v) and (not window or bool(w.get('period')))
            items.append({'id':sid,'name':BY_ID[sid].name+(' · '+('전년비' if window=='12m' else '3개월 가격 변화') if window else ''),'unit':'%' if window else BY_ID[sid].unit,'value':v if valid else None,'period':x.get('period') if valid else None,'previous_period':w.get('period') if valid and window else None,'available':bool(valid),'sources':[source_url(BY_ID[sid])]})
        groups.append({'id':gid,'name':name,'available':sum(x['available'] for x in items),'total':len(items),'items':items})
    return {'groups':groups,'ratio':ratio,'note':'공급·수요·가격·주가의 방향을 별도 확인. 전자부품 통계는 반도체 전용이 아니며 집적회로 수출과 메모리 집적회로 수출은 합산 금지. 메모리 현물/계약가격·기업 AI 주문은 미수집. 업황 단정 또는 매매 신호 아님.'}
