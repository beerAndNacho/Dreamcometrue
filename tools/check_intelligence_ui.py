"""Isolated real Chromium interaction check using FastAPI TestClient response bridge."""
import os,sys,tempfile,re,base64
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
out=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve();out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+temp+'/demo.db',SETTINGS_DIR=temp+'/settings',ADMIN_PASSWORD='ui-check-password-123456',AUTO_COLLECT='0')
    from app.main import app
    from app import release_calendar as cal
    from app.db import Session
    from sqlalchemy import delete
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
            if os.getenv('UI_VIEWPORT') and os.getenv('UI_VIEWPORT')!=label:continue
            print(label+': start',flush=True)
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);page.set_default_timeout(6000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',bridge)
            page.set_content(html('/intelligence').replace('</head>',boot+'</head>'),wait_until='load')
            page.wait_for_function("document.querySelectorAll('#evidenceRows tr').length>30 && document.querySelectorAll('#calendarRows .calendar-event').length===4 && !document.querySelector('#intelSave').disabled",timeout=12000)
            assert page.locator('#intelGenerate').is_disabled();assert page.locator('#blsRefresh').is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'overflow')
            page.screenshot(path=str(out/f'intelligence-{label}.png'))
            page.locator('#calendarPanel').screenshot(path=str(out/f'calendar-{label}.png'))
            page.locator('#intelStock').select_option('000270');page.wait_for_function("document.querySelector('#fact-stock_close') && !document.querySelector('#intelSave').disabled",timeout=12000)
            page.locator('#intelPeriod').select_option('weekly');page.wait_for_function("document.querySelector('#reportMeta').textContent.includes('주간') && !document.querySelector('#intelSave').disabled",timeout=12000)
            page.locator('#evidenceSearch').fill('stock_close');assert page.locator('#evidenceRows tr').count()==1;page.locator('#evidenceSearch').fill('')
            page.locator('#intelSave').click();page.wait_for_function("!document.querySelector('#reportDownload').hidden && document.querySelector('#historyRows [data-report]') && !document.querySelector('#intelSave').disabled",timeout=12000)
            page.locator('#historyRows [data-report]').first.click();page.wait_for_function("document.querySelector('#intelStatus').textContent.includes('저장 당시')",timeout=12000)
            print(label+': report history OK',flush=True)
            # No external network; exercise live calendar manual input on isolated synthetic database.
            cal.DEMO=False
            page.locator('#calendarDays').select_option('90');page.wait_for_function("!document.querySelector('#eventSave').disabled",timeout=12000)
            page.locator('#calendarPanel details summary').click();page.locator('#eventTitle').fill('PCE 테스트');page.locator('#eventAgency').select_option('BEA');page.locator('#eventDay').fill(str(cal.today()));page.locator('#eventTime').fill('08:30');page.locator('#eventSource').fill('https://www.bea.gov/news/schedule');page.locator('#eventConfirm').check();page.locator('#eventSave').click();page.wait_for_function("document.querySelector('#eventStatus').textContent.includes('완료')",timeout=12000)
            page.locator('#calendarRows [data-edit]').click();assert page.locator('#eventTitle').input_value()=='PCE 테스트';page.locator('#eventTitle').fill('PCE 테스트 수정');page.locator('#eventConfirm').check();page.locator('#eventSave').click();page.wait_for_function("document.querySelector('#calendarRows').textContent.includes('PCE 테스트 수정')",timeout=12000)
            page.locator('#calendarRows [data-delete]').click();page.wait_for_function("document.querySelector('#eventStatus').textContent.includes('삭제 완료')",timeout=12000)
            print(label+': calendar CRUD OK',flush=True)
            cal.DEMO=True
            page.set_content(html('/admin').replace('</head>',boot+'</head>'),wait_until='load');page.wait_for_function("document.querySelector('#AI_DAILY_LIMIT').value==='3' || document.querySelector('#AI_DAILY_LIMIT').value==='2'",timeout=12000)
            page.locator('#AI_PROVIDER').select_option('openai');page.locator('#OPENAI_API_KEY').fill('test-ui-fake-key');page.locator('#OPENAI_MODEL').fill('test-model');page.locator('#AI_DAILY_LIMIT').fill('2');page.locator('#saveAiKeys').click();page.wait_for_function("document.querySelector('#flag-OPENAI').textContent.includes('키 등록됨') && document.querySelector('#OPENAI_API_KEY').value===''",timeout=12000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'admin overflow');assert not errors,errors
            page.locator('#aiSetup').screenshot(path=str(out/f'ai-settings-{label}.png'));page.close();print(label+': preview, stock/weekly, search, history, manual calendar edit/delete, settings OK')
        browser.close()
