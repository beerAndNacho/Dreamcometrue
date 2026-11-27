"""Desktop/mobile Chromium test with actual FastAPI response bridge."""
import os,sys,tempfile,re,base64
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
out=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve();out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+tmp+'/demo.db',SETTINGS_DIR=tmp+'/settings',ADMIN_PASSWORD='ui-check-password-123456',AUTO_COLLECT='0',ALERT_ENABLED='0')
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
            with Session.begin() as db:db.execute(delete(JournalStore))
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);page.set_default_timeout(8000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',bridge)
            page.set_content(html('/journal').replace('</head>',boot+'</head>'),wait_until='load')
            page.wait_for_function("document.querySelector('#journalStatus').textContent.includes('불러왔습니다')")
            assert page.locator('#runVintage').is_disabled()
            def fill(id,v):page.locator('#'+id).fill(v)
            def choose(id,v):page.locator('#'+id).select_option(v)
            def click(id):page.locator('#'+id).click()
            def saved():page.wait_for_function("document.querySelector('#journalStatus').textContent.includes('저장했습니다') && !document.querySelector('#saveEntry').disabled")
            fill('entryDay','2024-01-02');fill('entryAmount','1000000');click('saveEntry');saved()
            choose('entryKind','buy');fill('entryDay','2024-01-02');fill('entrySymbol','QQQ');choose('entryCurrency','USD');assert page.locator('#entryFx').input_value()=='';fill('entryFx','1000');fill('entryQty','2');fill('entryPrice','100');fill('entryFee','2');fill('entryMemo','<script>not markup</script>');click('saveEntry');saved()
            assert '<script>' in page.locator('#entryRows').inner_text() and page.locator('#entryRows script').count()==0
            page.locator('[data-edit-entry]').first.click();fill('entryMemo','장기 지수 ETF 분할 매수 (테스트)');click('saveEntry');saved()
            page.wait_for_function("document.querySelector('#valuationNote').textContent.includes('가격 누락')")
            fill('markDay','2024-01-02');fill('markSymbol','QQQ');choose('markCurrency','USD');fill('markFx','1000');fill('markPrice','100');click('saveMark');saved()
            fill('markDay','2024-01-03');fill('markFx','1100');fill('markPrice','120');click('saveMark');saved()
            fill('asof','2024-01-03');click('refreshJournal');page.wait_for_function("document.querySelector('#journalMetrics').textContent.includes('1,062,000원')")
            assert page.locator('#returnChart svg').count()==1
            page.locator('[data-premark]').first.click();assert page.locator('#markSymbol').input_value()=='QQQ' and page.locator('#markPrice').input_value()=='120'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,page.evaluate('document.documentElement.scrollWidth'))
            page.screenshot(path=str(out/f'journal-{label}.png'),full_page=True)
            page.locator('[data-edit-entry]').first.click();assert page.locator('#entrySymbol').input_value()=='QQQ';fill('entryPrice','110');click('saveEntry');saved();page.wait_for_function("document.querySelector('#journalMetrics').textContent.includes('1,042,000원')")
            page.locator('[data-remove-mark]').first.click();saved();page.wait_for_function("document.querySelector('#valuationNote').textContent.includes('가격 누락')")
            page.locator('[data-remove-entry]').first.click();saved();assert '등록된 자료' in page.locator('#journalPositions').inner_text()
            assert not errors,errors
            data=client.get('/api/journal',auth=auth).json();assert len(data['book']['entries'])==1 and data['current']['cash']=='1000000.00'
            assert client.get('/api/journal').status_code==401
            page.close();print(label,'passed',flush=True)
        browser.close()
