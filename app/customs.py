"""Optional nationwide item trade API; exact HS code, completed months only.

Official catalog: https://www.data.go.kr/data/15101609/openapi.do
No aggregation of descendants, no silent totals, no credential in messages.
"""
from datetime import date
from urllib.parse import unquote
import os,re,math
from xml.etree import ElementTree as ET
from .analysis import month_before
from .providers import DataError,NotConfigured

URL='https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList'
LIMIT=1_000_000
CODES={'8542','854232'}

def normalized_key(raw):
    raw=unquote(raw.strip()) # do not turn literal + into spaces
    if raw and (len(raw)>512 or not re.fullmatch(r'[A-Za-z0-9_+=./-]+',raw)):
        raise ValueError('관세청 인증키 형식 오류')
    return raw

def parse_xml(body,code,start,end):
    if code not in CODES or len(body)>LIMIT or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise DataError('관세청 응답 형식 또는 크기 오류')
    try: root=ET.fromstring(body)
    except ET.ParseError: raise DataError('관세청 XML 응답 오류') from None
    status=root.findtext('./header/resultCode')
    if status!='00':
        error=root.findtext('.//returnReasonCode') or status or ''
        raise DataError('관세청 인증 또는 조회 오류'+(' (코드 '+error+')' if re.fullmatch(r'[0-9]{2}',error) else ''))
    rows=root.findall('./body/items/item')
    count=root.findtext('./body/totalCount')
    if count is not None and (not count.isascii() or not count.isdigit() or int(count)>len(rows)):
        raise DataError('관세청 응답 일부 누락 · 페이지 확인')
    out={}
    for row in rows:
        period=(row.findtext('year') or '').strip()
        if period=='총계':
            continue
        if not re.fullmatch(r'\d{4}\.\d{2}',period):
            raise DataError('관세청 월간 기간 형식 오류')
        if (row.findtext('hsCode') or '').strip()!=code:
            raise DataError('관세청 HS 코드 불일치 · 하위 품목을 합산하지 않습니다')
        try:
            d=date.fromisoformat(period.replace('.','-')+'-01')
            raw=(row.findtext('expDlr') or '').strip().replace(',','')
            if not re.fullmatch(r'\d{1,17}',raw):raise ValueError
            value=int(raw)/1_000_000
        except ValueError: raise DataError('관세청 수출금액 또는 관측월 오류') from None
        if not start<=d<=end or not math.isfinite(value):
            raise DataError('관세청 조회 범위 불일치')
        if d in out and out[d]!=value:
            raise DataError('관세청 월별 중복 충돌')
        out[d]=value
    if not out:
        raise DataError('관세청 유효 월별 수출자료 없음')
    expected=set()
    cursor=start
    while cursor<=end:
        expected.add(cursor);cursor=month_before(cursor,-1)
    if set(out)!=expected:
        raise DataError('관세청 조회 기간 월별 이력 누락')
    return out

def fetch(client,s,start,end,cfg=None):
    try: key=normalized_key((cfg if cfg is not None else os.environ).get('CUSTOMS_API_KEY',''))
    except ValueError: raise NotConfigured('관세청 인증키 형식 확인') from None
    if not key: raise NotConfigured('관세청 API 키 미등록 (선택)')
    if s.code not in CODES: raise DataError('지원하지 않는 HS 코드')
    # Previous completed month only, at most 10 years to bound first/full fetch.
    last=month_before(end,1)
    first=start.replace(day=1) if start.day==1 else month_before(start,-1)
    cursor=max(first,month_before(last,119))
    if cursor>last:raise DataError('완료된 월간 조회기간 없음')
    out={}
    while cursor<=last:
        stop=min(last,month_before(cursor,-5))
        params={'serviceKey':key,'strtYymm':cursor.strftime('%Y%m'),
                'endYymm':stop.strftime('%Y%m'),'hsSgn':s.code}
        # Fixed HTTPS endpoint, no redirects; bound decoded response bytes.
        with client.stream('GET',URL,params=params) as response:
            if response.status_code!=200:raise DataError(f'관세청 HTTP {response.status_code}')
            buf=bytearray()
            for chunk in response.iter_bytes():
                buf.extend(chunk)
                if len(buf)>LIMIT:raise DataError('관세청 응답 크기 초과')
        batch=parse_xml(bytes(buf),s.code,cursor,stop)
        if set(batch)&set(out):raise DataError('관세청 배치 중복')
        out.update(batch)
        cursor=month_before(stop,-1)
    if any(month_before(b,1)!=a for a,b in zip(sorted(out),sorted(out)[1:])):
        raise DataError('관세청 전체 월간 이력 누락')
    return out
