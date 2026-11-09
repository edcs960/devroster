/* Common accessible navigation. External contents never populate this menu.
   Layout: ≥1440 full sidebar · 1024–1439 icon rail (tooltips) · ≤1023 drawer opened by #app-menu-toggle.
   The page's own header.app-header becomes the sticky top bar (section / page, page actions). */
(() => {
  'use strict';
  if (document.getElementById('app-menu-toggle')) return;
  const brand = globalThis.AppBrand;
  const productName = brand?.name || '작업 공간';
  const tagline = brand?.tagline || '기획부터 개발·검증까지';
  const make = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
  // Supplied Feather v4.29.2 symbols live in the existing same-origin SVG asset.
  const icon = name => {
    if (!document.createElementNS) return null;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'app-nav-icon');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '/brand-mark.svg#nav-' + name); svg.append(use); return svg;
  };
  const iconNames = { '/home': 'home', '/studio': 'file-text', '/workflows': 'git-branch', '/agent': 'terminal', '/review': 'check-square', '/source': 'search', '/traceability': 'list', '/changes': 'edit-3', '/setup': 'settings', '/ai': 'sliders', '/integrations': 'link', '/references': 'image', '/wiki': 'archive', '/messengers': 'message-square' };
  const sections = [
    ['홈', ['/home', '프로젝트 홈', '준비 상태와 다음 할 일']],
    ['기획·설계', ['/studio', 'Studio', '기획서·명세서·화면 검토'], ['/workflows', '개발 흐름', '프리셋과 단계 편집']],
    ['개발·검토', ['/agent', '작업실', '작업 현황 확인과 실행'], ['/review', '변경 검토', '코드·화면 검토와 결정'], ['/source', '소스 탐색', '읽기 전용 원문과 관계'], ['/traceability', '요구사항 근거', '연결과 실제 검증 근거'], ['/changes', '요구사항 변경', '변경·영향·후속 추적']],
    ['설정·자료', ['/ai', 'AI 도구', '로그인·모델·API 키'], ['/setup', '프로젝트 설정', '폴더·사용할 AI·개발 환경'], ['/integrations', '외부 도구 연결', 'MCP·인증·연결 검사'], ['/references', '화면 참고 자료', 'Stitch·Figma 원본 읽기'], ['/wiki', '위키 승인', '재발 방지·테스트 지식 검토'], ['/messengers', '메신저 연결', '슬랙 요청·승인 연결']]
  ];
  const aliases = { '/dashboard': '/agent', '/settings': '/setup' };
  const rawPath = (globalThis.location?.pathname || '').replace(/\/$/, '');
  const currentPath = aliases[rawPath] || rawPath;
  const toggle = make('button', undefined, 'app-menu-toggle'); toggle.id = 'app-menu-toggle'; toggle.type = 'button';
  const toggleIcon = icon('menu'); if (toggleIcon) toggle.append(toggleIcon); else toggle.textContent = '☰';
  toggle.setAttribute('aria-label', '전체 메뉴 열기'); toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', 'app-menu');
  const backdrop = make('div', undefined, 'app-menu-backdrop'); backdrop.hidden = true;
  const panel = make('aside', undefined, 'app-menu'); panel.id = 'app-menu'; panel.setAttribute('aria-label', '전체 메뉴');
  const heading = make('div', undefined, 'app-menu-heading');
  const home = make('div', undefined, 'app-brand');
  if (brand?.logo) { const logo = document.createElement('img'); logo.src = brand.logo; logo.alt = ''; logo.className = 'brand-mark'; home.append(logo); }
  else { const mark = make('span', 'D', 'brand-mark'); mark.setAttribute('aria-hidden', 'true'); home.append(mark); }
  const names = make('span', undefined, 'app-brand-text'); names.append(make('strong', productName), make('small', tagline)); home.append(names);
  const close = make('button', undefined, 'app-menu-close'); close.type = 'button'; close.setAttribute('aria-label', '메뉴 닫기');
  const closeIcon = icon('x'); if (closeIcon) close.append(closeIcon); else close.textContent = '×';
  heading.append(home, close); panel.append(heading);
  const nav = make('nav'); nav.setAttribute('aria-label', '빠른 이동');
  let currentSection = '', currentTitle = '';
  for (const [label, ...links] of sections) {
    nav.append(make('h2', label));
    for (const [href, title, description] of links) {
      const link = make('a'); link.href = href; link.setAttribute('data-tip', title); link.setAttribute('data-tip-pos', 'right');
      const mark = icon(iconNames[href]); if (mark) link.append(mark);
      const text = make('span', undefined, 'app-nav-text'); text.append(make('span', title), make('small', description)); link.append(text);
      if (currentPath === href) { link.setAttribute('aria-current', 'page'); currentSection = label; currentTitle = title; }
      nav.append(link);
    }
  }
  panel.append(nav);
  document.body.classList.add('app-shell');
  // Desktop (≥1024): sidebar or rail, collapsible to give the work area full width. Narrow: modal drawer.
  const desktopQuery = globalThis.matchMedia?.('(min-width:1024px)');
  const isDrawer = () => Boolean(desktopQuery) && !desktopQuery.matches;
  let prior = null;
  function setToggle(expanded) {
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-label', expanded ? '전체 메뉴 닫기' : '전체 메뉴 열기');
  }
  function sync() {
    const drawer = isDrawer();
    panel.setAttribute('role', drawer ? 'dialog' : 'navigation');
    document.body.classList.remove('app-menu-open', 'app-nav-collapsed');
    backdrop.hidden = true;
    if (drawer) { panel.setAttribute('aria-modal', 'true'); panel.hidden = true; setToggle(false); }
    else { panel.removeAttribute?.('aria-modal'); panel.hidden = false; setToggle(true); }
  }
  function open() {
    prior = document.activeElement; panel.hidden = false; setToggle(true);
    if (isDrawer()) { backdrop.hidden = false; document.body.classList.add('app-menu-open'); close.focus?.(); }
    else document.body.classList.remove('app-nav-collapsed');
  }
  function shut() {
    panel.hidden = true; backdrop.hidden = true; setToggle(false);
    if (isDrawer()) { document.body.classList.remove('app-menu-open'); (prior || toggle).focus?.(); }
    else { document.body.classList.add('app-nav-collapsed'); toggle.focus?.(); }
  }
  toggle.onclick = () => panel.hidden ? open() : shut(); close.onclick = shut; backdrop.onclick = shut;
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape' && isDrawer()) { event.preventDefault(); shut(); return; }
    if (event.key === 'Tab' && isDrawer()) {
      const items = [...panel.querySelectorAll('button,a')];
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  desktopQuery?.addEventListener?.('change', sync);
  const header = document.querySelector('body > .app-header');
  if (brand?.name && header?.dataset?.pageTitle) { document.title = header.dataset.pageTitle + ' · ' + productName; }
  document.querySelectorAll?.('[data-brand-name]')?.forEach(el => { el.textContent = productName; });
  if (header) {
    const identity = make('div', undefined, 'app-header-identity');
    const crumbs = make('div', undefined, 'app-crumbs');
    if (currentSection) { crumbs.append(make('span', currentSection, 'app-crumb-section')); const sep = make('span', '/', 'app-crumb-sep'); sep.setAttribute('aria-hidden', 'true'); crumbs.append(sep); }
    crumbs.append(make('span', currentTitle || header.dataset.pageTitle || '개발 작업 공간', 'app-crumb-page'));
    identity.append(toggle, crumbs);
    header.prepend(identity);
  } else {
    toggle.classList.add('app-menu-floating');
    document.body.append(toggle);
  }
  document.body.append(backdrop, panel);
  const main = document.querySelector('body > main');
  if (main) {
    if (!main.id) main.id = 'app-main-content';
    main.tabIndex = -1;
    const skip = make('a', '본문으로 건너뛰기', 'app-skip-link');
    skip.href = '#' + main.id;
    skip.addEventListener('click', event => { event.preventDefault(); main.focus(); main.scrollIntoView({ block: 'start' }); });
    document.body.insertBefore(skip, document.body.firstChild);
  }
  if (header) { const syncHeader = () => document.documentElement.style.setProperty('--app-header-height', header.getBoundingClientRect().height + 'px'); syncHeader(); if (typeof ResizeObserver !== 'undefined') new ResizeObserver(syncHeader).observe(header); window.addEventListener('resize', syncHeader); }
  // The running app keeps its old code after a pull or an upgrade; ask for a restart.
  // A double-clicked app has no terminal to stop it from: offer 앱 종료 in the menu.
  const addQuit = () => {
    if (document.getElementById('app-quit')) return;
    const quit = make('button', undefined, 'app-menu-quit'); quit.id = 'app-quit'; quit.type = 'button'; quit.setAttribute('data-tip', '앱 종료'); quit.setAttribute('data-tip-pos', 'right');
    const quitIcon = icon('x'); if (quitIcon) quit.append(quitIcon); quit.append(make('span', '앱 종료', 'app-nav-text'));
    quit.addEventListener('click', async () => {
      const UI = globalThis.DevRosterUI;
      const ok = UI ? await UI.confirmAction('DevRoster를 종료할까요?', '진행 중인 AI 실행이 있으면 멈춥니다. 다시 쓰려면 앱을 다시 실행하세요.', '종료', { danger: true }) : globalThis.confirm?.('DevRoster를 종료할까요?');
      if (!ok) return;
      try { await fetch('/api/app/quit', { method: 'POST', headers: { 'X-DevRoster-Action': 'quit' } }); } catch { /* The server is already stopping. */ }
      const done = make('main', undefined, 'app-quit-done'); done.append(make('h1', 'DevRoster를 종료했습니다'), make('p', '이 탭을 닫아도 됩니다. 다시 쓰려면 DevRoster 앱을 실행하세요.'));
      document.body.replaceChildren(done);
    });
    panel.append(quit);
  };
  if (typeof fetch === 'function') { fetch('/api/app/status').then(r => r.ok ? r.json() : null).then(status => { if (status?.can_quit) addQuit(); if (!status?.restart_required || document.getElementById('app-restart-notice')) return; const notice = make('div', 'DevRoster 코드가 바뀌었습니다. 앱을 종료하고 다시 실행해야 새 코드가 적용됩니다.', 'app-restart-notice'); notice.id = 'app-restart-notice'; notice.setAttribute('role', 'status'); document.body.insertBefore(notice, document.body.firstChild); }).catch(() => {}); }
  sync();
})();
