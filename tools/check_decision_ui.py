"""Desktop/mobile smoke test of both new screens using FastAPI TestClient bridge."""
import os,sys,tempfile,re,base64
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
out=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve();out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+tmp+'/demo.db',SETTINGS_DIR=tmp+'/settings',BACKUP_DIR=tmp+'/backups',ADMIN_PASSWORD='ui-check-password-123456',AUTO_COLLECT='0',ALERT_ENABLED='0')
    from app.main import app
    auth=('admin',os.environ['ADMIN_PASSWORD'])
    with TestClient(app) as client,sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,executable_path=os.environ.get('CHROMIUM_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None),args=['--no-sandbox'])
        def html(path):
            txt=client.get(path,auth=auth).text
            def style(m):
                css=client.get(m[1],auth=auth).text
                css=re.sub(r"url\('(/static/[^']+\.svg)'\)",lambda a:"url('data:image/svg+xml;base64,"+base64.b64encode(client.get(a[1],auth=auth).content).decode()+"')",css)
                return '<style>'+css+'</style>'
            txt=re.sub(r'<link rel="stylesheet" href="([^"]+)">',style,txt)
            return re.sub(r'<script src="([^"]+)" defer></script>',lambda m:'<script>'+client.get(m[1],auth=auth).text+'</script>',txt)
        def bridge(path,opts):
            r=client.request(opts.get('method','GET'),path,headers=opts.get('headers',{}),content=opts.get('body'),auth=auth)
            return {'status':r.status_code,'body':r.text}
        boot='''<script>window.fetch=async(path,opts={})=>{const r=await pyFetch(String(path),opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})};</script>'''
        for label,w,h in [('desktop',1440,1100),('mobile',390,844)]:
            if os.getenv('UI_VIEWPORT') not in (None,'',label):continue
            errors=[]
            def new_page(path):
                p=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);p.set_default_timeout(12000);p.on('pageerror',lambda e:errors.append(str(e)));p.expose_function('pyFetch',bridge);p.set_content(html(path).replace('</head>',boot+'</head>'),wait_until='load');return p
            page=new_page('/global-market');page.wait_for_function("document.querySelector('#globalQuality').textContent.includes('600개 관측')")
            assert page.locator('#globalRefresh').is_disabled()
            page.locator('#globalGroup').select_option('미국 섹터 ETF');assert page.locator('#globalTiles button').count()==11
            page.locator('#globalSymbol').select_option('SOXX');page.wait_for_function("document.querySelector('#globalTitle').textContent.includes('SOXX')")
            page.locator('#autoSymbol1').select_option('QQQ');page.locator('#autoSymbol2').select_option('SOXX');page.locator('#autoSymbol3').select_option('VOO');page.locator('#globalAutoSave').click();page.wait_for_function("document.querySelector('#globalAutoState').textContent.includes('저장 완료')")
            assert client.get('/api/admin/settings',auth=auth).json()['values']['GLOBAL_AUTO_SYMBOLS']=='QQQ,SOXX,VOO'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,page.evaluate('document.documentElement.scrollWidth'))
            page.evaluate('window.scrollTo(0,0)');page.screenshot(path=str(out/f'global-{label}.png'),full_page=True);page.close()
            page=new_page('/decision-lab');page.wait_for_function("document.querySelector('#labStatus').textContent.includes('DEMO')")
            page.locator('#strategyRun').click();page.wait_for_function("!document.querySelector('#strategyResult').hidden")
            assert page.locator('#strategyMetrics .lab-result').count()==5
            page.locator('#labPeriod').select_option('120');assert page.locator('#strategyResult').is_hidden()
            page.locator('#strategyRun').click();page.wait_for_function("!document.querySelector('#strategyResult').hidden")
            page.locator('#riskRun').click();page.wait_for_function("!document.querySelector('#riskResult').hidden")
            assert page.locator('#riskTiles .global-tile').count()==7
            assert page.locator('#correlationGrid .global-tile').count()==6
            page.locator('#labPositions [data-weight]').first.fill('100');assert page.locator('#riskResult').is_hidden();page.locator('#riskRun').click();page.wait_for_function("document.querySelector('#riskStatus').textContent.includes('100% 이하')")
            page.locator('#labPositions [data-weight]').first.fill('40');page.locator('#labAdd').click();assert page.locator('.lab-position').count()==4;page.locator('.lab-position').last.locator('button').click();assert page.locator('.lab-position').count()==3
            page.locator('#riskRun').click();page.wait_for_function("!document.querySelector('#riskResult').hidden")
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,page.evaluate('document.documentElement.scrollWidth'))
            assert not errors,errors
            page.evaluate('window.scrollTo(0,0)');page.screenshot(path=str(out/f'decision-{label}.png'),full_page=True);page.close();print(label,'global and decision UI passed',flush=True)
        browser.close()
