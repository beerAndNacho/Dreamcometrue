/* Observation charts: calendar spacing, explicit missing values and responsive axes. */
'use strict';
(()=>{
 const DAY=86400000, NS='http://www.w3.org/2000/svg';
 function dateValue(raw,mode='date'){
  if(mode==='year')return typeof raw==='number'&&Number.isFinite(raw)?raw:NaN;
  if(typeof raw!=='string'||!/^\d{4}-\d{2}(?:-\d{2})?$/.test(raw))return NaN;
  const date=raw.length===7?raw+'-01':raw,t=Date.parse(date);
  return Number.isFinite(t)&&new Date(t).toISOString().slice(0,10)===date?t:NaN;
 }
 function normalize(points,mode){const data=new Map();for(const p of points||[]){const t=dateValue(p.date,mode);if(Number.isFinite(t))data.set(t,Number.isFinite(p.value)?p.value:null)}return [...data].sort((a,b)=>a[0]-b[0])}
 function segments(points,maxGap=Infinity){const out=[];let part=[],last=null;for(const [t,v] of points){if(v===null||(last!==null&&t-last>maxGap)){if(part.length)out.push(part);part=[]}if(v!==null)part.push([t,v]);last=v===null?null:t}if(part.length)out.push(part);return out}
 function cumulative(rows,key,maxDays=10){let sum=0,valid=true,last=null;return rows.map(r=>{const t=dateValue(r.date);if(!Number.isFinite(t)||!Number.isFinite(r[key])||(last!==null&&(t<=last||t-last>maxDays*DAY)))valid=false;last=t;return {date:r.date,value:valid?(sum+=r[key]):null}})}
 // The same pure functions run in Node CI and in the browser.
 function axisTicks(lo,hi){
  const span=hi-lo;
  if(!Number.isFinite(span)||span<=0)throw new RangeError('Invalid chart axis');
  const raw=span/4, power=10**Math.floor(Math.log10(raw));
  const step=([1,2,2.5,5,10].find(v=>v*power>=raw)||10)*power;
  const first=Math.floor(lo/step),last=Math.ceil(hi/step);
  const ticks=Array.from({length:last-first+1},(_,i)=>Number(((first+i)*step).toPrecision(14)));
  const magnitude=Math.max(Math.abs(ticks[0]),Math.abs(ticks.at(-1)));
  const scale=[[1e12,'조'],[1e8,'억'],[1e4,'만']].find(([n])=>magnitude>=n&&step>=n*.001)||[1,''];
  const scaledStep=step/scale[0];
  // A 2.5 step, for example, requires one decimal place. Preserve distinct ticks.
  let digits=0;while(digits<10&&Math.abs(scaledStep*10**digits-Math.round(scaledStep*10**digits))>1e-7*Math.max(1,scaledStep*10**digits))digits++;
  const labels=ticks.map(v=>{const x=v/scale[0];return (Math.abs(x)>0&&Math.abs(x)<1e-9?x.toExponential(2):x.toLocaleString('ko-KR',{maximumFractionDigits:digits}))+scale[1]});
  // Extremely small or extremely close values: do not display duplicate labels.
  return {ticks,labels:new Set(labels).size===labels.length?labels:ticks.map(v=>v.toPrecision(10)),step};
 }
 const helpers={dateValue,normalize,segments,cumulative,axisTicks};
 if(typeof module!=='undefined'&&module.exports){module.exports=helpers;return}
 const cache=new Map(),format=v=>Number.isFinite(v)?(v!==0&&Math.abs(v)<.0001?v.toExponential(3):v.toLocaleString('ko-KR',{maximumFractionDigits:4})):'—';
 const dateLabel=(t,mode)=>mode==='year'?String(t):new Date(t).toISOString().slice(0,10);
 const node=(tag,attrs={},content)=>{const e=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v));if(content!=null)e.textContent=content;return e};
 const resolve=e=>typeof e==='string'?document.getElementById(e):e;
 function width(el){const c=getComputedStyle(el);return Math.max(200,Math.min(1100,Math.floor(el.clientWidth-parseFloat(c.paddingLeft||0)-parseFloat(c.paddingRight||0))||300))}
 function clear(el){el=resolve(el);if(!el)return;cache.get(el)?.observer?.disconnect();cache.delete(el);el.replaceChildren()}
 function draw(el,input,options={}){
  el=resolve(el);if(!el)return;
  for(const [other,e] of cache)if(!other.isConnected){e.observer?.disconnect();cache.delete(other)}
  clear(el);el.classList.add('macro-chart-host');const mode=options.mode==='year'?'year':'date';
  const entry={lines:input.map(s=>({...s,points:normalize(s.points,mode)})),options,mode,width:0,cursor:null,observer:null,root:null};
  cache.set(el,entry);render(el,entry);
  if(typeof ResizeObserver!=='undefined'){entry.observer=new ResizeObserver(()=>{if(!el.isConnected||!entry.root||!el.contains(entry.root)){entry.observer.disconnect();if(cache.get(el)===entry)cache.delete(el);return}if(cache.get(el)===entry&&Math.abs(width(el)-entry.width)>4)render(el,entry)});entry.observer.observe(el)}
 }
 function render(el,entry){
  const {lines,options,mode}=entry,vals=lines.flatMap(s=>s.points.map(p=>p[1])).filter(Number.isFinite);
  const dates=[...new Set(lines.flatMap(s=>s.points.map(p=>p[0])))].sort((a,b)=>a-b);
  const W=entry.width=width(el);el.replaceChildren();
  const root=document.createElement('div');root.className='macro-chart-root';el.append(root);entry.root=root;
  if(!vals.length||!dates.length){root.textContent=options.empty||'유효한 관측값이 없습니다.';root.classList.add('note');return}
  const H=W<480?240:Math.max(260,Math.min(380,Math.round(W*.38))),R=19,T=26,B=36;
  const minT=dates[0],maxT=dates.at(-1),base=Number.isFinite(options.baseline)?options.baseline:null;
  let lo=Math.min(...vals,base??Infinity),hi=Math.max(...vals,base??-Infinity);
  const pad=hi===lo?Math.max(Math.abs(lo)*.025,.01):(hi-lo)*.06;lo-=pad;hi+=pad;
  const marks=axisTicks(lo,hi);lo=marks.ticks[0];hi=marks.ticks.at(-1);
  const L=Math.max(48,Math.min(Math.floor(W*.43),Math.max(...marks.labels.map(s=>s.length))*7.6+12));
  const X=t=>minT===maxT?(L+W-R)/2:L+(W-L-R)*(t-minT)/(maxT-minT),Y=v=>T+(H-T-B)*(hi-v)/(hi-lo);
  const svg=node('svg',{viewBox:[0,0,W,H].join(' '),class:'macro-time-chart',role:'img','aria-label':(options.title||lines.map(s=>s.label).join(' / '))+' 관측 추이'});
  if(options.unit)svg.append(node('text',{x:L,y:14,class:'macro-axis'},options.unit));
  marks.ticks.forEach((v,i)=>{const y=Y(v);svg.append(node('line',{x1:L,x2:W-R,y1:y,y2:y,class:'macro-grid'}),node('text',{x:L-6,y:y+4,'text-anchor':'end',class:'macro-axis macro-y-axis'},marks.labels[i]))});
  if(base!==null)svg.append(node('line',{x1:L,x2:W-R,y1:Y(base),y2:Y(base),class:'macro-baseline'}));
  const n=Math.min(dates.length,W<480?2:W<800?3:5),ticks=[];
  for(let i=0;i<n;i++){const t=n===1?minT:minT+(maxT-minT)*i/(n-1),nearest=dates.reduce((a,b)=>Math.abs(b-t)<Math.abs(a-t)?b:a,dates[0]);if(!ticks.includes(nearest))ticks.push(nearest)}
  // Dates can be highly clustered. Keep the ends and only insert internal
  // labels when their actual pixel spacing is sufficient.
  const first=ticks[0],last=ticks.at(-1),selectedTicks=first===last?[first]:[first];
  for(const t of ticks.slice(1,-1)){if(X(t)-X(selectedTicks.at(-1))>=95&&X(last)-X(t)>=95)selectedTicks.push(t)}
  if(first!==last)selectedTicks.push(last);
  const shortDates=mode==='date'&&W<420&&X(last)-X(first)<150;
  selectedTicks.forEach((t,i)=>svg.append(node('text',{x:X(t),y:H-10,'text-anchor':selectedTicks.length===1?'middle':i===0?'start':i===selectedTicks.length-1?'end':'middle',class:'macro-axis macro-date'},shortDates?dateLabel(t,mode).slice(2):dateLabel(t,mode))));
  let hasGap=false;
  for(const [i,s] of lines.entries()){
   const groups=segments(s.points,s.maxGap??options.maxGap??Infinity),tone=i%4;
   hasGap=hasGap||groups.length>1||s.points.some(p=>p[1]===null);
   for(const group of groups){
    const step=Math.max(1,Math.ceil(group.length/550)),reduced=step===1?group:[group[0],...Array.from({length:Math.ceil(group.length/step)},(_,j)=>{const b=group.slice(j*step,(j+1)*step);let a=0,c=0;for(let k=1;k<b.length;k++){if(b[k][1]<b[a][1])a=k;if(b[k][1]>b[c][1])c=k}return a===c?[b[a]]:[b[Math.min(a,c)],b[Math.max(a,c)]]}).flat(),group.at(-1)];
    if(group.length>1)svg.append(node('polyline',{points:reduced.map(([t,v])=>X(t).toFixed(2)+','+Y(v).toFixed(2)).join(' '),fill:'none',class:'macro-trace macro-tone-'+tone,'data-series':s.label||String(i),'vector-effect':'non-scaling-stroke'}));
    const [t,v]=group.at(-1),dot=node('circle',{cx:X(t),cy:Y(v),r:3.5,class:'macro-dot macro-tone-'+tone});dot.append(node('title',{},dateLabel(t,mode)+' · '+s.label+' '+format(v)));svg.append(dot);
   }
  }
  const cross=node('line',{class:'macro-cursor chart-cursor',y1:T,y2:H-B,'pointer-events':'none'});svg.append(cross);root.append(svg);
  const panel=document.createElement('div');panel.className='chart-scrub macro-chart-controls';
  const legend=document.createElement('div');legend.className='macro-chart-legend';
  const label=document.createElement('label');label.textContent=mode==='year'?'연도 탐색':'관측일 탐색';
  const range=document.createElement('input');range.type='range';range.min=0;range.max=dates.length-1;range.step=1;range.setAttribute('aria-label',label.textContent);range.disabled=dates.length===1;
  const ix=dates.indexOf(entry.cursor);range.value=ix<0?dates.length-1:ix;label.append(range);
  for(const [i,s] of lines.entries()){const key=document.createElement('span'),dot=document.createElement('i');dot.className='macro-tone-'+i%4;key.append(dot,document.createTextNode(s.label||'값'));legend.append(key)}
  const result=document.createElement('div');result.className='chart-values macro-readout';result.setAttribute('aria-live','polite');const maps=lines.map(s=>new Map(s.points));
  const periodNode=document.createElement('strong');periodNode.className='macro-readout-date';
  const valueNodes=lines.map((s,i)=>{const item=document.createElement('span'),dot=document.createElement('i'),label=document.createElement('span'),value=document.createElement('b');dot.className='macro-tone-'+i%4;label.textContent=s.label||'값';item.append(dot,label,value);return {item,value}});
  result.append(periodNode,...valueNodes.map(v=>v.item));
  function update(){const t=dates[Number(range.value)],period=dateLabel(t,mode);entry.cursor=t;range.setAttribute('aria-valuetext',period);periodNode.textContent=period;valueNodes.forEach((v,i)=>{const num=maps[i].get(t);v.value.textContent=Number.isFinite(num)?format(num)+(options.unit?' '+options.unit:''):'자료 없음'});cross.setAttribute('x1',X(t));cross.setAttribute('x2',X(t))}
  function seek(e){const matrix=svg.getScreenCTM();if(!matrix)return;const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(matrix.inverse()),t=minT+Math.max(0,Math.min(1,(p.x-L)/(W-L-R)))*(maxT-minT);let a=0,b=dates.length-1;while(a<b){const m=Math.floor((a+b)/2);if(dates[m]<t)a=m+1;else b=m}if(a>0&&Math.abs(dates[a-1]-t)<Math.abs(dates[a]-t))a--;if(Number(range.value)!==a){range.value=a;update()}}
  range.addEventListener('input',update);svg.addEventListener('pointermove',e=>{if(e.pointerType==='mouse')seek(e)});svg.addEventListener('click',seek);
  panel.append(legend,label,result);
  const note=document.createElement('p');note.className='note';note.textContent=[options.note,hasGap?'누락값/긴 관측 간격은 연결하지 않음.':''].filter(Boolean).join(' ');if(note.textContent)panel.append(note);root.append(panel);update();
 }
 window.MacroChart={...helpers,draw,clear};
})();
