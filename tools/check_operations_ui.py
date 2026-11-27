"""PC/mobile operations and journal import against FastAPI TestClient response bridge."""
import os,sys,tempfile,re,base64,json
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
out=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve();out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+tmp+'/demo.db',SETTINGS_DIR=tmp+'/settings',BACKUP_DIR=tmp+'/backups',ADMIN_PASSWORD='ui-check-password-123456',AUTO_COLLECT='0',ALERT_ENABLED='0')
    from app.main import app
    from app.db import Session
    from app.journal import JournalStore
    from sqlalchemy import delete
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
            with Session.begin() as db:db.execute(delete(JournalStore))
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);page.set_default_timeout(12000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',bridge)
            page.set_content(html('/operations').replace('</head>',boot+'</head>'),wait_until='load')
            page.wait_for_function("document.querySelector('#opsMessage').textContent.includes('불러왔습니다')")
            assert page.locator('#retryIndicator').is_disabled()
            page.locator('#BACKUP_KEEP').fill('2');page.locator('#backupSettings button').click();page.wait_for_function("document.querySelector('#opsMessage').textContent.includes('저장했습니다')")
            page.locator('#backupNow').click();page.wait_for_function("document.querySelector('#opsMessage').textContent.includes('백업 완료')")
            page.locator('[data-verify]').first.click();page.wait_for_function("document.querySelector('#opsMessage').textContent.includes('검사 통과')")
            page.locator('#integrityCheck').click();page.wait_for_function("document.querySelector('#integrityNote').textContent.includes('건 검사')")
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,page.evaluate('document.documentElement.scrollWidth'))
            page.evaluate('window.scrollTo(0,0)');page.wait_for_timeout(250)
            page.screenshot(path=str(out/f'operations-{label}.png'),full_page=True)
            page.close()
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);page.set_default_timeout(12000);page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',bridge)
            page.set_content(html('/journal').replace('</head>',boot+'</head>'),wait_until='load')
            page.wait_for_function("document.querySelector('#journalStatus').textContent.includes('불러왔습니다')")
            archive={'format':'macro-invest-journal-v1','demo':True,'revision':10,'book':{'entries':[{'id':'demo-import','day':'2024-01-01','kind':'deposit','amount':'1000000'}],'marks':[],'valuation_days':[]}}
            page.locator('#importJournalFile').set_input_files({'name':'demo.json','mimeType':'application/json','buffer':json.dumps(archive).encode()});page.locator('#previewJournalImport').click();page.wait_for_function("document.querySelector('#importJournalNote').textContent.includes('교체 예정')")
            assert client.get('/api/journal',auth=auth).json()['revision']==0
            page.locator('#applyJournalImport').click();page.wait_for_function("document.querySelector('#importJournalNote').textContent.includes('가져오기 완료')")
            assert client.get('/api/journal',auth=auth).json()['current']['cash']=='1000000.00'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            assert not errors,errors
            page.evaluate('window.scrollTo(0,0)');page.wait_for_timeout(250)
            page.screenshot(path=str(out/f'journal-import-{label}.png'),full_page=True)
            page.close();print(label,'operations and import UI passed',flush=True)
        browser.close()
