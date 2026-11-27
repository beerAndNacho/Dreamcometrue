"""Render the bundled, developer-authored manual. No third-party renderer or user input."""
from html import escape
from pathlib import Path
import re
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, FileResponse

router=APIRouter(tags=['manual'])
SOURCE=Path(__file__).with_name('USER_MANUAL.md')
NAV={
    '01':('/admin','관리자 열기'),
    '02':('/admin#apiSetup','API 설정'),
    '03':('/admin#scheduleSetup','수집 일정'),
    '04':('/','대시보드'),
    '05':('/#monitor','경제지표'),
    '06':('/#scenarioPanel','위험 시뮬레이터'),
    '07':('/kr-market','국내 증시·수급'),
    '08':('/kr-insights','수급 순위·업종'),
    '09':('/kr-pressure','시장 압력'),
    '10':('/kr-value','기업 가치·배당'),
    '11':('/intelligence','투자 분석센터'),
    '12':('/admin#aiSetup','AI 설정'),
    '13':('/intelligence#calendarPanel','경제 캘린더'),
    '14':('/#portfolio','포트폴리오'),
    '15':('/#backtestPanel','백테스트'),
    '16':('/admin','관리자 연결 검사'),
    '17':('/docs','개발 API 문서'),
}
INLINE=re.compile(r'(\x60[^\x60\n]+\x60|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https://[^\s)]+\))')
SECTION=re.compile(r'^## ([0-9]{2})\. (.+)$')

def inline(text):
    """Only code, bold, and explicit HTTPS links. All other input is escaped."""
    parts=[]
    for i,chunk in enumerate(INLINE.split(text)):
        if i%2==0:parts.append(escape(chunk));continue
        if chunk.startswith('\x60'):parts.append('<code>'+escape(chunk[1:-1])+'</code>')
        elif chunk.startswith('**'):parts.append('<strong>'+escape(chunk[2:-2])+'</strong>')
        else:
            label,url=chunk[1:-1].split('](',1)
            parts.append('<a href="'+escape(url,quote=True)+'" target="_blank" rel="noopener noreferrer">'+escape(label)+'</a>')
    return ''.join(parts)

def blocks(lines):
    """Small, documented Markdown subset: paragraphs, h3, lists, fenced code."""
    out=[];para=[];listing=None;code=None
    def flush():
        if para:out.append('<p>'+inline(' '.join(para))+'</p>');para.clear()
    def close_list():
        nonlocal listing
        if listing:out.append('</'+listing+'>');listing=None
    for line in lines:
        if code is not None:
            if line.startswith('\x60\x60\x60'):
                out.append('<pre><code>'+escape('\n'.join(code))+'</code></pre>');code=None
            else:code.append(line)
            continue
        if line.startswith('\x60\x60\x60'):
            flush();close_list();code=[];continue
        if line.startswith('### '):
            flush();close_list();out.append('<h3>'+inline(line[4:])+'</h3>');continue
        match=re.match(r'^(- |[0-9]+\. )(.+)$',line)
        if match:
            flush();kind='ul' if match[1]=='- ' else 'ol'
            if kind!=listing:close_list();listing=kind;out.append('<'+kind+'>')
            out.append('<li>'+inline(match[2])+'</li>');continue
        close_list()
        if line.strip():para.append(line.strip())
        else:flush()
    flush();close_list()
    if code is not None:raise ValueError('Unclosed manual code fence')
    return ''.join(out)

def render_manual(source):
    lines=source.splitlines()
    if not lines or not lines[0].startswith('# '):raise ValueError('Manual title missing')
    title=escape(lines[0][2:])
    intro=[];sections=[];current=None;fenced=False
    for line in lines[1:]:
        if line.startswith('\x60\x60\x60'):fenced=not fenced
        match=SECTION.match(line) if not fenced else None
        if match:
            number,name=match.groups()
            if number not in NAV or any(number==s[0] for s in sections):raise ValueError('Invalid manual section')
            current=(number,name,[]);sections.append(current)
        elif current is None:intro.append(line)
        else:current[2].append(line)
    if {s[0] for s in sections}!=set(NAV):raise ValueError('Incomplete manual sections')
    toc=''.join('<li><a href="#section-'+n+'">'+n+'. '+escape(name)+'</a></li>' for n,name,_ in sections)
    content=[]
    for n,name,body in sections:
        url,label=NAV[n]
        content.append('<section class="panel manual-section" id="section-'+n+'"><div class="manual-section-head"><h2>'+n+'. '+escape(name)+'</h2><a class="button secondary" href="'+escape(url,quote=True)+'">'+escape(label)+'</a></div>'+blocks(body)+'<p class="manual-back"><a href="#manual-top">목차로 돌아가기</a></p></section>')
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>'+title+'</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/manual.css"><link rel="stylesheet" href="/static/terminal.css?v=500"></head>'
        '<body class="manual-body"><header class="manual-header"><a class="brand" href="/"><span class="logo">M</span><div><strong>Macro Invest</strong><small>USER MANUAL</small></div></a>'
        '<nav aria-label="주요 메뉴"><a href="/">대시보드</a><a href="/admin">관리자</a><a href="/intelligence">투자 분석센터</a><button type="button" class="secondary" data-theme-toggle aria-label="화면 색상 전환">라이트</button></nav></header>'
        '<main class="manual-main" id="manual-top"><div class="manual-hero"><div class="eyebrow">HELP / USER GUIDE</div><h1>'+title+'</h1>'+blocks(intro)
        +'<div class="manual-actions"><a class="button" href="/manual/download">매뉴얼 Markdown 다운로드</a><button class="secondary" id="manual-print" type="button">인쇄 / PDF 저장</button></div></div>'
        '<div class="manual-layout"><aside class="panel manual-toc" aria-label="매뉴얼 목차"><h2>목차</h2>'
        '<label for="manual-search">기능 및 문제 검색</label><input id="manual-search" type="search" placeholder="예: AI, 배당, KOSIS" autocomplete="off">'
        '<p class="note" id="manual-status" role="status">전체 17개 항목</p><ol id="manual-toc-list">'+toc+'</ol></aside>'
        '<article id="manual-content">'+''.join(content)+'</article></div>'
        '<footer>Macro Invest · 공식 원자료 및 운영 환경 검증 필요</footer></main><script src="/static/manual.js" defer></script><script src="/static/terminal.js?v=500" defer></script></body></html>')

@router.get('/manual',include_in_schema=False)
def manual_page():
    return HTMLResponse(render_manual(SOURCE.read_text(encoding='utf-8')))

@router.get('/manual/download',include_in_schema=False)
def manual_download():
    return FileResponse(SOURCE,media_type='text/markdown; charset=utf-8',filename='Macro-Invest-USER-MANUAL.md')
