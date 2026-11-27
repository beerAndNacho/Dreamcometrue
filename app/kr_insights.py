"""Read-only capital-flow research. Intraday estimates never enter daily caches."""
from datetime import datetime, time, date
from math import isfinite
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo
import re
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .admin import require_admin, require_write
from . import kr_market as km
from .collect import LOCK
from .settings import load_settings
from .db import DEMO

router = APIRouter(tags=['kr-insights'])
SEOUL = ZoneInfo('Asia/Seoul')
INVESTORS = {'foreign': '1', 'institution': '2'}
MARKET_CODES = {'KOSPI': '0001', 'KOSDAQ': '1001'}
RANK_CACHE = {}  # Bounded by eight combinations; never persisted in the daily DB.
LAST_ATTEMPT = -1000.0

class RankRequest(BaseModel):
    market: Literal['KOSPI', 'KOSDAQ'] = 'KOSPI'
    investor: Literal['foreign', 'institution'] = 'foreign'
    direction: Literal['buy', 'sell'] = 'buy'

class CompareRequest(BaseModel):
    period: Literal[1, 5, 20, 60] = 5
    sectors: dict[str, str] = Field(default_factory=dict, max_length=20)


def parse_ranking(raw, investor, direction):
    """Rank returned candidates by signed net trading amount, not share count."""
    if not isinstance(raw, list) or len(raw) > 1000:
        raise km.MarketError('가집계 응답 형식 오류')
    seen = {}
    for row in raw:
        if not isinstance(row, dict):
            raise km.MarketError('가집계 행 형식 오류')
        code = str(row.get('mksc_shrn_iscd', ''))
        if not re.fullmatch(r'[0-9]{6}', code):
            continue
        name = str(row.get('hts_kor_isnm') or code).strip()[:100]
        foreign = km.num(row.get('frgn_ntby_tr_pbmn'))
        institution = km.num(row.get('orgn_ntby_tr_pbmn'))
        entry = {'code': code, 'name': name,
                 'foreign': None if foreign is None else foreign / 100,
                 'institution': None if institution is None else institution / 100,
                 'price': km.num(row.get('stck_prpr')),
                 'change_pct': km.num(row.get('prdy_ctrt'))}
        if code in seen and seen[code] != entry:
            raise km.MarketError('가집계 종목 중복 값 불일치')
        seen[code] = entry
    if raw and not any(x[investor] is not None for x in seen.values()):
        raise km.MarketError('유효한 순매수 거래대금 없음')
    sign = 1 if direction == 'buy' else -1
    items = [r for r in seen.values() if r[investor] is not None and r[investor] * sign > 0]
    items.sort(key=lambda r: (-sign * r[investor], r['code']))
    return items[:30]


def fetch_ranking(client, cfg, req):
    result = km.kis_get(client, cfg, 'foreign-institution-total', 'FHPTJ04400000', {
        'FID_COND_MRKT_DIV_CODE': 'V', 'FID_COND_SCR_DIV_CODE': '16449',
        'FID_INPUT_ISCD': MARKET_CODES[req.market], 'FID_DIV_CLS_CODE': '1',
        'FID_RANK_SORT_CLS_CODE': '0' if req.direction == 'buy' else '1',
        'FID_ETC_CLS_CODE': INVESTORS[req.investor]})
    return parse_ranking(result.get('output'), req.investor, req.direction)


def rank_response(req, items, stamp, demo=False):
    return {**req.model_dump(), 'items': items, 'demo': demo, 'kind': 'intraday_estimate',
            'retrieved_at': stamp.isoformat(timespec='seconds'), 'observation_date': None,
            'unit': '억원', 'source': km.SOURCES['KIS'],
            'note': '장중 가집계 자료, 확정 일별 수급 아님. 조회 시각만 표시하며 실제 집계일/시각은 응답에서 확인되지 않음. 휴장 여부 및 지연 가능성 확인 필요. 공급처 반환 종목 중 최대 30개.'}


def demo_ranking(req):
    sign = 1 if req.direction == 'buy' else -1
    rows = [{'code': f'{900000+i:06}', 'name': f'가상 종목 {i+1}',
             'foreign': sign * (90-i*7), 'institution': sign * (65-i*4),
             'price': 20000+i*1000, 'change_pct': (i-3)/2} for i in range(8)]
    return rank_response(req, rows, datetime.now(SEOUL), True)


