"""Optional isolated Chromium test: python -m tools.check_value_ui OUTPUT_DIR.
Uses FastAPI TestClient and synthetic data; never calls external APIs.
"""
import os,sys,tempfile,re,base64
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
out=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve();out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+temp+'/demo.db',SETTINGS_DIR=temp+'/settings',ADMIN_PASSWORD='ui-check-password-123456',AUTO_COLLECT='0')
    from app.main import app
    from app import kr_value as v
    from app.db import Session,MarketCache
    auth=('admin',os.environ['ADMIN_PASSWORD']);head={'X-Macro-Admin':'1'}
    with TestClient(app) as client,sync_playwright() as pw:
        assert client.put('/api/kr-market/watch',auth=auth,headers=head,json={'code':'000270','name':'기아 테스트'}).status_code==200
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
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',bridge)
            page.set_content(html('/kr-value').replace('</head>',boot+'</head>'),wait_until='load')
            page.wait_for_function("document.querySelector('#annualChart svg') && !document.querySelector('#valueStock').disabled",timeout=12000)
            assert page.locator('#multiplesSave').is_disabled();assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            page.locator('#valueYears').select_option('10');page.wait_for_function("document.querySelectorAll('#annualRows tbody tr').length===10 && !document.querySelector('#valueStock').disabled",timeout=12000)
            page.screenshot(path=str(out/f'kr-value-{label}.png'))
            page.locator('#dividendPanel').screenshot(path=str(out/f'kr-dividend-{label}.png'))
            page.locator('#applyActual').click();page.locator('#sBps').fill('80000');page.locator('#sIc').fill('200000');page.locator('#scenarioRun').click();page.wait_for_function("document.querySelector('#reinvestChart svg')",timeout=12000)
            page.locator('#scenarioPanel').screenshot(path=str(out/f'kr-value-scenario-{label}.png'))
            # Isolated temporary DB, simulated live UI for manual user-import, no provider calls.
            v.DEMO=False
            page.locator('#valueLoad').click();page.wait_for_function("document.querySelector('#valueStatus').textContent.includes('OpenDART') && !document.querySelector('#valueStock').disabled",timeout=12000)
            page.locator('#multiplesPanel summary').click();page.locator('#multiplesCsv').fill('date,price,eps,bps\n2025-11-25,100000,10000,80000\n2025-11-26,120000,10000,80000\n');page.locator('#multiplesSource').fill('UI test synthetic');page.locator('#multiplesConfirm').check();page.locator('#multiplesSave').click();page.wait_for_function("document.querySelector('#multiplesMessage').textContent.includes('완료')",timeout=12000)
            assert page.locator('#multiplesRows tbody tr').count()==2 and page.locator('#multiplesChart svg').count()==1
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');assert not errors,errors
            page.locator('#multiplesPanel').screenshot(path=str(out/f'kr-multiples-{label}.png'))
            with Session.begin() as db: db.delete(db.get(MarketCache,v.key('multiples','000270','COMMON')))
            v.DEMO=True;page.close();print(label+': final value page, 10 years, scenario, CSV import, demo safeguards OK')
        browser.close()
