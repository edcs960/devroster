/* Common accessible navigation. External contents never populate this menu. */
(() => {
  'use strict';
  if (document.getElementById('app-menu-toggle')) return;
  const brand = globalThis.AppBrand;
  const productName = brand?.name || '작업 공간';
  const tagline = brand?.tagline || '기획부터 개발·검증까지';
  const make = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
  // Supplied Feather v4.29.2 symbols live in the existing same-origin SVG asset.
  const icon = name => {
    if(!document.createElementNS) return null;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
    svg.setAttribute('class','app-nav-icon');
    const use=document.createElementNS('http://www.w3.org/2000/svg','use');
    use.setAttribute('href','/brand-mark.svg#nav-'+name);svg.append(use);return svg;
  };
  const iconNames={'/home':'home','/studio':'file-text','/workflows':'git-branch','/agent':'terminal','/dashboard':'bar-chart-2','/review':'check-square','/source':'search','/traceability':'list','/changes':'edit-3','/setup':'settings','/ai':'settings','/integrations':'link','/references':'image','/wiki':'check-square','/messengers':'link'};
  const toggle = make('button', '☰', 'app-menu-toggle');toggle.id='app-menu-toggle';toggle.type='button';toggle.setAttribute('aria-label','전체 메뉴 열기');toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls','app-menu');
  const backdrop=make('div',undefined,'app-menu-backdrop');backdrop.hidden=true;
  const panel=make('aside',undefined,'app-menu');panel.id='app-menu';panel.hidden=true;panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-label','전체 메뉴');
  const heading=make('div',undefined,'app-menu-heading');heading.append(make('strong',productName));const close=make('button','×','app-menu-close');close.type='button';close.setAttribute('aria-label','메뉴 닫기');heading.append(close);panel.append(heading,make('p',tagline,'app-menu-subtitle'));
  const sections=[
    ['홈',['/home','프로젝트 홈','준비 상태와 다음 할 일']],
    ['기획·설계',['/studio','Studio','기획서·명세서·화면 검토'],['/workflows','개발 흐름','프리셋과 단계 편집']],
    ['개발·검토',['/agent','에이전트 작업실','입력 확인과 실행'],['/dashboard','개발 현황','작업 상태와 근거'],['/review','변경 검토','작업을 선택해 코드·화면 검토'],['/source','소스 탐색','읽기 전용 원문과 관계'],['/traceability','요구사항 근거','연결과 실제 검증 근거'],['/changes','요구사항 변경','변경·영향·후속 추적']],
    ['설정·자료',['/ai','AI 도구','로그인·모델·API 키'],['/setup','프로젝트 설정','폴더·사용할 AI·개발 환경'],['/integrations','외부 도구 연결','MCP·인증·연결 검사'],['/references','화면 참고 자료','Stitch·Figma 원본 읽기'],['/wiki','위키 승인','재발 방지·테스트 지식 검토'],['/messengers','외부 메신저','슬랙 요청·승인 연결']]
  ];
  const currentPath=location.pathname.replace(/\/$/,'');
  const nav=make('nav');nav.setAttribute('aria-label','빠른 이동');for(const [label,...links] of sections){nav.append(make('h2',label));for(const [href,title,description] of links){const link=make('a');link.href=href;const mark=icon(iconNames[href]);if(mark)link.append(mark);link.append(make('span',title),make('small',description));if(currentPath===href)link.setAttribute('aria-current','page');nav.append(link);}}panel.append(nav);
  const desktop = globalThis.matchMedia?.('(min-width:1200px)');
  document.body.classList.add('app-shell');
  let prior=null;
  function syncRole() {
    const wide=Boolean(desktop?.matches);
    panel.setAttribute('role',wide?'navigation':'dialog');
    if(wide) panel.removeAttribute?.('aria-modal'); else panel.setAttribute('aria-modal','true');
  }
  function open(){
    prior=document.activeElement;panel.hidden=false;
    backdrop.hidden=Boolean(desktop?.matches);
    toggle.setAttribute('aria-expanded','true');
    toggle.setAttribute('aria-label','전체 메뉴 닫기');
    document.body.classList.add('app-nav-visible');
    if(!desktop?.matches){document.body.classList.add('app-menu-open');close.focus();}
    syncRole();
  }
  function shut(){
    panel.hidden=true;backdrop.hidden=true;
    toggle.setAttribute('aria-expanded','false');
    toggle.setAttribute('aria-label','전체 메뉴 열기');
    document.body.classList.remove('app-menu-open','app-nav-visible');
    (desktop?.matches ? toggle : prior || toggle).focus?.();
  }
  toggle.onclick=()=>panel.hidden?open():shut();close.onclick=shut;backdrop.onclick=shut;
  panel.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();shut();return;}
    if(event.key==='Tab'&&!desktop?.matches){
      const items=[...panel.querySelectorAll('button,a')];
      const first=items[0],last=items[items.length-1];
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  });
  function setViewport(){
    syncRole();
    if(desktop?.matches) open(); else {
      if(panel.contains?.(document.activeElement)) toggle.focus?.();
      panel.hidden=true;backdrop.hidden=true;
      document.body.classList.remove('app-menu-open','app-nav-visible');
      toggle.setAttribute('aria-expanded','false');
      toggle.setAttribute('aria-label','전체 메뉴 열기');
    }
  }
  desktop?.addEventListener('change',setViewport);
  const header = document.querySelector('body > .app-header');
  if (brand?.name && header?.dataset?.pageTitle) { document.title = header.dataset.pageTitle + ' · ' + productName; }
  document.querySelectorAll?.('[data-brand-name]')?.forEach(el => { el.textContent = productName; });
  if (header) {
    const identity = make('div', undefined, 'app-header-identity');
    const brandEl = make('a', undefined, 'app-brand');
    brandEl.href = '/';
    let mark;
    if (brand?.logo) {
      mark = document.createElement('img');
      mark.src = brand.logo;
      mark.alt = '';
      mark.className = 'brand-mark';
    } else {
      mark = make('span', 'AD', 'brand-mark');
      mark.setAttribute('aria-hidden', 'true');
    }
    const name = make('span', undefined, 'app-brand-name');
    name.append(make('span', productName), make('strong', header.dataset.pageTitle || '개발 작업 공간'));
    brandEl.append(mark, name);
    identity.append(toggle, brandEl);
    header.prepend(identity);
  } else {
    toggle.classList.add('app-menu-floating');
    document.body.append(toggle);
  }
  document.body.append(backdrop,panel);
  const main = document.querySelector('body > main');
  if(main){
    if(!main.id) main.id='app-main-content';
    main.tabIndex=-1;
    const skip=make('a','본문으로 건너뛰기','app-skip-link');
    skip.href='#'+main.id;
    skip.addEventListener('click',event=>{event.preventDefault();main.focus();main.scrollIntoView({block:'start'});});
    document.body.insertBefore(skip,document.body.firstChild);
    const firstContent=document.querySelector('body > .studio-intro, body > main');
    if(firstContent) document.body.insertBefore(panel,firstContent);
  }
  if(header){const syncHeader=()=>document.documentElement.style.setProperty('--app-header-height',header.getBoundingClientRect().height+'px');syncHeader();if(typeof ResizeObserver!=='undefined')new ResizeObserver(syncHeader).observe(header);window.addEventListener('resize',syncHeader);}
  // The running app keeps its old code after a pull or an upgrade; ask for a restart.
  if(typeof fetch==='function'){fetch('/api/app/status').then(r=>r.ok?r.json():null).then(status=>{if(!status?.restart_required||document.getElementById('app-restart-notice'))return;const notice=make('div','DevRoster 코드가 바뀌었습니다. 앱을 종료하고 다시 실행해야 새 코드가 적용됩니다.','app-restart-notice');notice.id='app-restart-notice';notice.setAttribute('role','status');document.body.insertBefore(notice,document.body.firstChild);}).catch(()=>{});}
  setViewport();
})();
