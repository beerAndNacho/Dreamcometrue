"""Isolated Chromium interaction test using real FastAPI response bridge.
No live Telegram calls. Browser service worker installation requires a real HTTPS test.
"""
import os,sys,tempfile,re,base64
from pathlib import Path
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
out=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve();out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    os.environ.update(DEMO_MODE='1',HOSTED_MODE='0',DEMO_DATABASE_URL='sqlite:///'+tmp+'/demo.db',SETTINGS_DIR=tmp+'/settings',ADMIN_PASSWORD='ui-check-password-123456',AUTO_COLLECT='0',ALERT_ENABLED='0')
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
            txt=re.sub(r'src="(/static/[^" ]+\.png)"',lambda m:'src="data:image/png;base64,'+base64.b64encode(client.get(m[1],auth=auth).content).decode()+'"',txt)
            return re.sub(r'<script src="([^"]+)" defer></script>',lambda m:'<script>'+client.get(m[1],auth=auth).text+'</script>',txt)
        def bridge(path,opts):
            r=client.request(opts.get('method','GET'),path,headers=opts.get('headers',{}),content=opts.get('body'),auth=auth)
            return {'status':r.status_code,'body':r.text}
        boot='''<script>window.fetch=async(path,opts={})=>{const r=await pyFetch(String(path),opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})};</script>'''
        for label,w,h in [('desktop',1440,1100),('mobile',390,844)]:
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);page.set_default_timeout(8000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',bridge)
            def show(path):page.set_content(html(path).replace('</head>',boot+'</head>'),wait_until='load')
            show('/notifications');page.wait_for_function("document.querySelector('#alertStatus').textContent.includes('DEMO')")
            assert page.locator('#testAlert').is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'overflow')
            page.screenshot(path=str(out/f'notifications-{label}.png'),full_page=True)
            page.locator('#TELEGRAM_BOT_TOKEN').fill('123456789:example_fake_bot_token_1234567890');page.locator('#TELEGRAM_CHAT_ID').fill('123456789');page.locator('#ALERT_HOUR_KST').fill('10');page.locator('#ALERT_PRICE_RULES').fill('005930,above,100000');page.locator('#saveAlerts').click()
            page.wait_for_function("document.querySelector('#alertStatus').textContent.includes('저장했습니다')")
            assert page.locator('#TELEGRAM_BOT_TOKEN').input_value()=='' and page.locator('#TELEGRAM_CHAT_ID').input_value()==''
            assert page.locator('#testAlert').is_disabled()
            page.close()
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);page.set_default_timeout(8000);page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.expose_function('pyFetch',bridge)
            show('/notifications');page.wait_for_function("document.querySelector('#ALERT_HOUR_KST').value==='10'")
            assert page.locator('#ALERT_PRICE_RULES').input_value()=='005930,above,100000'
            page.locator('#clearTelegram').check();page.locator('#saveAlerts').click();page.wait_for_function("document.querySelector('#telegramConfigured').textContent.includes('미등록') && document.querySelector('#alertStatus').textContent.includes('저장했습니다')")
            page.close()
            page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1);page.set_default_timeout(8000);page.on('pageerror',lambda e:errors.append(str(e)));page.expose_function('pyFetch',bridge)
            show('/admin');page.wait_for_function("document.querySelector('#KOSIS_LEADING_QUERY').value.includes('DT_1C8015')")
            assert page.locator('#notificationSetup a').get_attribute('href')=='/notifications'
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(label,'admin overflow')
            assert not errors,errors
            page.close();print(label+': settings, reload, clearing, demo guard, KOSIS and admin navigation OK (response bridge; actual PWA installation not tested)',flush=True)
        browser.close()
