from html.parser import HTMLParser
import re
from fastapi.testclient import TestClient
from app.main import app
from app.manual import SOURCE,NAV,inline,blocks,render_manual

AUTH=('admin','test-admin-password-1234')

class Links(HTMLParser):
    def __init__(self):
        super().__init__();self.ids=set();self.hrefs=[];self.scripts=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if 'id' in a:
            assert a['id'] not in self.ids
            self.ids.add(a['id'])
        if tag=='a' and 'href' in a:self.hrefs.append(a['href'])
        if tag=='script':self.scripts.append(a.get('src'))

def test_manual_and_download_same_source():
    source=SOURCE.read_text(encoding='utf-8')
    with TestClient(app) as c:
        r=c.get('/manual')
        assert r.status_code==200 and r.text==render_manual(source)
        assert 'Content-Security-Policy' in r.headers
        assert c.get('/manual/download').content==SOURCE.read_bytes()
        assert 'attachment' in c.get('/manual/download').headers['content-disposition']
        assert c.get('/static/manual.js').status_code==200
        assert c.get('/static/manual.css').status_code==200
        p=Links();p.feed(r.text)
        assert p.scripts==['/static/manual.js','/static/terminal.js?v=512']
        for n,(route,_) in NAV.items():
            assert 'section-'+n in p.ids and '#section-'+n in p.hrefs and route in p.hrefs
            page,_,fragment=route.partition('#')
            result=c.get(page,auth=AUTH)
            assert result.status_code==200,route
            if fragment:assert re.search(r'\bid=["\']'+re.escape(fragment)+r'["\']',result.text),route
        assert len(re.findall(r'^## \d{2}\.',source,re.M))==17
        for key in ['103개','5개 파생','프롬프트 + 데이터 복사','OpenDART 연결 검사','백테스트','관세청','수급 순위','포트폴리오']:
            assert key in r.text

def test_manual_visible_on_all_main_pages():
    with TestClient(app) as c:
        for path in ['/','/admin','/kr-market','/kr-insights','/kr-pressure','/kr-value','/intelligence']:
            r=c.get(path,auth=AUTH)
            assert r.status_code==200 and 'href="/manual"' in r.text,path

def test_safe_subset_renderer():
    assert '<script>' not in inline('<script>alert(1)</script>')
    assert 'href="javascript:' not in inline('[bad](javascript:alert(1))')
    assert '<a href="https://example.test"' in inline('[example](https://example.test)')
    assert '<img' not in blocks(['\x60\x60\x60','<img src=x onerror=alert(1)>','\x60\x60\x60'])
    assert '<li>first</li>' in blocks(['1. first','2. second'])
