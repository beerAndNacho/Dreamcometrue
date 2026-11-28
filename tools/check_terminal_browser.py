"""Real local HTTP + Chromium, isolated synthetic data. No real credentials or external calls."""
import os
import sys
import tempfile
import subprocess
import time
import socket
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

OUT=Path(sys.argv[1] if len(sys.argv)>1 else 'ui-checks').resolve()
OUT.mkdir(parents=True,exist_ok=True)

def run(base):
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        for width,height in [(320,800),(390,844),(768,1024),(1440,1000)]:
            ctx=browser.new_context(viewport={'width':width,'height':height},device_scale_factor=1,
                http_credentials={'username':'admin','password':'ui-check-password-123456'})
            page=ctx.new_page()
            page.set_default_timeout(18000)
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            def go(path,selector=None):
                page.goto(base+path,wait_until='networkidle')
                if selector:page.locator(selector).first.wait_for(state='visible')
            def check_layout(label):
                overflow=page.evaluate('''() => ({width:innerWidth,scroll:document.documentElement.scrollWidth})''')
                assert overflow['scroll']<=overflow['width']+2,(width,label,overflow)
            def check_chart(selector):
                chart=page.locator(selector+' .macro-time-chart')
                chart.wait_for()
                page.wait_for_function('''sel=>{const el=document.querySelector(sel),svg=el?.querySelector('.macro-time-chart');if(!svg)return false;const css=getComputedStyle(el),available=Math.min(1100,el.clientWidth-parseFloat(css.paddingLeft)-parseFloat(css.paddingRight));return svg.getBoundingClientRect().width>=available*.92}''',arg=selector)
                stats=chart.evaluate('''svg=>({scale:svg.getScreenCTM().a,axis:[...svg.querySelectorAll('.macro-y-axis')].map(n=>n.textContent),bounds:[...svg.querySelectorAll('.macro-axis')].map(n=>{const b=n.getBoundingClientRect(),s=svg.getBoundingClientRect();return b.left>=s.left-2&&b.right<=s.right+2&&b.top>=s.top-2&&b.bottom<=s.bottom+2})})''')
                assert stats['scale']>=.90,(width,selector,stats)
                assert all(stats['bounds']),(width,selector,'axis label clipped',stats)
                assert len(set(stats['axis']))==len(stats['axis']),(width,selector,'duplicate ticks')
            def snap(name):
                page.screenshot(path=str(OUT/f'{width}-{name}.png'))
            go('/','#cliChart .macro-time-chart')
            expect(page.locator('#qualitySummary')).to_contain_text('103')
            check_layout('dashboard-dark')
            check_chart('#cliChart')
            expect(page.locator('#marketDiagnosis .domain-card')).to_have_count(7)
            expect(page.locator('#semiconductorDiagnosis .domain-card')).to_have_count(5)
            page.locator('#validationLoad').click()
            expect(page.locator('#validationResults .domain-card')).to_have_count(7)
            page.locator('#validationResults .domain-card summary').first.click()
            expect(page.locator('#validationResults')).to_contain_text('아직 완료된 표본 없음')
            page.evaluate('window.scrollTo(0,0)')
            snap('dashboard-dark')
            page.locator('#cliChart').scroll_into_view_if_needed()
            page.locator('#cliChart').screenshot(path=str(OUT/f'{width}-cli.png'))
            page.locator('#search').fill('코스피')
            page.locator('#indicators [data-series]').first.click()
            page.locator('#detailChart .macro-time-chart').wait_for()
            expect(page.locator('#detailChart .chart-values')).not_to_be_empty()
            check_chart('#detailChart')
            page.locator('#detailChart input[type=range]').fill('0')
            page.locator('#detailPanel').screenshot(path=str(OUT/f'{width}-detail.png'))
            if width<=760:
                page.locator('#terminal-menu').click()
                expect(page.locator('#terminal-sidebar')).to_be_visible()
                expect(page.locator('#terminal-menu')).to_have_attribute('aria-expanded','true')
                page.keyboard.press('Escape')
                expect(page.locator('#terminal-sidebar')).to_be_hidden()
            page.locator('[data-theme-toggle]').first.click()
            expect(page.locator('html')).to_have_attribute('data-theme','light')
            check_layout('dashboard-light')
            page.evaluate('window.scrollTo(0,0)')
            snap('dashboard-light')
            go('/kr-market','#flowChart .macro-time-chart')
            check_layout('market')
            check_chart('#flowChart')
            page.locator('#chartMode').select_option('cum')
            expect(page.locator('#flowChart .chart-values')).not_to_be_empty()
            page.locator('#flow').screenshot(path=str(OUT/f'{width}-flow.png'))
            go('/intelligence','#reportPanel')
            expect(page.locator('#promptCopy')).to_be_enabled()
            check_layout('report')
            snap('report')
            page.locator('.terminal-report-actions').screenshot(path=str(OUT/f'{width}-report-actions.png'))
            page.locator('[data-intel-view=calendar]').click()
            expect(page.locator('#calendarPanel')).to_be_visible()
            expect(page.locator('#reportPanel')).to_be_hidden()
            check_layout('calendar')
            snap('calendar')
            page.locator('[data-intel-view=report]').click()
            page.route('**/api/intelligence?*',lambda route:route.fulfill(status=503,content_type='application/json',body='{"detail":"UI test failure"}'))
            page.locator('#intelPreview').click()
            expect(page.locator('#intelStatus')).to_contain_text('조회 실패')
            expect(page.locator('#promptCopy')).to_be_disabled()
            expect(page.locator('#intelSave')).to_be_disabled()
            page.unroute('**/api/intelligence?*')
            page.locator('#intelPreview').click()
            expect(page.locator('#promptCopy')).to_be_enabled()
            for path,sel in [('/kr-insights','#terminal-sidebar'),('/kr-pressure','#programPanel'),('/kr-value','#annualPanel'),('/admin','#adminStats'),('/manual','#manual-search')]:
                go(path,None if path=='/kr-insights' else sel)
                check_layout(path)
                if path=='/kr-value':
                    check_chart('#annualChart')
                    check_chart('#dividendChart')
                if path=='/kr-pressure':
                    check_chart('#programData .pressure-graph-host')
                snap(path.strip('/'))
            assert not errors,(width,errors)
            print(f'{width}px browser smoke passed',flush=True)
            ctx.close()
        browser.close()

if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env={**os.environ,'DEMO_MODE':'1','HOSTED_MODE':'0','DEMO_DATABASE_URL':'sqlite:///'+tmp+'/demo.db','SETTINGS_DIR':tmp+'/settings','ADMIN_PASSWORD':'ui-check-password-123456','AUTO_COLLECT':'0'}
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],env=env)
        try:
            with httpx.Client(timeout=2) as client:
                for _ in range(100):
                    if proc.poll() is not None:raise RuntimeError('server exited')
                    try:
                        if client.get(base+'/healthz').status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.2)
                else:raise RuntimeError('server readiness timed out')
                r=client.put(base+'/api/kr-market/watch',auth=('admin',env['ADMIN_PASSWORD']),headers={'X-Macro-Admin':'1'},json={'code':'000270','name':'기아 UI 테스트'})
                r.raise_for_status()
            run(base)
        finally:
            proc.terminate()
            try:proc.wait(timeout=10)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
