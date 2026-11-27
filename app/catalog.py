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
    Series("tga","미국 재무부 일반계정 (TGA)",F,"WDTGAL","W","백만 달러","유동성","level"),
    Series("reserves","미국 은행 지급준비금",F,"WRBWFRBL","W","백만 달러","유동성","level"),
    Series("sofr","미국 SOFR 익일 담보부 조달금리",F,"SOFR","D","%","유동성","level"),
    Series("iorb","미국 준비금 이자율 (IORB)",F,"IORB","D","%","금리","level"),
    Series("real10y","미국 10년 실질 국채금리",F,"DFII10","D","%","금리","level"),
    Series("breakeven10y","미국 10년 손익분기 인플레이션",F,"T10YIE","D","%","물가","level"),
    Series("inflation5y5y","미국 5년 후 5년 기대 인플레이션",F,"T5YIFR","D","%","물가","level"),
    Series("sahm","미국 실시간 Sahm 경기침체 지표",F,"SAHMREALTIME","M","%p","고용","level"),
    Series("claims4w","미국 신규 실업수당 4주 평균",F,"IC4WSA","W","건","고용","level"),
    Series("continued_claims","미국 계속 실업수당 청구",F,"CCSA","W","건","고용","level"),
    Series("housing_starts","미국 주택착공",F,"HOUST","M","천 호 SAAR","경기","level"),
    Series("building_permits","미국 주택건축 허가",F,"PERMIT","M","천 호 SAAR","경기","level"),
    Series("capital_orders","미국 핵심 자본재 신규주문 증가율",F,"NEWORDER","M","% YoY","경기","yoy"),
    Series("capacity","미국 설비가동률",F,"TCU","M","%","경기","level"),
    Series("ig_spread","미국 투자등급 회사채 OAS",F,"BAMLC0A0CM","D","%p","위험","level"),
    Series("business_loans","미국 상업·산업 대출 증가율",F,"BUSLOANS","M","% YoY","유동성","yoy"),
    Series("lending_tightening","미국 대기업·중견기업 대출기준 순긴축",F,"DRTSCILM","Q","%p","위험","level"),
    Series("mortgage30","미국 30년 고정 주택담보대출 금리",F,"MORTGAGE30US","W","%","금리","level"),
    Series("saving_rate","미국 개인저축률",F,"PSAVERT","M","%","경기","level"),
    Series("wage_growth","미국 생산·비관리직 시간당 임금 증가율",F,"AHETPI","M","% YoY","고용","yoy"),
    Series("participation","미국 경제활동참가율",F,"CIVPART","M","%","고용","level"),
    Series("ppi","미국 전체 상품 생산자물가 증가율",F,"PPIACO","M","% YoY","물가","yoy"),
    Series("nasdaq100","나스닥 100 가격지수",F,"NASDAQ100","D","pt","주가지수","level"),
    Series("sox","필라델피아 반도체 SOX 가격지수",F,"NASDAQSOX","D","pt","주가지수","level"),
    Series("semi_ip","미국 반도체·전자부품 산업생산 증가율",F,"IPG3344S","M","% YoY","반도체","yoy"),
    Series("semi_ppi","미국 반도체·전자부품 생산자물가 증가율",F,"PCU33443344","M","% YoY","반도체","yoy"),
    Series("kr_gdp","한국 실질 GDP 성장률 (OECD)",F,"KORGDPRQPSMEI","Q","% YoY","한국 실물경제","level"),
    Series("kr_unrate","한국 실업률 15세 이상 (OECD)",F,"LRHUTTTTKRM156S","M","%","한국 실물경제","level"),
    Series("kr_ip","한국 산업생산 증가율 (OECD)",F,"KORPRINTO01GYSAM","M","% YoY","한국 실물경제","level"),
    Series("kr_mfg","한국 제조업 생산 증가율 (OECD)",F,"KORPRMNTO01GYSAM","M","% YoY","한국 실물경제","level"),
    Series("kr_exports","한국 상품 수출 증가율 (OECD)",F,"XTEXVA01KRM659S","M","% YoY","한국 무역","level"),
    Series("kr_imports","한국 상품 수입 증가율 (OECD)",F,"XTIMVA01KRM659S","M","% YoY","한국 무역","level"),
    Series("kr_confidence","한국 소비자신뢰 설문 순응답 (OECD)",F,"CSCICP02KRM066S","M","%p","한국 실물경제","level"),
    Series("kr_orders","한국 제조업 수주 체감 (OECD)",F,"BSOITE02KRM460S","M","%p","한국 실물경제","level"),
    Series("ecb","유럽중앙은행 예금금리",F,"ECBDFR","D","%","금리","level"),
    Series("usdjpy","달러당 일본 엔",F,"DEXJPUS","D","JPY/USD","환율","level"),
    Series("usdcny","달러당 중국 위안",F,"DEXCHUS","D","CNY/USD","환율","level"),
    Series("eurusd","유로당 미국 달러",F,"DEXUSEU","D","USD/EUR","환율","level"),
    Series("brent","브렌트유 현물가격",F,"DCOILBRENTEU","D","USD/배럴","원자재","level"),
    Series("gas","미국 헨리허브 천연가스 현물가격",F,"DHHNGSP","D","USD/MMBtu","원자재","level"),
    Series("copper","국제 구리 월평균 가격",F,"PCOPPUSDM","M","USD/톤","원자재","level"),
    Series("kr_cpi","한국 소비자물가 증가율 (ECOS)","ECOS","901Y009","M","% YoY","한국 실물경제","yoy",item="0"),
    Series("cli_jp","일본 OECD CLI","OECD","JPN","M","pt","경기선행","level"),
    Series("cli_cn","중국 OECD CLI","OECD","CHN","M","pt","경기선행","level"),
    Series("cli_de","독일 OECD CLI","OECD","DEU","M","pt","경기선행","level"),
    Series("cli_in","인도 OECD CLI","OECD","IND","M","pt","경기선행","level"),
    Series("cli_g7","G7 OECD CLI","OECD","G7","M","pt","경기선행","level"),
    Series("electronics_orders","미국 기타 전자부품 신규주문",F,"A34HNO","M","백만 달러","반도체","level"),
    Series("electronics_inventory","미국 기타 전자부품 재고",F,"A34HTI","M","백만 달러","반도체","level"),
    Series("electronics_shipments","미국 기타 전자부품 출하",F,"A34HVS","M","백만 달러","반도체","level"),
    Series("electronics_backlog","미국 기타 전자부품 미납주문",F,"A34HUO","M","백만 달러","반도체","level"),
    Series("semi_import_price","미국 반도체 수입물가 증가율",F,"IR21320","M","% YoY","반도체","yoy"),
    Series("semi_export_price","미국 반도체 수출물가 증가율",F,"IQ21320","M","% YoY","반도체","yoy"),
    Series("electronics_import_price","미국 반도체·전자부품 수입물가 증가율",F,"IZ3344","M","% YoY","반도체","yoy"),
    Series("corporate_profit","미국 세후 기업이익 (IVA·CCAdj 조정)",F,"CPATAX","Q","십억 달러 SAAR","기업 이익","level"),
    Series("nominal_gdp","미국 명목 GDP",F,"GDP","Q","십억 달러 SAAR","경기","level"),
    Series("nonfinancial_equity","미국 비금융기업 주식 부채 평가액",F,"NCBEILQ027S","Q","백만 달러","밸류에이션 참고","level"),
    Series("creditcard_delinquency","미국 신용카드 대출 연체율",F,"DRCCLACBS","Q","%","위험","level"),
    Series("loan_delinquency","미국 전체 은행대출 연체율",F,"DRALACBS","Q","%","위험","level"),
    Series("financial_stress","세인트루이스 연은 금융스트레스지수",F,"STLFSI4","W","지수","위험","level"),
    Series("adjusted_nfci","시카고 연은 조정 금융여건",F,"ANFCI","W","지수","위험","level"),
    Series("cfnai","시카고 연은 국가활동지수",F,"CFNAI","M","지수","경기","level"),
    Series("real_income","미국 실질 가처분소득 증가율",F,"DSPIC96","M","% YoY","경기","yoy"),
    Series("kr_ic_exports","한국 집적회로 수출 (HS 8542)","CUSTOMS","8542","M","백만 달러","반도체","level"),
    Series("kr_memory_exports","한국 메모리 집적회로 수출 (HS 854232)","CUSTOMS","854232","M","백만 달러","반도체","level"),
]
BY_ID = {s.id:s for s in SERIES}

def source_url(s):
    if s.provider == F: return 'https://fred.stlouisfed.org/series/'+s.code
    return {'OECD':'https://data-explorer.oecd.org/','ECOS':'https://ecos.bok.or.kr/','KOSIS':'https://kosis.kr/','CUSTOMS':'https://www.data.go.kr/data/15101609/openapi.do'}[s.provider]

def metadata(s):
    return {**asdict(s),'url':source_url(s)}
