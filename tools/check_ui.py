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
            assert '38' in page.locator('#qualitySummary').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'overflow')
            page.screenshot(path=str(out/f'dashboard-{label}.png'))
            page.locator('#search').fill('코스피');assert page.locator('#indicators tr').count()==1
            page.locator('#indicators [data-series]').click();page.wait_for_function("document.querySelector('#detailChart svg') && document.querySelector('#detailTitle').textContent.includes('코스피')",timeout=12000)
            page.locator('#symbol').fill('기아 테스트');page.locator('#eps').fill('10000');page.locator('#price').fill('100000');page.locator('#saveHolding').click();assert '기아 테스트' in page.locator('#holdings').inner_text()
            page.locator('#savePortfolioServer').click();page.wait_for_function("document.querySelector('#portfolioSync').textContent.includes('서버 저장 완료')",timeout=12000)
            page.locator('#holdings [data-remove]').click();page.locator('#loadPortfolioServer').click();page.wait_for_function("document.querySelector('#holdings').textContent.includes('기아 테스트')",timeout=12000)
            page.locator('#pStock').fill('5000');page.locator('#pMode').select_option('manual');assert '차이' in page.locator('#pRebalance').inner_text()
            assert not errors,errors
            page.close()
            page,errors=page_for('/admin');page.wait_for_function("document.querySelector('#adminStats').textContent.includes('38')",timeout=12000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'admin overflow')
            page.screenshot(path=str(out/f'admin-{label}.png'))
            page.locator('#FRED_API_KEY').fill('ui-mock-key');page.locator('#saveSettings').click();page.wait_for_function("document.querySelector('#flag-FRED').textContent.includes('등록됨') && document.querySelector('#FRED_API_KEY').value===''",timeout=12000)
            assert not errors,errors
            print(label+': layout, charts, holding save/restore, admin settings OK')
            context.close()
        browser.close()
