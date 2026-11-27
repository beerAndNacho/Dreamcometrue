import csv, io, math, os, time
from datetime import date, datetime
from urllib.parse import parse_qsl
import httpx
from .catalog import SERIES

class NotConfigured(Exception): pass
class DataError(Exception): pass

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
            if r.status_code>=300: raise DataError(f'HTTP {r.status_code}')
            return r
        except httpx.RequestError as e:
            if attempt==2: raise DataError(type(e).__name__) from None
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
    if not key: raise NotConfigured('ECOS_API_KEY 필요')
    if key=='sample': raise NotConfigured('ECOS sample 키는 전체 이력 수집에 사용할 수 없습니다.')
    fmt='%Y%m%d' if s.freq=='D' else '%Y%m'; rows=[]; offset=1
    for _ in range(100):
        path=f'https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/{offset}/{offset+999}/{s.code}/{s.freq}/{start.strftime(fmt)}/{end.strftime(fmt)}/{s.item}'
        j=get(client,path).json(); block=j.get('StatisticSearch')
        if not block: raise DataError('ECOS 응답 오류: 키 및 통계 항목 확인')
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

class KosisError(DataError):
    """Safe diagnostics: never use upstream errMsg, URLs or credentials."""

KOSIS_ERRORS={
    '10':'인증키 누락 · 저장된 키 확인',
    '11':'유효하지 않은 인증키 · KOSIS 활용신청 상태/유효기간 및 발급 원문 확인',
    '20':'필수 요청변수 누락 · 통계표/항목/분류 확인',
    '21':'잘못된 요청변수 · 통계표/항목/기간 확인',
    '30':'조회결과 없음 · 통계표/분류/수록기간 확인',
    '31':'조회결과 초과 · 조회 기간 축소',
    '40':'호출 제한 · 잠시 후 재시도',
    '41':'조회 행 수 제한 · 조회 기간 축소',
    '42':'사용자별 이용 제한 · KOSIS 활용신청 확인',
    '50':'KOSIS 서버 오류 · 잠시 후 재시도',
}

def kosis(client,s,start,end,cfg=None):
    cfg=cfg if cfg is not None else os.environ
    key=cfg.get('KOSIS_API_KEY','').strip()
    if not key: raise NotConfigured('KOSIS API 키 미등록 · 관리자에서 저장 후 검사하세요.')
    from .settings import effective_kosis_query
    try:
        query=effective_kosis_query(s.code,cfg.get(s.code,''))
    except ValueError:
        raise NotConfigured('KOSIS 통계표 설정 오류 · 기본 순환변동치 설정 또는 단일 항목 URL을 사용하세요.') from None
    q=dict(parse_qsl(query,keep_blank_values=True))
    if not all(q.get(k) for k in ('orgId','tblId','objL1','itmId')):
        raise NotConfigured('KOSIS 통계표 URL 생성에서 orgId/tblId/objL1/itmId 설정 필요')
    p=dict(q)
    p.update(method='getList',apiKey=key,format='json',jsonVD='Y',prdSe='M',startPrdDe=start.strftime('%Y%m'),endPrdDe=end.strftime('%Y%m'))
    try:
        r=get(client,'https://kosis.kr/openapi/Param/statisticsParameterData.do',p)
    except DataError as e:
        raise KosisError('KOSIS HTTP/네트워크 오류 ('+str(e)+') · 서버 연결 및 이용 제한 확인') from None
    try:
        j=r.json()
    except ValueError:
        raise KosisError('KOSIS JSON 응답 형식 오류 · 서비스 상태/서버 네트워크 확인') from None
    errors=[j] if isinstance(j,dict) else j if isinstance(j,list) else []
    for item in errors:
        if isinstance(item,dict) and ('err' in item or 'errMsg' in item):
            code=str(item.get('err',''))
            if code in KOSIS_ERRORS: raise KosisError('KOSIS 오류 '+code+' · '+KOSIS_ERRORS[code])
            raise KosisError('KOSIS 오류 응답 · 활용신청 및 조회조건 확인')
    if not isinstance(j,list) or any(not isinstance(r,dict) for r in j):
        raise KosisError('KOSIS 응답 구조 오류 · 통계표 설정 확인')
    if not j: raise KosisError('KOSIS 조회결과 없음 · 통계표/수록기간 확인')
    # Prevent mixing multiple categories and silently swapping leading/coincident indexes.
    identities={tuple(str(r.get(k,'')) for k in ('ITM_ID',*[f'C{i}' for i in range(1,9)])) for r in j}
    if len(identities)>1: raise KosisError('KOSIS 항목/분류를 한 개로 제한하세요.')
    preset_table=q['orgId']=='101' and q['tblId']=='DT_1C8015'
    expected='선행' if s.id=='kr_leading' else '동행'
    for row in j:
        for field, param in [('ORG_ID','orgId'),('TBL_ID','tblId'),('ITM_ID','itmId'),*[(f'C{i}',f'objL{i}') for i in range(1,9)]]:
            if param in q and row.get(field) not in (None,'') and str(row[field])!=q[param]:
                raise KosisError('KOSIS 응답 통계표/항목/분류 코드 불일치')
        if row.get('PRD_SE') not in (None,'','M'):
            raise KosisError('KOSIS 월간 주기 불일치')
        names=''.join(str(row.get(k) or '') for k in ('ITM_NM',*[f'C{i}_NM' for i in range(1,9)]))
        names=''.join(names.split())
        if preset_table or names:
            if expected not in names or '순환변동치' not in names:
                raise KosisError('KOSIS 지표명 불일치 · '+expected+'지수 순환변동치인지 확인')
    try:
        data=points(j,'PRD_DE','DT')
    except DataError:
        raise KosisError('KOSIS 유효 관측값 없음 또는 동일 월의 복수 값 · 통계표 확인') from None
    return data
