import csv, io, math, os, time
from datetime import date, datetime
from urllib.parse import parse_qsl
import httpx
from .catalog import SERIES

class NotConfigured(Exception): pass
class DataError(Exception): pass

class ProviderHTTPError(DataError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'HTTP {status_code}')


class ProviderConnectionError(DataError): pass
class KosisResponseError(DataError): pass

class EcosKeyError(NotConfigured): pass
class EcosResponseError(DataError): pass

def number(raw):
    try:
        v=float(str(raw).replace(',',''))
        return v if math.isfinite(v) else None
    except (ValueError,TypeError): return None

def period(raw):
    text=str(raw).strip()
    if len(text)==7 and text[4]=='-': text+='-01'
    if len(text)==6 and text.isdigit(): text=text[:4]+'-'+text[4:]+'-01'
    if len(text)==8 and text.isdigit(): text=text[:4]+'-'+text[4:6]+'-'+text[6:]
    return date.fromisoformat(text)

def points(rows,date_key,value_key):
    result={}
    for r in rows:
        v=number(r.get(value_key)); raw=r.get(date_key)
        if v is None or not raw: continue
        try: d=period(raw)
        except ValueError: continue
        if d in result and result[d]!=v: raise DataError('동일 시점 복수 값: 통계표의 분류/항목을 하나로 선택하세요.')
        result[d]=v
    if not result: raise DataError('유효한 데이터가 없습니다.')
    return result

def get(client,url,params=None):
    for attempt in range(3):
        try:
            r=client.get(url,params=params)
            if r.status_code in (429,500,502,503,504) and attempt<2:
                time.sleep(2**attempt); continue
            if r.status_code>=300: raise ProviderHTTPError(r.status_code)
            return r
        except httpx.RequestError as e:
            if attempt==2: raise ProviderConnectionError(type(e).__name__) from None
            time.sleep(2**attempt)
    raise DataError('재시도 실패')

def fred(client,s,start,end,cfg=None):
    key=(cfg if cfg is not None else os.environ).get('FRED_API_KEY','').strip()
    if not key: raise NotConfigured('FRED_API_KEY 필요')
    rows=[]; offset=0
    for _ in range(100):
        p={'api_key':key,'series_id':s.code,'file_type':'json','observation_start':str(start),'observation_end':str(end),'limit':100000,'offset':offset}
        j=get(client,'https://api.stlouisfed.org/fred/series/observations',p).json()
        if 'observations' not in j: raise DataError('FRED 응답 구조 오류')
        batch=j['observations']; rows.extend(batch); offset+=len(batch)
        if not batch or offset>=int(j.get('count',offset)): break
    else: raise DataError('FRED 페이지 제한 초과')
    return points(rows,'date','value')

def ecos(client,s,start,end,cfg=None):
    key=(cfg if cfg is not None else os.environ).get('ECOS_API_KEY','').strip()
    if not key:
        raise EcosKeyError('저장된 ECOS API 키가 없습니다. 한국은행 ECOS 카드에 발급 키를 입력하고 저장하세요.')
    if key == 'sample':
        raise EcosKeyError('ECOS 공개 sample 키는 실데이터 수집에 사용할 수 없습니다. 발급받은 키를 저장하세요.')
    fmt='%Y%m%d' if s.freq=='D' else '%Y%m'; rows=[]; offset=1
    for _ in range(100):
        path=f'https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/{offset}/{offset+999}/{s.code}/{s.freq}/{start.strftime(fmt)}/{end.strftime(fmt)}/{s.item}'
        j=get(client,path).json(); block=j.get('StatisticSearch')
        if not block:
            result = j.get('RESULT')
            code = result.get('CODE') if isinstance(result, dict) else None
            if code == 'INFO-100':
                raise EcosKeyError('ECOS가 인증키를 거부했습니다 (INFO-100). 발급 상태와 입력한 키를 확인하세요.')
            if code == 'INFO-200':
                raise EcosResponseError('ECOS에 조회 기간의 데이터가 없습니다 (INFO-200). 조회 기간과 통계 항목을 확인하세요.')
            raise DataError('ECOS 응답 오류: 키 및 통계 항목 확인')
        batch=block.get('row',[])
        for row in batch:
            if row.get('STAT_CODE') not in (None,s.code) or row.get('ITEM_CODE1') not in (None,s.item):
                raise DataError('ECOS 응답 통계코드 불일치')
            # Check human-readable identity when ECOS includes it. Fail closed on a mislabeled rate.
            expected={'kr10y':('국고채','10년'),'kr3y':('국고채','3년'),'kospi':('코스피',),'kosdaq':('코스닥',)}.get(s.id)
            name=str(row.get('ITEM_NAME1') or '').lower().replace(' ','').replace('(','').replace(')','')
            if expected and name and not all(k in name for k in expected):
                if not (s.id in ('kospi','kosdaq') and s.id in name):
                    raise DataError('ECOS 통계 항목명 불일치: 공식 통계표 확인 필요')
        rows.extend(batch); offset+=len(batch)
        if not batch or offset>int(block.get('list_total_count',len(rows))): break
    else: raise DataError('ECOS 페이지 제한 초과')
    return points(rows,'TIME','DATA_VALUE')

