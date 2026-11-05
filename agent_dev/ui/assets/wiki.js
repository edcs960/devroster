/* Wiki review: approve, reject and retire knowledge revisions through the server rules. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const node = (tag, value, className) => {
    const el = document.createElement(tag);
    if (value !== undefined) el.textContent = value;
    if (className) el.className = className;
    return el;
  };
  const STATES = {candidate: '승인 대기', active: '운영 중', rejected: '반려', retired: '폐기', superseded: '이전 버전'};
  const VERIFY = {verified: '근거 확인됨', source_changed: '근거 파일이 바뀜 — 개정 필요', resolution_unverified: '해결 근거 없음 — 승인 불가', body_mismatch: '본문이 기록과 다름'};
  let token = '', pages = {}, items = [], generation = 0;
  try { $('wiki-actor').value = localStorage.getItem('devroster.wiki.actor') || ''; } catch (_) { /* storage unavailable */ }

  async function call(path, payload) {
    const options = payload === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Local-Token': token}, body: JSON.stringify(payload)};
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    return data;
  }
  function fail(message) { $('wiki-error').textContent = message; $('wiki-error').hidden = false; }

  async function load() {
    $('wiki-loading').hidden = false; $('wiki-error').hidden = true; $('wiki-retry').hidden = true;
    try {
      const data = await call('/api/wiki/bootstrap');
      token = data.token; pages = data.pages || {};
      if (!data.projects.length) { $('wiki-empty').hidden = false; return; }
      const select = $('wiki-project'); select.replaceChildren();
      for (const project of data.projects) { const option = node('option', project.id + ' · ' + project.name); option.value = project.id; select.append(option); }
      select.value = data.projects.some(p => p.id === data.workspace_project) ? data.workspace_project : data.projects[0].id;
      $('wiki-section').hidden = false;
      await refresh();
    } catch (error) { fail(error.message); $('wiki-retry').hidden = false; }
    finally { $('wiki-loading').hidden = true; }
  }

  async function refresh() {
    const mine = ++generation;
    const data = await call('/api/wiki/items?project_id=' + encodeURIComponent($('wiki-project').value));
    if (mine !== generation) return;
    items = data.items; render();
  }

  function render() {
    const state = $('wiki-state').value, list = $('wiki-list');
    const shown = items.filter(item => state ? item.state === state : item.state !== 'superseded');
    list.replaceChildren(...shown.map(card));
    $('wiki-list-empty').hidden = shown.length > 0;
    const waiting = items.filter(item => item.state === 'candidate').length;
    $('wiki-state').options[0].textContent = '승인 대기 (' + waiting + ')';
  }

  function card(item) {
    const box = node('article', undefined, 'wiki-card'); box.dataset.state = item.state; box.setAttribute('role', 'listitem');
    box.append(node('h2', item.item_id + ' r' + item.revision + ' · ' + item.content.title));
    const meta = node('p', undefined, 'wiki-meta');
    meta.append(node('span', STATES[item.state] || item.state, 'wiki-badge'), node('span', pages[item.page] || item.page, 'wiki-badge'));
    const verify = item.verification?.state;
    if (verify) meta.append(node('span', VERIFY[verify] || verify, 'wiki-badge' + (verify === 'verified' ? '' : ' warn')));
    box.append(meta);
    const facts = node('dl');
    const fact = (label, value) => { if (!value) return; facts.append(node('dt', label), node('dd', value)); };
    fact('확인할 것', item.content.check);
    fact('적용 조건', item.content.condition);
    fact('근거 파일', (item.sources || []).map(s => s.path).join(', '));
    if (item.approval?.actor) fact('처리', item.approval.actor + (item.approval.retired_reason ? ' · 폐기 이유: ' + item.approval.retired_reason : ''));
    box.append(facts);
    const actions = node('div', undefined, 'wiki-actions');
    if (item.state === 'candidate') {
      actions.append(button('승인', 'approve', item), button('반려', 'reject', item, 'secondary'));
    } else if (item.state === 'active') {
      const label = node('label', '폐기 이유'), reason = node('input'); reason.maxLength = 300; label.append(reason);
      actions.append(label, button('폐기', 'retire', item, 'secondary', () => reason.value));
    }
    if (actions.children.length) box.append(actions);
    return box;
  }

  function button(text, action, item, className, reason) {
    const control = node('button', text, className); control.type = 'button';
    control.addEventListener('click', async () => {
      const actor = $('wiki-actor').value.trim();
      if (!actor) { $('wiki-result').textContent = '처리자 이름을 입력해주세요.'; $('wiki-actor').focus(); return; }
      try { localStorage.setItem('devroster.wiki.actor', actor); } catch (_) { /* storage unavailable */ }
      const payload = {project_id: $('wiki-project').value, item_id: item.item_id, revision: item.revision, actor};
      if (reason) payload.reason = reason();
      control.disabled = true; $('wiki-result').textContent = text + ' 처리 중…';
      try {
        const result = await call('/api/wiki/' + action, payload);
        let message = item.item_id + ' r' + item.revision + ': ' + (STATES[result.state] || result.state) + '(으)로 바꿨습니다.';
        if (result.rendered) message += ' 열람본 ' + result.rendered.directory + ' 갱신.';
        if (result.render_error) message += ' 열람본 갱신 실패: ' + result.render_error;
        $('wiki-result').textContent = message;
        await refresh();
      } catch (error) { $('wiki-result').textContent = text + ' 실패: ' + error.message; control.disabled = false; }
    });
    return control;
  }

  $('wiki-project').addEventListener('change', () => refresh().catch(error => fail(error.message)));
  $('wiki-state').addEventListener('change', render);
  $('wiki-retry').addEventListener('click', load);
  load();
})();
