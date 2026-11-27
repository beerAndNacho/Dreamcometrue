/* Shared shell: local preference only; no data calls or external dependencies. */
'use strict';
(()=>{
 const root=document.documentElement,body=document.body,buttons=[...document.querySelectorAll('[data-theme-toggle]')];
 let theme='dark';try{if(localStorage.getItem('macro-theme-v5')==='light')theme='light'}catch{}
 function applyTheme(){
  root.dataset.theme=theme;
  for(const button of buttons){button.textContent=theme==='dark'?'라이트':'다크';button.setAttribute('aria-pressed',String(theme==='light'));button.setAttribute('aria-label',theme==='dark'?'라이트 모드 전환':'다크 모드 전환')}
 }
 applyTheme();for(const button of buttons)button.addEventListener('click',()=>{theme=theme==='dark'?'light':'dark';try{localStorage.setItem('macro-theme-v5',theme)}catch{}applyTheme()});
 const menu=document.getElementById('terminal-menu'),sidebar=document.getElementById('terminal-sidebar');
 if(!menu||!sidebar)return;
 const mobile=matchMedia('(max-width: 760px)');
 function setMenu(open,focus=false){
  body.classList.toggle('terminal-menu-open',open);menu.setAttribute('aria-expanded',String(open));
  if(focus){if(open)sidebar.querySelector('a')?.focus();else menu.focus()}
 }
 menu.addEventListener('click',()=>setMenu(!body.classList.contains('terminal-menu-open'),true));
 sidebar.addEventListener('click',e=>{if(e.target.closest('a')&&mobile.matches)setMenu(false)});
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&body.classList.contains('terminal-menu-open'))setMenu(false,true)});
 document.addEventListener('click',e=>{if(body.classList.contains('terminal-menu-open')&&!sidebar.contains(e.target)&&!menu.contains(e.target))setMenu(false)});
 mobile.addEventListener('change',()=>setMenu(false));
 body.classList.add('terminal-ready');
 function markLocal(){
  const path=location.pathname,hash=location.hash;
  sidebar.querySelectorAll('.local-navigation a').forEach(a=>{if(a.getAttribute('href')===hash)a.setAttribute('aria-current','location');else a.removeAttribute('aria-current')});
  if(path==='/'){
   const asset=hash==='#portfolio'||hash==='#backtestPanel';
   document.querySelectorAll('.terminal-bottom a').forEach(a=>{const on=a.getAttribute('href')===(asset?'/#portfolio':'/');a.classList.toggle('active',on);if(on)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});
  }
 }
 window.addEventListener('hashchange',markLocal);markLocal();
})();
