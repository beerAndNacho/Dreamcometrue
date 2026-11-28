"""Optional Chromium smoke test: python -m tools.check_ui OUTPUT_DIR.
The sandbox uses set_content and a FastAPI TestClient fetch bridge instead of a live
TCP server. The actual HTML/CSS/JS and real API responses are used; browser storage
and transport are simulated, and CSP headers are tested separately by pytest.
"""
import os,sys,tempfile,base64,re
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
out=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve();out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+temp+'/demo.db',SETTINGS_DIR=temp+'/settings',ADMIN_PASSWORD='ui-check-password-123456',AUTO_COLLECT='0')
    from app.main import app
    auth=('admin',os.environ['ADMIN_PASSWORD'])
    with TestClient(app) as client, sync_playwright() as pw:
        exe=os.environ.get('CHROMIUM_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None)
        browser=pw.chromium.launch(headless=True,executable_path=exe,args=['--no-sandbox'])
        def html(path):
            txt=client.get(path,auth=auth).text
            def style(m):
                css=client.get(m[1],auth=auth).text
                def asset(match):
                    raw=client.get(match[1],auth=auth).content
                    return "url('data:image/svg+xml;base64,"+base64.b64encode(raw).decode()+"')"
                css=re.sub(r"url\('(/static/[^']+\.svg)'\)",asset,css)
                return '<style>'+css+'</style>'
            txt=re.sub(r'<link rel="stylesheet" href="([^"]+)">',style,txt)
            txt=re.sub(r'<script src="([^"]+)" defer></script>',lambda m:'<script>'+client.get(m[1],auth=auth).text+'</script>',txt)
            return txt
        def fetch_bridge(path,opts):
            r=client.request(opts.get('method','GET'),path,headers=opts.get('headers',{}),content=opts.get('body'),auth=auth)
            return {'status':r.status_code,'body':r.text}
        boot='''<script>const __store={};Object.defineProperty(window,'localStorage',{value:{getItem:k=>__store[k]??null,setItem:(k,v)=>__store[k]=String(v),removeItem:k=>delete __store[k]},configurable:true});window.fetch=async(path,opts={})=>{const r=await pyFetch(String(path),opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})};</script>'''
        for label,width,height in [('desktop',1440,1100),('mobile',390,844)]:
            context=browser.new_context(viewport={'width':width,'height':height},device_scale_factor=1)
            def page_for(path):
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',fetch_bridge)
                page.set_content(html(path).replace('</head>',boot+'</head>'),wait_until='load')
                return page,errors
            page,errors=page_for('/');page.wait_for_function("document.querySelectorAll('#marketMetrics .metric').length===8 && document.querySelector('#cliChart svg')",timeout=12000)
            assert '103' in page.locator('#qualitySummary').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'overflow')
            page.wait_for_function("document.querySelectorAll('#guideList .guide-card').length===103 && document.querySelector('#orbitChart svg') && document.querySelector('#simResult .sim-score')",timeout=12000)
            assert page.locator('#briefNotes .brief-note').count()<=3
            page.screenshot(path=str(out/f'dashboard-{label}.png'))
            page.locator('#orbitPanel').screenshot(path=str(out/f'orbit-{label}.png'))
            page.locator('#simCrisis').click();page.wait_for_function("document.querySelector('#simResult .sim-score strong')?.textContent==='100'",timeout=12000)
            page.locator('#scenarioPanel').screenshot(path=str(out/f'scenario-{label}.png'))
            page.locator('#simActual').click();page.wait_for_function("document.querySelector('#simResult .sim-score strong')?.textContent===String(dashboard.model.score)",timeout=12000)
            page.locator('#guideSearch').fill('VIX');assert page.locator('#guideList .guide-card').count()==1
            page.locator('#guideList summary').click();assert '변동성' in page.locator('#guideList dd').first.inner_text()
            page.locator('#learningPanel').screenshot(path=str(out/f'guide-{label}.png'))
            page.locator('#guideSearch').fill('')
            page.locator('#search').fill('미국 M2');page.locator('#indicators [data-favorite]').click()
            assert page.locator('#marketMetrics .metric').count()==9
            page.locator('#search').fill('');page.locator('#favoriteOnly').check();assert page.locator('#indicators tr').count()==1
            page.locator('#indicators [data-favorite]').click();assert '조건에 맞는 지표' in page.locator('#indicators').inner_text()
            page.locator('#resetFilters').click();assert page.locator('#indicators tr').count()==103
            page.locator('#search').fill('코스피');assert page.locator('#indicators tr').count()==1
            page.locator('#indicators [data-series]').click();page.wait_for_function("document.querySelector('#detailChart svg') && document.querySelector('#detailTitle').textContent.includes('코스피')",timeout=12000)
            assert '한국 유가증권시장' in page.locator('#detailGuide').inner_text()
            page.locator('#detailChart input[type=range]').fill('0');assert page.locator('#detailChart .chart-values').inner_text().startswith('20')
            page.locator('#symbol').fill('기아 테스트');page.locator('#eps').fill('10000');page.locator('#price').fill('100000');page.locator('#saveHolding').click();assert '기아 테스트' in page.locator('#holdings').inner_text()
            page.locator('#holdings [data-edit]').click();assert page.locator('#symbol').input_value()=='기아 테스트'
            page.locator('#qty').fill('2');page.locator('#saveHolding').click();assert '200,000' in page.locator('#holdings').inner_text()
            assert '200,000원' in page.locator('#holdingKrw').inner_text()
            page.locator('#savePortfolioServer').click();page.wait_for_function("document.querySelector('#portfolioSync').textContent.includes('서버 저장 완료')",timeout=12000)
            page.locator('#holdings [data-remove]').click();page.locator('#loadPortfolioServer').click();page.wait_for_function("document.querySelector('#holdings').textContent.includes('기아 테스트')",timeout=12000)
            page.locator('#pStock').fill('5000');page.locator('#pMode').select_option('manual');assert '차이' in page.locator('#pRebalance').inner_text()
            assert not errors,errors
            page.close()
            page,errors=page_for('/admin');page.wait_for_function("document.querySelector('#adminStats').textContent.includes('103')",timeout=12000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'admin overflow')
            assert page.locator('#setupSteps .setup-step').count()==5
            page.screenshot(path=str(out/f'admin-{label}.png'))
            page.locator('#FRED_API_KEY').fill('ui-mock-key');page.locator('#saveSettings').click();page.wait_for_function("document.querySelector('#flag-FRED').textContent.includes('등록됨') && document.querySelector('#FRED_API_KEY').value===''",timeout=12000)
            assert not errors,errors
            page.locator('#KIS_APP_KEY').fill('ui-kis-key');page.locator('#KIS_APP_SECRET').fill('ui-kis-secret');page.locator('#DART_API_KEY').fill('ui-dart-key');page.locator('#saveKrKeys').click();page.wait_for_function("document.querySelector('#flag-KIS').textContent==='키 쌍 등록됨' && document.querySelector('#KIS_APP_SECRET').value===''",timeout=12000)
            assert not errors,errors
            page.close()
            page,errors=page_for('/kr-market');page.wait_for_function("document.querySelectorAll('#marketCards .panel').length===2 && document.querySelector('#flowChart svg')",timeout=12000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'KR market overflow')
            assert 'DEMO' in page.locator('#krStatus').inner_text();assert page.locator('#refreshFlow').is_disabled()
            page.screenshot(path=str(out/f'kr-market-{label}.png'))
            page.locator('#flow').screenshot(path=str(out/f'kr-flow-{label}.png'))
            page.locator('#marketSelect').select_option('KOSDAQ');page.locator('#rangeSelect').select_option('5');page.locator('#chartMode').select_option('cum');assert page.locator('#flowRows tr').count()==5
            page.locator('#loadWatch').click();page.wait_for_function("document.querySelector('#watchStatus').textContent.includes('개 등록')",timeout=12000)
            page.locator('#ticker').fill('005930');page.locator('#tickerName').fill('삼성전자 테스트');page.locator('#corpCode').fill('00126380');page.locator('#watchForm button').click();page.wait_for_function("document.querySelector('#watchRows').textContent.includes('삼성전자 테스트')",timeout=12000)
            page.locator('#watchRows [data-action=detail]').click();assert page.locator('#finCode').input_value()=='005930';assert page.locator('#stockChart svg').count()==1
            page.locator('#watch').screenshot(path=str(out/f'kr-watch-{label}.png'))
            page.locator('#watchRows [data-action=edit]').click();assert page.locator('#ticker').input_value()=='005930'
            page.locator('#tickerName').fill('수정 종목');page.locator('#watchForm button').click();page.wait_for_function("document.querySelector('#watchRows').textContent.includes('수정 종목')",timeout=12000)
            page.locator('#watchRows [data-action=delete]').click();page.wait_for_function("document.querySelector('#watchRows').textContent.includes('등록한 종목 없음')",timeout=12000)
            assert not errors,errors
            print(label+': all v4 UI + KR flow filters/chart, watch edit/delete, DART form, admin key save OK')
            page.close()
            for code,name in [('005930','삼성전자 테스트'),('000660','하이닉스 테스트'),('000270','기아 테스트')]:
                assert client.put('/api/kr-market/watch',auth=auth,headers={'X-Macro-Admin':'1'},json={'code':code,'name':name}).status_code==200
            page,errors=page_for('/kr-insights')
            page.wait_for_function("document.querySelector('#compareStatus').textContent.includes('3/3종목')",timeout=12000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'insights overflow')
            page.locator('#rankRun').click();page.wait_for_function("document.querySelector('#rankStatus').textContent.includes('가상 데이터')",timeout=12000)
            assert page.locator('#rankRows tr').count()==8
            page.locator('#rankDirection').select_option('sell');assert '조회 결과 없음' in page.locator('#rankRows').inner_text()
            page.locator('#rankRun').click();page.wait_for_function("document.querySelector('#rankStatus').textContent.includes('순매도')",timeout=12000)
            page.screenshot(path=str(out/f'kr-insights-{label}.png'))
            page.locator('#comparePanel summary').click()
            page.locator('#sectorFields select[data-code="005930"]').select_option('반도체')
            page.wait_for_function("document.querySelector('#sectorRows').textContent.includes('반도체') && !document.querySelector('#compareRun').disabled",timeout=12000)
            page.locator('#sectorFields select[data-code="000660"]').select_option('반도체')
            page.wait_for_function("document.querySelector('#sectorRows').textContent.includes('2/2') && !document.querySelector('#compareRun').disabled",timeout=12000)
            page.locator('#comparePeriod').select_option('20');page.locator('#compareRun').click()
            page.wait_for_function("document.querySelector('#compareStatus').textContent.includes('20개 공통 관측일')",timeout=12000)
            page.locator('#compareSort').select_option('institution');page.locator('#bothBuy').check();page.locator('#bothBuy').uncheck()
            page.locator('#comparePanel').screenshot(path=str(out/f'kr-compare-{label}.png'))
            assert not errors,errors
            page.close()
            page,errors=page_for('/kr-pressure')
            page.wait_for_function("document.querySelector('#programData .metric') && !document.querySelector('#pressureStock').disabled",timeout=12000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'pressure overflow')
            assert '가상' in page.locator('#pressureStatus').inner_text() and page.locator('#pressureRefresh').is_disabled()
            page.locator('#pressureStock').select_option('005930')
            page.wait_for_function("document.querySelector('#loanData .metric') && !document.querySelector('#pressureStock').disabled",timeout=12000)
            assert page.locator('#shortData svg').count()==1
            assert page.locator('#stockData').is_visible()
            page.locator('#pressurePeriod').select_option('5')
            page.wait_for_function("document.querySelector('#programData').textContent.includes('최근 5개 관측일 요약') && !document.querySelector('#pressureMarket').disabled",timeout=12000)
            page.screenshot(path=str(out/f'kr-pressure-{label}.png'))
            page.locator('#stockPanel').screenshot(path=str(out/f'kr-short-loan-{label}.png'))
            page.locator('#pressureMarket').select_option('KOSDAQ')
            page.wait_for_function("document.querySelector('#spotFutures').textContent.includes('코스닥 현물과 직접 비교하지') && !document.querySelector('#pressureStock').disabled",timeout=12000)
            assert not errors,errors
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'pressure stock overflow')
            print(label+': market pressure, stock selection, period, comparison, demo safeguards OK')
            page.close()
            page,errors=page_for('/kr-value')
            page.wait_for_function("document.querySelector('#annualChart svg') && !document.querySelector('#valueStock').disabled",timeout=12000)
            assert '가상 데이터' in page.locator('#valueStatus').inner_text()
            assert page.locator('#valueRefresh').is_disabled() and page.locator('#eventSave').is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'value overflow')
            page.locator('#valueStock').select_option('000270')
            page.wait_for_function("document.querySelector('#valueStatus').textContent.includes('기아 테스트') && !document.querySelector('#valueStock').disabled",timeout=12000)
            page.locator('#valueYears').select_option('10')
            page.wait_for_function("document.querySelectorAll('#annualRows tbody tr').length===10 && !document.querySelector('#valueStock').disabled",timeout=12000)
            page.screenshot(path=str(out/f'kr-value-{label}.png'))
            page.locator('#dividendPanel').screenshot(path=str(out/f'kr-dividend-{label}.png'))
            page.locator('#applyActual').click(); assert float(page.locator('#sEps').input_value())>0
            page.locator('#sBps').fill('80000');page.locator('#sIc').fill('200000');page.locator('#scenarioRun').click()
            page.wait_for_function("document.querySelector('#reinvestChart svg') && document.querySelector('#scenarioMessage').textContent.includes('완료')",timeout=12000)
            assert page.locator('#scenarioCards .metric').count()==7
            page.locator('#scenarioPanel').screenshot(path=str(out/f'kr-value-scenario-{label}.png'))
            page.locator('#sEps').fill('-100');page.locator('#scenarioRun').click();page.wait_for_function("document.querySelector('#reinvestChart svg')",timeout=12000)
            assert '—' in page.locator('#scenarioPrices tbody tr').first.inner_text()
            assert not errors,errors
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'value scenario overflow')
            print(label+': annual financials, dividend history, company/period selection, scenario and demo safeguards OK')
            page.close()
            for code in ['005930','000660','000270']:
                assert client.delete('/api/kr-market/watch/'+code,auth=auth,headers={'X-Macro-Admin':'1'}).status_code==200
            print(label+': intraday ranking, aligned comparison, sector tags, filters and responsive UI OK')
            context.close()
        browser.close()
