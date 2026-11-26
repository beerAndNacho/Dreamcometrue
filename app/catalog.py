"""Indicator definitions: provider identifiers, display units and frequency."""
from dataclasses import dataclass, asdict

@dataclass(frozen=True)
class Series:
    id: str
    name: str
    provider: str
    code: str
    freq: str
    unit: str
    group: str
    transform: str = 'level'
    item: str = ''

F = 'FRED'
SERIES = [
    Series('cpi','미국 소비자물가',F,'CPIAUCSL','M','% YoY','물가','yoy'),
    Series('core_cpi','미국 근원 CPI',F,'CPILFESL','M','% YoY','물가','yoy'),
    Series('pce','미국 PCE 물가',F,'PCEPI','M','% YoY','물가','yoy'),
    Series('core_pce','미국 근원 PCE',F,'PCEPILFE','M','% YoY','물가','yoy'),
    Series('unrate','미국 실업률',F,'UNRATE','M','%','고용'),
    Series('claims','미국 신규 실업수당',F,'ICSA','W','건','고용'),
    Series('payroll','미국 비농업 고용 증감',F,'PAYEMS','M','천 명 MoM','고용','diff'),
    Series('jolts','미국 구인 건수',F,'JTSJOL','M','천 건','고용'),
    Series('fed','미국 기준금리 상단',F,'DFEDTARU','D','%','금리'),
    Series('us10y','미국 국채 10년',F,'DGS10','D','%','금리'),
    Series('us2y','미국 국채 2년',F,'DGS2','D','%','금리'),
    Series('us3m','미국 국채 3개월',F,'DGS3MO','D','%','금리'),
    Series('curve','미국 10년-2년 금리차',F,'T10Y2Y','D','%p','금리'),
    Series('curve3m','미국 10년-3개월 금리차',F,'T10Y3M','D','%p','금리'),
    Series('vix','VIX 공포지수',F,'VIXCLS','D','pt','위험'),
    Series('hy','미국 하이일드 OAS',F,'BAMLH0A0HYM2','D','%p','위험'),
    Series('nfci','시카고 연은 금융여건',F,'NFCI','W','지수','위험'),
    Series('m2','미국 M2 증가율',F,'M2SL','M','% YoY','유동성','yoy'),
    Series('fed_assets','연준 총자산',F,'WALCL','W','백만 달러','유동성'),
    Series('rrp','연준 역레포',F,'RRPONTSYD','D','십억 달러','유동성'),
    Series('ip','미국 산업생산 증가율',F,'INDPRO','M','% YoY','경기','yoy'),
    Series('retail','미국 소매판매 증가율',F,'RSAFS','M','% YoY','경기','yoy'),
    Series('gdp','미국 실질 GDP 성장률',F,'A191RL1Q225SBEA','Q','% 연율','경기'),
    Series('sp500','S&P 500 가격지수',F,'SP500','D','pt','주가지수'),
    Series('nasdaq','나스닥 종합 가격지수',F,'NASDAQCOM','D','pt','주가지수'),
    Series('dollar','미국 광의 달러지수',F,'DTWEXBGS','D','pt','환율'),
    Series('oil','WTI 유가',F,'DCOILWTICO','D','USD/배럴','원자재'),
    Series('cli_us','미국 OECD CLI','OECD','USA','M','pt','경기선행'),
    Series('cli_kr','한국 OECD CLI','OECD','KOR','M','pt','경기선행'),
    Series('cli_uk','영국 OECD CLI','OECD','GBR','M','pt','경기선행'),
    Series('kr_rate','한국은행 기준금리','ECOS','722Y001','D','%','금리',item='0101000'),
    Series('kr10y','한국 국고채 10년','ECOS','817Y002','D','%','금리',item='010210000'),
    Series('kr3y','한국 국고채 3년','ECOS','817Y002','D','%','금리',item='010200000'),
    Series('usdkrw','원/달러 매매기준율','ECOS','731Y001','D','KRW/USD','환율',item='0000001'),
    Series('kospi','코스피 가격지수','ECOS','802Y001','D','pt','주가지수',item='0001000'),
    Series('kosdaq','코스닥 가격지수','ECOS','802Y001','D','pt','주가지수',item='0089000'),
    Series('kr_leading','한국 선행지수 순환변동치','KOSIS','KOSIS_LEADING_QUERY','M','pt','경기선행'),
    Series('kr_coincident','한국 동행지수 순환변동치','KOSIS','KOSIS_COINCIDENT_QUERY','M','pt','경기선행'),
]
BY_ID = {s.id:s for s in SERIES}

def source_url(s):
    if s.provider == F: return 'https://fred.stlouisfed.org/series/'+s.code
    return {'OECD':'https://data-explorer.oecd.org/','ECOS':'https://ecos.bok.or.kr/','KOSIS':'https://kosis.kr/'}[s.provider]

def metadata(s):
    return {**asdict(s),'url':source_url(s)}
