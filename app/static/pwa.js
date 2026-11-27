'use strict';
(()=>{
  const install=document.getElementById('installApp');
  const hint=document.getElementById('installHint');
  let promptEvent=null;
  function installed(){return window.matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;}
  function render(){if(install)install.hidden=installed();if(hint&&installed())hint.textContent='독립 앱 화면으로 실행 중입니다.';}
  window.addEventListener('beforeinstallprompt',e=>{if(!install)return;e.preventDefault();promptEvent=e;render();});
  window.addEventListener('appinstalled',()=>{promptEvent=null;render();});
  if(install)install.addEventListener('click',async()=>{
    if(!window.isSecureContext){if(hint)hint.textContent='HTTPS 또는 localhost에서 설치할 수 있습니다.';return;}
    if(promptEvent){const p=promptEvent;promptEvent=null;try{await p.prompt();await p.userChoice;}catch{}render();return;}
    if(hint)hint.textContent='Android Chrome/Samsung Internet: 메뉴 → 홈 화면에 추가 또는 앱 설치. iPhone Safari: 공유 → 홈 화면에 추가. 브라우저별 지원 여부가 다릅니다.';
  });
  if(window.isSecureContext&&'serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js',{scope:'/',updateViaCache:'none'}).catch(()=>{if(hint)hint.textContent='앱 설치 준비를 확인하지 못했습니다. HTTPS와 로그인 상태를 확인하세요.';});}
  render();
})();
