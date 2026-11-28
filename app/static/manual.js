'use strict';
const search=document.getElementById('manual-search'),statusLine=document.getElementById('manual-status');
const sections=[...document.querySelectorAll('.manual-section')];
const links=[...document.querySelectorAll('#manual-toc-list a')];
const normalize=s=>s.toLowerCase().normalize('NFKC').trim();
function filter(){
 const words=normalize(search.value).split(/\s+/).filter(Boolean);
 let count=0;
 for(const section of sections){
  const visible=words.every(word=>normalize(section.textContent).includes(word));
  section.hidden=!visible;
  const link=links.find(a=>a.getAttribute('href')==='#'+section.id);
  if(link)link.closest('li').hidden=!visible;
  if(visible)count++;
 }
 statusLine.textContent=words.length?(count?count+'개 항목 검색됨':'검색 결과 없음 · 다른 검색어를 입력하세요.'):'전체 '+sections.length+'개 항목';
}
search.addEventListener('input',filter);
window.addEventListener('hashchange',()=>{const section=sections.find(s=>'#'+s.id===location.hash);if(section?.hidden){search.value='';filter();section.scrollIntoView()}});
document.getElementById('manual-print').addEventListener('click',()=>window.print());
