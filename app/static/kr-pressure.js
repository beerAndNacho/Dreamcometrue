'use strict';
(()=>{
const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), fmt=v=>Number.isFinite(v)?v.toLocaleString('ko-KR',{maximumFractionDigits:4}):'—';
const names={program:'프로그램매매',futures:'코스피200 선물',short:'공매도 거래',loan:'대차거래',short_balance:'공매도 순보유잔고'};
const fields={program:{arbitrage_qty:'차익 순매수',nonarbitrage_qty:'비차익 순매수',total_qty:'합계 순매수'},futures:{foreign_contracts:'외국인 계약',institution_contracts:'기관 계약',individual_contracts:'개인 계약'},short:{short_qty:'공매도 수량',volume:'전체 거래량',short_pct:'거래량 비중 (%)'},loan:{new_qty:'신규 주수',returned_qty:'상환 주수',balance_qty:'잔고 주수'},short_balance:{balance_qty:'잔고 주수',balance_pct:'잔고 비율 (%)'}};
const primary={program:'total_qty',futures:'foreign_contracts',short:'short_pct',loan:'balance_qty',short_balance:'balance_qty'};
const colors={program:'#82b7f9',futures:'#ad98f4',short:'#f2b77e',loan:'#75cabc',short_balance:'#f29ca5'};
let busy=false, ready=false, snapshot=null;
function status(id,msg,error=false){$(id).textContent=msg;$(id).classList.toggle('error-banner',error)}
function query(){return new URLSearchParams({market:$('pressureMarket').value,code:$('pressureStock').value,period:$('pressurePeriod').value})}
async function api(url,opts={}){let r=await fetch(url,opts);let j;try{j=await r.json()}catch{throw Error(`HTTP ${r.status}`)}if(!r.ok)throw Error(typeof j.detail==='string'?j.detail:`HTTP ${r.status}`);return j}
function graph(rows,field,color){
 const valid=rows.filter(r=>Number.isFinite(r[field]));
 if(!valid.length)return '<p class="note">차트 관측값 부족</p>';
 return '<div class="pressure-graph-host"></div><div class="pressure-small muted">'+esc(valid[0].date)+' ~ '+esc(valid.at(-1).date)+' · 최저 '+fmt(Math.min(...valid.map(r=>r[field])))+' / 최고 '+fmt(Math.max(...valid.map(r=>r[field])))+'</div>';
}
function render(kind, data, period){const el=$(kind+'Data');if(!data){el.textContent='관측값 없음';return}const keys=Object.keys(fields[kind]);const latest=data.latest,summary=data.summary;
const mark=data.demo?'가상 자료':data.origin==='manual_csv'?'사용자 등록 CSV (출처 미검증)':'KIS 일별 API';
const quality=data.stale?'누락 / 오래됨 / 오류 — 기간 요약 보류':'관측일 조건 충족 (거래일 완전성 별도 확인)';
let result=`<div class="pressure-small muted">${esc(mark)} · 관측일 ${esc(latest?.date||'—')} · ${esc(quality)} · ${esc(data.message||'')}</div>`;
if(data.updated)result+=`<p class="note">최근 저장 ${esc(new Date(data.updated).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'}))} KST</p>`;
if(latest){result+=`<div class="pressure-metrics">${keys.map(k=>`<div class="metric"><small>${esc(fields[kind][k])} · 최근</small><strong>${fmt(latest[k])}</strong></div>`).join('')}</div>`;}
if(summary){result+=`<h4>최근 ${period}개 관측일 요약</h4><div class="pressure-metrics">${Object.entries(summary).map(([k,v])=>`<div class="metric"><small>${esc(fields[kind][k]||'기간 처음 대비 잔고 증감')}${k==='balance_qty'||k==='balance_pct'?' (마지막)':k==='short_pct'?' (가중 비중)':k==='balance_change'?'':' (합계)'}</small><strong>${fmt(v)}</strong></div>`).join('')}</div>`;}else result+='<p class="note">유효한 기간 요약 없음. 수집하거나 기간을 줄이세요.</p>';
result+=graph(data.rows,primary[kind],colors[kind]);
if(data.rows.length){result+=`<details class="pressure-history"><summary>일별 관측값 ${data.rows.length}개 보기</summary><div class="table-wrap"><table><thead><tr><th>관측일</th>${keys.map(k=>`<th>${esc(fields[kind][k])}</th>`).join('')}</tr></thead><tbody>${[...data.rows].reverse().map(r=>`<tr><td>${esc(r.date)}</td>${keys.map(k=>`<td>${fmt(r[k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details>`;}el.innerHTML=result;
const host=el.querySelector('.pressure-graph-host');if(host){const field=primary[kind],unit=field.endsWith('_pct')?'%':field.endsWith('_contracts')?'계약':'주';MacroChart.draw(host,[{label:fields[kind][field],points:data.rows.map(r=>({date:r.date,value:r[field]}))}],{maxGap:10*86400000,unit,baseline:field==='balance_qty'?null:0,note:'저장 관측일 기준 · 거래일 완전성은 별도 확인.'})}
}
function show(data){snapshot=data;let code=$('pressureStock').value;let list=data.watchlist;$('pressureStock').replaceChildren(new Option('선택 안 함',''),...list.map(x=>new Option(`${x.name} (${x.code})`,x.code)));if(list.some(x=>x.code===code))$('pressureStock').value=code;
status('pressureStatus',`${data.demo?'가상 데이터 · ':''}${data.asof} KST · ${data.note}${data.busy?' · 다른 수집 진행 중':''}`);
render('program',data.data.program,data.period);render('futures',data.data.futures,data.period);let c=data.comparison;status('spotFutures',c.state==='ok'?`${c.date} · ${c.label} | 코스피 현물 ${fmt(c.spot_foreign_eok)}억원 / 코스피200 선물 ${fmt(c.futures_foreign_contracts)}계약. ${c.note}`:c.reason);
const showStock=!!data.code;$('stockData').hidden=!showStock;$('stockEmpty').hidden=showStock;$('stockTitle').textContent=showStock?($('pressureStock').selectedOptions[0]?.textContent||data.code):'등록한 관심종목을 선택하세요.';if(showStock)for(const k of ['short','loan','short_balance'])render(k,data.data[k],data.period);
ready=true;
}
function setBusy(b){busy=b;for(const id of ['pressureReload','pressureMarket','pressureStock','pressurePeriod','pressureRefresh','futuresImport','short_balanceImport','futuresFile','short_balanceFile','futuresConfirm','short_balanceConfirm'])$(id).disabled=b;if(!b){$('pressureRefresh').disabled=!ready||!!snapshot?.demo;$('futuresImport').disabled=!ready||!!snapshot?.demo;$('short_balanceImport').disabled=!ready||!!snapshot?.demo;}}
function clearStale(){ready=false;snapshot=null;for(const kind of Object.keys(fields))$(kind+'Data').textContent='선택이 바뀌었습니다. 저장 자료를 조회하세요.';$('spotFutures').textContent='비교 자료 조회 필요';$('stockData').hidden=true;$('stockEmpty').hidden=false;for(const k of ['futures','short_balance']){$(k+'Confirm').checked=false;$(k+'File').value='';$(k+'Message').textContent='';}setBusy(false)}
async function load(){if(busy)return;setBusy(true);try{show(await api('/api/kr-pressure?'+query()))}catch(e){clearStale();status('pressureStatus','조회 실패: '+e.message,true)}finally{setBusy(false)}}
$('pressureForm').onsubmit=e=>{e.preventDefault();load()};for(const id of ['pressureMarket','pressureStock','pressurePeriod'])$(id).onchange=()=>{clearStale();load()};
$('pressureRefresh').onclick=async()=>{if(busy||!ready)return;setBusy(true);status('pressureStatus','KIS 완료일 자료 수집 중…');try{let r=await api('/api/kr-pressure/refresh?'+query(),{method:'POST',headers:{'X-Macro-Admin':'1'}});show(await api('/api/kr-pressure?'+query()));status('pressureStatus',r.results.map(x=>`${names[x.kind]}: ${x.ok?x.count+'개 수집':x.message}`).join(' · '),r.results.some(x=>!x.ok))}catch(e){status('pressureStatus','수집 실패: '+e.message,true)}finally{setBusy(false)}};
for(const kind of ['futures','short_balance']){const header='date,'+Object.keys(fields[kind]).join(',');$(kind+'Template').onclick=()=>{const url=URL.createObjectURL(new Blob([header+'\n'],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=kind+'-template.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
$(kind+'Import').onclick=async()=>{if(busy||!ready||snapshot?.demo)return;const file=$(kind+'File').files[0],confirmed=$(kind+'Confirm').checked,code=kind==='short_balance'?$('pressureStock').value:'';if(!file||!confirmed||file.size>200000||(kind==='short_balance'&&!code)){status(kind+'Message','파일·확인 체크·종목을 확인하세요 (최대 200KB).',true);return}setBusy(true);try{let r=await api('/api/kr-pressure/import',{method:'POST',headers:{'X-Macro-Admin':'1','Content-Type':'application/json'},body:JSON.stringify({kind,code,confirmed,csv:await file.text()})});show(await api('/api/kr-pressure?'+query()));status(kind+'Message',`${r.count}개 등록 · ${r.first} ~ ${r.last}`);$(kind+'Confirm').checked=false;$(kind+'File').value=''}catch(e){status(kind+'Message','등록 실패: '+e.message,true)}finally{setBusy(false)}};}
load();
})();