@router.post('/api/kr-insights/ranking', dependencies=[Depends(require_write)])
def ranking(req: RankRequest):
    global LAST_ATTEMPT
    if DEMO:
        return demo_ranking(req)
    stamp = datetime.now(SEOUL)
    if stamp.weekday() >= 5 or not time(10, 0) <= stamp.time() < time(15, 31):
        raise HTTPException(409, '장중 가집계 조회는 평일 10:00~15:30 KST에만 가능합니다. 휴장일은 별도 확인하세요.')
    if not LOCK.acquire(blocking=False):
        raise HTTPException(409, '다른 데이터 작업 중입니다.')
    try:
        cfg = load_settings()
        if not cfg['KIS_APP_KEY'] or not cfg['KIS_APP_SECRET']:
            raise HTTPException(400, '관리자에서 KIS 키를 먼저 등록하세요.')
        # Do not serve a cached response after changing credentials.
        identity = sha256((cfg['KIS_APP_KEY']+'\0'+cfg['KIS_APP_SECRET']).encode()).hexdigest()
        key = (req.market, req.investor, req.direction)
        clock = monotonic()
        old = RANK_CACHE.get(key)
        if old and old[0] == identity and 0 <= clock-old[1] < 60 and old[2]['retrieved_at'][:10] == str(stamp.date()):
            return {**old[2], 'cached': True}
        if clock-LAST_ATTEMPT < 3:
            raise HTTPException(429, '호출 간격 제한: 3초 후 다시 시도하세요.')
        LAST_ATTEMPT = clock
        try:
            with httpx.Client(timeout=httpx.Timeout(12, connect=5), follow_redirects=False) as client:
                rows = fetch_ranking(client, cfg, req)
        except km.MarketError as exc:
            raise HTTPException(502, str(exc)) from None
        except Exception:
            raise HTTPException(502, '가집계 API 연결/응답 오류') from None
        response = rank_response(req, rows, datetime.now(SEOUL))
        RANK_CACHE[key] = (identity, monotonic(), response)
        return {**response, 'cached': False}
    finally:
        LOCK.release()


def compare_watchlist(items, markets, period, sectors):
    """Compare only the same latest observed dates, never fill unknown values with zero."""
    out = {'period': period, 'registered': len(items), 'included': 0, 'rows': [],
           'sectors': [], 'excluded': [], 'dates': [], 'reason': None,
           'watchlist': [{'code': x['code'], 'name': x['name']} for x in items]}
    if any(m.get('stale', True) for m in markets):
        out['reason'] = '시장 일별 수급이 누락되었거나 오래되었습니다. 시장 수급을 먼저 수집하세요.'
        return out
    calendars = [[r['date'] for r in m['rows'][-(period+1):]] for m in markets]
    if len(calendars) != 2 or any(len(c) < period for c in calendars) or calendars[0][-period:] != calendars[1][-period:]:
        out['reason'] = '코스피·코스닥의 동일 관측일을 충분히 확보하지 못했습니다.'
        return out
    dates = calendars[0][-period:]
    if len(set(dates)) != period or any(not 0 < (date.fromisoformat(b)-date.fromisoformat(a)).days <= 10 for a,b in zip(dates, dates[1:])):
        out['reason'] = '시장 관측일 불연속: 비교 보류'
        return out
    out['dates'] = dates
    base = calendars[0][0] if len(calendars[0]) == period+1 and calendars[0] == calendars[1] else None
    groups = {}
    for item in items:
        code = item['code']; sector = sectors.get(code, '').strip() or '미분류'
        group = groups.setdefault(sector, {'name': sector, 'registered': 0, 'included': 0, **{k: None for k in km.CORE}})
        group['registered'] += 1
        flow = item['flow']; data = {r['date']: r for r in flow['rows']}
        if flow.get('stale', True) or any(d not in data or any(not isinstance(data[d].get(k), (int, float)) or not isfinite(data[d][k]) for k in km.CORE) for d in dates):
            out['excluded'].append({'code': code, 'name': item['name'], 'reason': '수급 오류/누락 또는 관측일 불일치'})
            continue
        vals = {k: round(sum(data[d][k] for d in dates), 2) for k in km.CORE}
        prices = {r['date']: r.get('close') for r in item['price']['rows']}
        p0, p1 = prices.get(base), prices.get(dates[-1])
        change = round((p1/p0-1)*100, 2) if not item['price'].get('stale', True) and isinstance(p0, (int, float)) and isinstance(p1, (int, float)) and isfinite(p0) and isfinite(p1) and p0>0 and p1>0 else None
        out['rows'].append({'code':code, 'name':item['name'], 'sector':sector, **vals,
                            'both_buy':vals['foreign']>0 and vals['institution']>0,
                            'price_return_pct':change})
        group['included'] += 1
        for k in km.CORE:
            group[k] = round((group[k] or 0) + vals[k], 2)
    out['included'] = len(out['rows'])
    out['sectors'] = sorted(groups.values(), key=lambda g:g['name'])
    out['rows'].sort(key=lambda r:(-r['foreign'],r['code']))
    return out


@router.post('/api/kr-insights/watch', dependencies=[Depends(require_write)])
def compare(req: CompareRequest):
    if any(not re.fullmatch(r'[0-9]{6}', k) or len(v)>40 for k,v in req.sectors.items()):
        raise HTTPException(422, '업종 분류 입력값 확인')
    if not LOCK.acquire(blocking=False):
        raise HTTPException(409, '수집 완료 후 비교하세요.')
    try:
        items = []
        for r in km.watches():
            price, flow = km.watch_data(r['code'])
            items.append({**r, 'price':price, 'flow':flow})
        result = compare_watchlist(items, [km.market_state(m) for m in km.MARKETS], req.period, req.sectors)
        return {**result, 'demo':DEMO, 'unit':'억원', 'source':km.SOURCES['KIS'],
                'note':'관심종목 최대 20개 표본의 합계이며 시장 전체 업종 수급/공식 순위가 아닙니다. 실제 저장 관측일 비교, 거래일 완전성 별도 검증 필요. 미확인 종목 제외.'}
    finally:
        LOCK.release()


@router.get('/kr-insights', include_in_schema=False, dependencies=[Depends(require_admin)])
def insights_page():
    return FileResponse(Path(__file__).parent/'static'/'kr-insights.html', headers={'Cache-Control':'no-store'})
