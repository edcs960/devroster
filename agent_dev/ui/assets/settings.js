(() => {
  'use strict';
  // Readable failure text: network/parse errors become a Korean hint, server messages pass through.
  const errText = error => globalThis.DevRosterUI ? globalThis.DevRosterUI.errorMessage(error) : (error?.message || String(error));
  // Shared confirmation dialog (ui-feedback.js); falls back to the browser dialog if it is missing.
  const ask = (title, body, confirm, opts = {}) => globalThis.DevRosterUI ? globalThis.DevRosterUI.confirmAction(title, body, confirm, opts) : Promise.resolve(window.confirm(title + '\n' + body));
  // Shared settings screen. Every action calls the local settings API; rules live in the services.
  const $ = id => document.getElementById(id);
  const LIMITS = ['max_attempts', 'max_runner_seconds', 'phase_timeout_seconds', 'max_reentries', 'session_timeout_seconds'];
  const LABELS = {
    'ai.provider': '기본 AI 도구', 'ai.model': '모델', 'ai.effort': '추론 강도',
    'limits.max_attempts': '최대 시도', 'limits.max_runner_seconds': '실행 최대 초',
    'limits.phase_timeout_seconds': '단계 제한 초', 'limits.max_reentries': '재진입 횟수',
    'limits.session_timeout_seconds': '세션 제한 초'
  };
  const ORIGINS = {global: '공통값', project: '프로젝트 지정', default: '기본값'};
  const state = {token: '', shared: null, effective: null, root: '', plan: null};
  const make = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; };
  const notice = (text, error = false) => { $('notice').textContent = text; $('notice').classList.toggle('error', error); };
  const list = value => value.split(',').map(item => item.trim()).filter(Boolean);

  async function api(path, payload) {
    const options = payload === undefined ? {} : {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Setup-Token': state.token},
      body: JSON.stringify(payload)
    };
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({error: '응답을 읽지 못했습니다.'}));
    if (!response.ok) {
      const error = new Error(response.status === 409 ? '다른 화면에서 바뀌었습니다. 다시 불러온 뒤 시도해주세요. (' + (data.error || '') + ')' : (data.error || '요청을 처리하지 못했습니다.'));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function report(id, promise, success) {
    $(id).textContent = '처리 중…';
    $(id).classList.remove('error');
    return promise.then(result => { $(id).textContent = success(result); return result; })
      .catch(error => { $(id).textContent = errText(error); $(id).classList.add('error'); throw error; });
  }

  function renderLimits() {
    const box = $('global-limits');
    box.replaceChildren();
    const values = state.shared.settings.values;
    for (const key of LIMITS) {
      for (const [group, suffix] of [['limits', ''], ['limit_caps', ' 상한']]) {
        const wrap = make('div');
        const id = 'global-' + group + '-' + key;
        const label = make('label', LABELS['limits.' + key] + suffix);
        label.htmlFor = id;
        const input = make('input');
        input.id = id; input.type = 'number'; input.min = '0'; input.dataset.group = group; input.dataset.key = key;
        const current = (values[group] || {})[key];
        input.value = current === undefined ? '' : String(current);
        wrap.append(label, input);
        box.append(wrap);
      }
    }
  }

  function renderGlobal() {
    const values = state.shared.settings.values;
    const ai = values.ai || {};
    $('global-provider').value = ai.provider || '';
    $('global-model').value = ai.model || '';
    $('global-model-empty').checked = ai.model === '';
    $('global-effort').value = ai.effort || '';
    renderLimits();
    const revision = state.shared.settings.revision;
    $('global-revision').textContent = revision ? '적용 revision ' + revision : '공통 설정 없음';
  }

  function globalValues() {
    const ai = {};
    if ($('global-provider').value) ai.provider = $('global-provider').value;
    if ($('global-model').value.trim() || $('global-model-empty').checked) ai.model = $('global-model').value.trim();
    if ($('global-effort').value.trim()) ai.effort = $('global-effort').value.trim();
    const values = {};
    if (Object.keys(ai).length) values.ai = ai;
    for (const input of $('global-limits').querySelectorAll('input')) {
      if (input.value === '') continue;
      (values[input.dataset.group] ||= {})[input.dataset.key] = Number(input.value);
    }
    const templates = state.shared.settings.values.templates;
    if (templates) values.templates = templates;
    return values;
  }

  // Override choices the user made but has not applied yet, by setting path.
  let pendingEdits = {};
  function renderEffective() {
    const effective = state.effective;
    const body = $('effective-table').querySelector('tbody');
    body.replaceChildren();
    for (const [path, origin] of Object.entries(effective.origins)) {
      const [group, key] = path.split('.');
      const row = make('tr');
      const head = make('th', LABELS[path] || path); head.scope = 'row';
      const value = effective.values[group][key];
      const override = (effective.overrides[group] || {})[key];
      const control = make('td');
      const mode = make('select');
      mode.setAttribute('aria-label', (LABELS[path] || path) + ' 지정 방식');
      for (const [option, text] of [['inherit', '공통값 사용'], ['project', '직접 지정']]) {
        const item = make('option', text); item.value = option; mode.append(item);
      }
      const input = make('input');
      input.setAttribute('aria-label', (LABELS[path] || path) + ' 프로젝트 값');
      input.dataset.path = path;
      input.type = group === 'limits' ? 'number' : 'text';
      mode.value = override === undefined ? 'inherit' : 'project';
      input.value = override === undefined ? '' : String(override);
      // A reload (e.g. after saving shared defaults) must not discard choices not yet applied.
      const edit = pendingEdits[path];
      if (edit) { mode.value = edit.mode; input.value = edit.value; }
      input.disabled = mode.value === 'inherit';
      const remember = () => { pendingEdits[path] = {mode: mode.value, value: input.value}; };
      mode.addEventListener('change', () => { input.disabled = mode.value === 'inherit'; remember(); if (!input.disabled) input.focus(); });
      input.addEventListener('input', remember);
      control.append(mode, input);
      row.append(head, make('td', value === '' ? '(CLI 기본값)' : String(value)), make('td', ORIGINS[origin] || origin, 'origin-' + origin), control);
      body.append(row);
    }
    $('pending-global').hidden = !effective.pending_global;
    const applied = effective.applied ? '반영된 공통 revision ' + effective.applied.global_revision : '공통값 반영 기록 없음';
    $('effective-revision').textContent = '프로젝트 설정 revision ' + effective.project_revision + ' · ' + applied + ' · 최신 공통 revision ' + effective.global_revision;
    $('effective-project').textContent = '대상 프로젝트: ' + state.root;
    $('apply-global').disabled = !effective.global_revision;
  }

  function overrides() {
    const result = {};
    for (const input of $('effective-table').querySelectorAll('input[data-path]')) {
      if (input.disabled) continue;
      const [group, key] = input.dataset.path.split('.');
      (result[group] ||= {})[key] = group === 'limits' ? Number(input.value) : input.value.trim();
    }
    return result;
  }

  function renderConnections() {
    const box = $('connection-list');
    const select = $('bind-connection');
    box.replaceChildren(); select.replaceChildren();
    if (!state.shared.connections.length) box.append(make('li', '등록한 공통 연결이 없습니다.', 'hint'));
    for (const row of state.shared.connections) {
      const item = make('li');
      const scopes = (row.definition.scopes || []).join(', ') || '없음';
      const operations = Object.keys(row.definition.operations || {}).join(', ');
      item.append(make('strong', row.connection_id + ' · v' + row.version), make('span', ' ' + (row.enabled ? '사용 가능' : '비활성화됨'), row.enabled ? 'badge pass' : 'badge fail'), make('p', '제공자 ' + row.definition.provider + ' · 자료 범위 ' + scopes + ' · 도구 ' + operations, 'hint'));
      if (row.enabled) {
        const button = make('button', '비활성화', 'secondary'); button.type = 'button';
        button.addEventListener('click', async () => {
          if (!(await ask(row.connection_id + ' 연결을 비활성화할까요?', '모든 프로젝트에서 이 연결을 즉시 사용할 수 없게 됩니다.', '연결 비활성화'))) return;
          report('connection-status', api('/api/settings/connections/disable', {connection_id: row.connection_id, expected_revision: row.state_revision}), () => '비활성화했습니다.').then(load).catch(() => {});
        });
        item.append(button);
        const option = make('option', row.connection_id + ' (v' + row.version + ')'); option.value = row.connection_id; option.dataset.version = row.version;
        select.append(option);
      }
      box.append(item);
    }
  }

  function renderLocation() {
    const location = state.shared.location;
    const box = $('location-details');
    box.replaceChildren();
    const sources = {environment: 'DEVROSTER_HOME 환경변수', bootstrap: '선택한 관리 폴더', default: '기본 앱 위치'};
    const rows = [
      ['관리 폴더', location.management_root + ' (' + (sources[location.management_source] || location.management_source) + ')'],
      ['레이아웃', location.workspace.layout], ['소스 폴더', location.workspace.source_root],
      ['설정 위치', location.workspace.config_root], ['작업 DB', location.workspace.state_db],
      ['작업 공간(worktree)', location.workspace.worktree_root]
    ];
    for (const [term, value] of rows) box.append(make('dt', term), make('dd', value));
  }

  function renderPlan(plan) {
    state.plan = plan;
    $('move-result').hidden = false;
    const box = $('move-checks');
    box.replaceChildren();
    for (const check of plan.checks) {
      const item = make('li');
      item.append(make('span', check.status === 'pass' ? '확인' : '조치 필요', 'badge ' + check.status), make('span', ' ' + check.message));
      if (check.status !== 'pass' && check.fix) item.append(make('p', check.fix, 'hint'));
      box.append(item);
    }
    $('move-apply').disabled = !plan.ready;
    $('recover-id').value = plan.plan_id;
  }

  async function load() {
    state.shared = await api('/api/settings/global');
    state.token = state.shared.token;
    const root = state.shared.location.workspace.source_root;
    if (root !== state.root) pendingEdits = {};  // edits belong to the project they were made for
    state.root = root;
    state.effective = await api('/api/settings/effective?root=' + encodeURIComponent(state.root));
    renderGlobal(); renderEffective(); renderConnections(); renderLocation();
    notice('공통 설정을 불러왔습니다.');
  }

  $('global-form').addEventListener('submit', event => {
    event.preventDefault();
    report('global-status', api('/api/settings/global', {values: globalValues(), expected_revision: state.shared.settings.revision}), result => '저장했습니다. 공통 revision ' + result.revision + '. 프로젝트에는 ‘반영’할 때만 적용됩니다.').then(load).catch(() => {});
  });
  $('apply-global').addEventListener('click', () => {
    report('effective-status', api('/api/settings/apply-global', {root: state.root, global_revision: state.effective.global_revision, expected_revision: state.effective.project_revision, overrides: overrides(), templates: $('apply-templates').checked}), result => '새 프로젝트 프로필 ' + result.profile_version + '을(를) 만들었습니다. 기존 승인 작업은 그대로입니다.').then(() => { pendingEdits = {}; return load(); }).catch(() => {});
  });
  $('connection-form').addEventListener('submit', event => {
    event.preventDefault();
    const config = {provider: $('connection-provider').value.trim(), scopes: list($('connection-scopes').value)};
    if ($('connection-credential').value.trim()) config.credential_ref = $('connection-credential').value.trim();
    report('connection-status', api('/api/settings/connections', {connection_id: $('connection-id').value.trim(), config, expected_version: 0}), row => row.connection_id + ' v' + row.version + '을(를) 등록했습니다.').then(load).catch(() => {});
  });
  $('bind-form').addEventListener('submit', event => {
    event.preventDefault();
    const option = $('bind-connection').selectedOptions[0];
    if (!option) { $('connection-status').textContent = '사용 가능한 공통 연결을 먼저 등록해주세요.'; return; }
    report('connection-status', api('/api/settings/connections/bind', {root: state.root, local_id: $('bind-local').value.trim(), connection_id: option.value, version: Number(option.dataset.version), scopes: list($('bind-scopes').value), operations: list($('bind-operations').value), expected_version: Number($('bind-expected').value || 0), credential_ref: $('bind-credential').value.trim()}), row => row.id + ' 연결을 지정했습니다. 외부 도구 연결 화면에서 실제 읽기를 검사해주세요.').then(load).catch(() => {});
  });
  $('move-form').addEventListener('submit', event => {
    event.preventDefault();
    report('move-status', api('/api/settings/storage/plan', {source: state.root, destination: $('move-destination').value.trim(), scope: $('move-scope').value}), plan => plan.ready ? '점검을 통과했습니다. 내용을 확인한 뒤 실행해주세요.' : '조치가 필요한 항목이 있습니다.').then(renderPlan).catch(() => {});
  });
  $('move-apply').addEventListener('click', () => {
    if (!state.plan) return;
    $('move-apply').disabled = true;
    report('move-status', api('/api/settings/storage/apply', {plan_id: state.plan.plan_id, expected_registry_revision: state.plan.registry_revision}), result => '이전 ' + result.state + '. 원본은 남아 있습니다. 웹·CLI·앱을 다시 열어주세요.').then(load).catch(() => {});
  });
  $('recover-run').addEventListener('click', () => {
    report('move-status', api('/api/settings/storage/recover', {migration_id: $('recover-id').value.trim(), action: $('recover-action').value}), result => '복구 결과: ' + result.state).then(load).catch(() => {});
  });
  load().catch(error => notice(errText(error), true));
})();
