(() => {
  'use strict';
  // Readable failure text: network/parse errors become a Korean hint, server messages pass through.
  const errText = error => globalThis.DevRosterUI ? globalThis.DevRosterUI.errorMessage(error) : (error?.message || String(error));
  // Shared confirmation dialog (ui-feedback.js); falls back to the browser dialog if it is missing.
  const ask = (title, body, confirm, opts = {}) => globalThis.DevRosterUI ? globalThis.DevRosterUI.confirmAction(title, body, confirm, opts) : Promise.resolve(window.confirm(title + '\n' + body));
  // Shared AI tools screen. Rules live in the services; this page only edits and calls the API.
  const $ = id => document.getElementById(id);
  const state = {token: '', data: null, dirty: false};
  const make = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; };
  const notice = (text, error = false) => { $('notice').textContent = text; $('notice').classList.toggle('error', error); };
  const EFFORTS = provider => provider === 'gemini_cli' ? [] : provider === 'gemini_api' || provider === 'xai_api' ? ['low', 'medium', 'high'] : provider === 'anthropic_api' ? ['low', 'medium', 'high', 'max'] : ['low', 'medium', 'high', 'xhigh', 'max'];
  const loginLabel = row => !row ? '로그인 상태 미확인' : row.authenticated ? '로그인됨' : '로그인 필요';

  async function api(path, payload) {
    const options = payload === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Setup-Token': state.token}, body: JSON.stringify(payload)};
    const response = await fetch('/api/settings/ai' + path, options);
    const data = await response.json().catch(() => ({error: '응답을 읽지 못했습니다.'}));
    if (!response.ok) throw new Error(response.status === 409 ? '다른 화면에서 AI 도구 설정이 바뀌었습니다. 새로고침한 뒤 다시 저장해주세요.' : (data.error || '요청을 처리하지 못했습니다.'));
    return data;
  }

  function field(parent, label, value, name, options) {
    const id = 'f-' + Math.random().toString(36).slice(2), wrap = make('div'), lab = make('label', label);
    lab.htmlFor = id;
    let input;
    if (options) { input = make('select'); for (const [v, text] of options) { const o = make('option', text); o.value = v; input.append(o); } }
    else { input = make('input'); input.type = typeof value === 'number' ? 'number' : 'text'; }
    input.id = id; input.value = value ?? ''; input.dataset.field = name;
    input.addEventListener('input', () => { state.dirty = true; });
    wrap.append(lab, input); parent.append(wrap);
    return input;
  }

  function loginBox(provider) {
    const box = make('div', undefined, 'cli-login'), status = make('p', loginLabel(state.data.auth[provider]), 'hint'), start = make('button', 'DevRoster 전용 로그인', 'secondary'), cancel = make('button', '로그인 취소', 'secondary'), out = make('pre');
    status.setAttribute('role', 'status'); status.dataset.loginStatus = provider; start.type = cancel.type = 'button'; cancel.hidden = out.hidden = true;
    let job = '';
    const show = data => {
      out.hidden = !data.output; out.textContent = data.output || ''; cancel.hidden = data.done; start.disabled = !data.done;
      if (data.done) { status.textContent = loginLabel({authenticated: data.logged_in}); notice(data.logged_in ? '로그인을 완료했습니다.' : '로그인이 완료되지 않았습니다. 출력 내용을 확인해주세요.', !data.logged_in); }
    };
    const poll = async () => { try { const data = await api('/login-status', {job_id: job}); show(data); if (!data.done) setTimeout(poll, 2000); } catch (e) { notice(errText(e), true); start.disabled = false; } };
    start.addEventListener('click', async () => {
      if (state.dirty) { notice('실행파일을 바꿨다면 먼저 저장한 뒤 로그인해주세요.', true); return; }
      try { start.disabled = true; status.textContent = '로그인 진행 중: 안내에 따라 브라우저에서 로그인하세요.'; const data = await api('/login', {provider}); job = data.job_id; show(data); if (!data.done) setTimeout(poll, 1000); }
      catch (e) { notice(errText(e), true); start.disabled = false; }
    });
    cancel.addEventListener('click', async () => { try { show(await api('/login-cancel', {job_id: job})); } catch (e) { notice(errText(e), true); } });
    box.append(status, start, cancel, out);
    return box;
  }

  function toolCard(provider) {
    const info = state.data.providers[provider], row = state.data.values.tools[provider], isApi = info.kind === 'api';
    const card = make('details', undefined, 'card'); card.dataset.provider = provider; card.open = row.enabled;
    const summary = make('summary', info.label);
    if (!isApi) { const badge = make('span', ' · ' + loginLabel(state.data.auth[provider]), 'hint'); badge.dataset.loginStatus = provider; summary.append(badge); }
    card.append(summary);
    const use = make('label', undefined, 'check'), check = make('input'); check.type = 'checkbox'; check.checked = row.enabled; check.dataset.field = 'enabled';
    check.addEventListener('change', () => { state.dirty = true; renderOrder(); }); use.append(check, document.createTextNode(' 이 도구 사용'));
    card.append(use, make('p', isApi ? 'API 생성 · 기획·명세·목업과 텍스트 단계' : '개발 CLI · 파일 읽기·수정과 기획 생성', 'hint'));
    const grid = make('div', undefined, 'columns');
    if (isApi) {
      field(grid, 'API 주소', row.base_url, 'base_url');
      field(grid, '인증 키 이름', row.credential_ref, 'credential_ref');
      field(grid, '출력 토큰 상한', row.max_output_tokens, 'max_output_tokens');
      field(grid, '추론 옵션', row.reasoning, 'reasoning', [['native', '모델이 지원하는 추론 옵션 전달'], ['none', '추론 옵션 전달하지 않음']]);
    } else {
      field(grid, '실행파일 (이름 또는 절대 경로)', row.command, 'command');
    }
    field(grid, isApi ? '고정 모델 (자동 선택이 아니면 필수)' : '고정 모델 (비우면 CLI 기본 모델)', row.model, 'model');
    field(grid, '고정 추론 강도', row.effort || '', 'effort', [['', '도구 기본값'], ...EFFORTS(provider).map(v => [v, v])]);
    card.append(grid);
    if (row.routing?.mode === 'auto') card.append(make('p', '작업별 자동 선택이 켜져 있습니다. 자동 선택 모델 구성은 그대로 유지됩니다.', 'hint'));
    if (!isApi && ['codex_cli', 'claude_cli'].includes(provider)) card.append(loginBox(provider));
    else if (!isApi) card.append(make('p', '로그인: ' + info.login, 'hint'));
    return card;
  }

  function collectTools() {
    const tools = {};
    for (const card of $('tool-cards').children) {
      const provider = card.dataset.provider, base = state.data.values.tools[provider], row = {...base};
      for (const input of card.querySelectorAll('[data-field]')) {
        const name = input.dataset.field;
        row[name] = input.type === 'checkbox' ? input.checked : name === 'max_output_tokens' ? Number(input.value) : input.value.trim();
      }
      tools[provider] = row;
    }
    return tools;
  }

  function renderOrder() {
    const enabled = new Set([...$('tool-cards').querySelectorAll('[data-field=enabled]')].filter(c => c.checked).map(c => c.closest('[data-provider]').dataset.provider));
    $('order-list').replaceChildren();
    state.order.forEach((provider, index) => {
      const li = make('li'); li.dataset.provider = provider;
      const name = make('span', `${index + 1}. ${state.data.providers[provider].label}` + (enabled.has(provider) ? '' : ' (꺼짐)'), 'role-name');
      const actions = make('span', undefined, 'role-actions');
      for (const [label, delta] of [['위로', -1], ['아래로', 1]]) {
        const b = make('button', label, 'secondary'); b.type = 'button'; b.disabled = index + delta < 0 || index + delta >= state.order.length;
        b.setAttribute('aria-label', `${state.data.providers[provider].label} ${label}`);
        b.addEventListener('click', () => { const next = [...state.order]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; state.order = next; state.dirty = true; renderOrder(); $('order-list').querySelector(`[data-provider="${provider}"] button`)?.focus(); });
        actions.append(b);
      }
      li.append(name, actions); $('order-list').append(li);
    });
  }

  function renderKeys() {
    $('key-rows').replaceChildren();
    const sources = state.data.key_sources || {};
    for (const [name, set] of Object.entries(state.data.keys)) {
      const source = sources[name], kept = set || !!source;
      const where = source ? `환경변수 ${source.env}에서 읽음 · ${source.present ? '설정됨' : '지금 설정되지 않음'}` : (set ? '저장됨' : '없음');
      const row = make('div', undefined, 'path-row'), label = make('label', `${name} · ${where}`), input = make('input'), save = make('button', kept ? '교체' : '저장', 'secondary'), remove = make('button', '삭제', 'secondary');
      const id = 'key-' + name; label.htmlFor = id; input.id = id; input.type = 'password'; input.autocomplete = 'off'; input.placeholder = kept ? '새 값 또는 env:환경변수이름으로 교체' : '키 값 또는 env:환경변수이름';
      save.type = remove.type = 'button'; remove.disabled = !kept;
      save.addEventListener('click', async () => { if (!input.value) { $('key-status').textContent = '키 값을 입력해주세요.'; return; } try { const reference = input.value.startsWith('env:'); state.data = Object.assign(state.data, await api('/key', {name, value: input.value})); input.value = ''; renderKeys(); $('key-status').textContent = name + (reference ? ' 키를 환경변수 이름으로 저장했습니다. 값은 저장하지 않습니다.' : ' 키를 저장했습니다.'); return; $('key-status').textContent = name + ' 키를 저장했습니다.'; } catch (e) { $('key-status').textContent = errText(e); } });
      remove.addEventListener('click', async () => { if (!(await ask(name + ' 키를 삭제할까요?', '이 키를 쓰는 모든 프로젝트의 API 실행이 다음 실행부터 인증에 실패합니다. 되돌릴 수 없습니다.', '키 삭제'))) return; try { state.data = Object.assign(state.data, await api('/key', {name, delete: true})); renderKeys(); $('key-status').textContent = name + ' 키를 삭제했습니다.'; } catch (e) { $('key-status').textContent = errText(e); } });
      const wrap = make('div'); wrap.append(label, row); row.append(input, save, remove); $('key-rows').append(wrap);
    }
  }

  function render(data) {
    state.data = data; state.dirty = false; state.order = [...data.values.order];
    $('tool-cards').replaceChildren(...data.values.order.map(toolCard));
    renderOrder(); renderKeys();
    $('revision').textContent = data.revision ? `AI 도구 설정 r${data.revision}` : '아직 저장하지 않음';
    $('project-list').replaceChildren(...(data.projects.length ? data.projects.map(p => make('li', `${p.name} · ${p.root}`)) : [make('li', '아직 공통 AI 도구를 쓰는 프로젝트가 없습니다. 프로젝트 설정의 AI 도구 단계에서 전환할 수 있습니다.', 'hint')]));
  }

  async function load() {
    try { const data = await api(''); state.token = data.token; render(data); notice(data.revision ? '도구를 켜고 모델·강도를 확인한 뒤 저장해주세요.' : '공통 AI 도구가 아직 없습니다. 사용할 도구를 켜고 저장하거나, 프로젝트 설정에서 기존 설정을 가져오세요.'); }
    catch (e) { notice(errText(e), true); }
  }

  $('ai-form').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      $('save').disabled = true;
      const data = await api('', {values: {tools: collectTools(), order: state.order}, expected_revision: state.data.revision});
      render(data);
      notice(`저장했습니다 (r${data.revision}). 다음에 승인하는 작업부터 적용됩니다` + (data.projects.length ? ` · 사용 중인 프로젝트 ${data.projects.length}개.` : '.'));
    } catch (e) { notice(errText(e), true); } finally { $('save').disabled = false; }
  });
  $('auth-check').addEventListener('click', async () => {
    try { $('auth-check').disabled = true; const data = await api('/auth-check', {}); state.data.auth = data.auth; for (const el of document.querySelectorAll('[data-login-status]')) { const row = data.auth[el.dataset.loginStatus]; el.textContent = (el.tagName === 'SPAN' ? ' · ' : '') + loginLabel(row); } notice('로그인 상태를 확인했습니다.'); }
    catch (e) { notice(errText(e), true); } finally { $('auth-check').disabled = false; }
  });
  window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
  load();
})();