def oecd(client,start,end):
    url='https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/USA+KOR+GBR.M.LI...AA...H'
    r=get(client,url,{'startPeriod':start.strftime('%Y-%m'),'endPeriod':end.strftime('%Y-%m'),'dimensionAtObservation':'AllDimensions','format':'csvfile'})
    rows=list(csv.DictReader(io.StringIO(r.text.lstrip('\ufeff'))))
    if not rows or not {'REF_AREA','TIME_PERIOD','OBS_VALUE'}.issubset(rows[0]): raise DataError('OECD CSV 응답 형식 오류')
    result={}
    for s in SERIES:
        if s.provider=='OECD':
            subset=[row for row in rows if row['REF_AREA']==s.code]
            try: result[s.id]=points(subset,'TIME_PERIOD','OBS_VALUE')
            except DataError as e: result[s.id]=e
    return result

KOSIS_ERRORS = {
    '10': '인증키가 전달되지 않았습니다. KOSIS 카드에서 발급 키를 다시 저장하세요.',
    '11': '인증키가 유효하지 않거나 만료되었습니다. KOSIS 활용신청에서 발급 상태를 확인하세요.',
    '20': '필수 조회 조건이 누락되었습니다. 저장된 통계표 설정을 확인하세요.',
    '21': '조회 조건이 잘못되었습니다. 통계표, 항목, 분류 코드를 확인하세요.',
    '30': '조회 기간에 해당하는 데이터가 없습니다. 통계표와 기간을 확인하세요.',
    '31': '조회 가능 건수를 초과했습니다. 수집 조회 시작일을 조정하세요.',
    '40': 'API 호출 한도를 초과했습니다. KOSIS 이용현황을 확인하세요.',
    '41': '조회 행 수 한도를 초과했습니다. KOSIS 이용현황을 확인하세요.',
    '42': '이용 제한이 적용되었습니다. KOSIS 활용신청 상태를 확인하세요.',
    '50': 'KOSIS 서버가 오류를 반환했습니다. 잠시 후 다시 검사하세요.',
}


def kosis(client,s,start,end,cfg=None):
    cfg=cfg if cfg is not None else os.environ
    key=cfg.get('KOSIS_API_KEY','').strip(); query=cfg.get(s.code,'').strip()
    if not key or not query: raise NotConfigured(f'KOSIS_API_KEY 및 {s.code} 필요')
    # Accept query string or generated URL; send only to the fixed official host.
    from .settings import clean_query
    q=dict(parse_qsl(clean_query(query).split('?',1)[-1],keep_blank_values=True))
    if not all(q.get(k) for k in ('orgId','tblId','objL1','itmId')): raise NotConfigured('KOSIS 통계표 URL 생성에서 orgId/tblId/objL1/itmId 설정 필요')
    p={k:v for k,v in q.items() if k in {'orgId','tblId','itmId',*[f'objL{i}' for i in range(1,9)]}}
    p.update(method='getList',apiKey=key,format='json',prdSe='M',startPrdDe=start.strftime('%Y%m'),endPrdDe=end.strftime('%Y%m'))
    try:
        response = get(client, 'https://kosis.kr/openapi/Param/statisticsParameterData.do', p)
    except ProviderHTTPError as error:
        raise KosisResponseError(f'KOSIS 요청이 실패했습니다 (HTTP {error.status_code}).') from None
    except ProviderConnectionError:
        raise KosisResponseError('앱 서버에서 KOSIS에 연결하지 못했습니다. 서버의 인터넷 연결을 확인하세요.') from None

    try:
        j = response.json()
    except ValueError:
        raise KosisResponseError('KOSIS에서 JSON 통계 응답을 받지 못했습니다.') from None
    if isinstance(j, dict):
        code = str(j.get('err') or j.get('errCode') or '')
        if code in KOSIS_ERRORS:
            raise KosisResponseError(f'KOSIS 오류 {code} · {KOSIS_ERRORS[code]}')
        raise KosisResponseError('KOSIS가 인식할 수 없는 오류 응답을 보냈습니다.')
    if not isinstance(j, list) or any(not isinstance(row, dict) for row in j):
        raise KosisResponseError('KOSIS 통계 응답 형식이 올바르지 않습니다.')
    if not j:
        raise KosisResponseError('KOSIS에 조회 기간의 데이터가 없습니다.')
    # Prevent accidentally treating multiple categories (even equal values) as one series.
    identities={tuple(r.get(k,'') for k in ('ITM_ID',*[f'C{i}' for i in range(1,9)])) for r in j}
    if len(identities)>1: raise DataError('KOSIS 항목/분류를 한 개로 제한하세요.')
    if any(str(r.get('ITM_ID','')) not in ('',q['itmId']) for r in j):
        raise DataError('KOSIS 응답 항목 불일치')
    return points(j,'PRD_DE','DT')
