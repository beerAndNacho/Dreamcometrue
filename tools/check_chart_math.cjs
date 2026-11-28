const assert=require('node:assert/strict');
const {dateValue,normalize,segments,cumulative,axisTicks}=require('../app/static/terminal-chart.js');
const D=86400000;
assert.ok(Number.isNaN(dateValue('2026-02-30')));
assert.equal(dateValue('2026-02'),Date.parse('2026-02-01'));
const rows=[{date:'2026-01-01',value:10},{date:'2026-01-02',value:null},{date:'2026-01-03',value:0},{date:'2026-02-01',value:3}];
assert.deepEqual(segments(normalize(rows),10*D).map(x=>x.map(p=>p[1])),[[10],[0],[3]]);
assert.deepEqual(normalize([{date:'2026-01-02',value:2},{date:'2026-01-01',value:1}]).map(p=>p[1]),[1,2]);
assert.deepEqual(cumulative(rows,'value').map(p=>p.value),[10,null,null,null]);
assert.deepEqual(cumulative([{date:'2026-01-01',x:3},{date:'2026-01-02',x:-3},{date:'2026-01-20',x:2}],'x').map(p=>p.value),[3,0,null]);
assert.deepEqual(segments(normalize([{date:2023,value:10},{date:2025,value:20}],'year'),1).map(x=>x.length),[1,1]);
console.log('Chart gap, date, missing, zero and cumulative tests passed');

for(const [lo,hi] of [[98.02,102.08],[2668.399,3160.4234],[-.001,.0003],[100000000,100500000],[1e-12,2e-12],[0,1]]){
 const a=axisTicks(lo,hi);
 assert.ok(a.ticks[0]<=lo+Math.abs(lo)*1e-12&&a.ticks.at(-1)>=hi-Math.abs(hi)*1e-12);
 assert.ok(a.ticks.length>=2&&a.ticks.length<=7);
 assert.equal(new Set(a.labels).size,a.ticks.length);
 assert.ok(a.ticks.every(Number.isFinite));
}
assert.ok(axisTicks(98.02,102.08).ticks.includes(100));
console.log('Readable distinct chart axis ticks passed');
