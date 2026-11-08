/* Code review: every response is bound to the task generation that requested it. */
(() => {
  'use strict';
// Readable failure text: network/parse errors become a Korean hint, server messages pass through.
  const errText = error => globalThis.DevRosterUI ? globalThis.DevRosterUI.errorMessage(error) : (error?.message || String(error));
  const $ = id => document.getElementById(id);
  const stale = Symbol('superseded');
  const statusLabels = {A: '추가', M: '수정', D: '삭제', R: '이름 변경', C: '복사', T: '형식 변경'};
  const stateLabels = {open: '처리 필요', resolved: '처리됨', stale: '코드 변경됨'};
  let token = '', task = '', generation = 0, data = null, selected = null, anchor = null;
  const node = (tag, value, className) => {
    const el = document.createElement(tag);
    if (value !== undefined) el.textContent = value;
    if (className) el.className = className;
    return el;
  };
  async function request(path, payload, g = generation) {
    const options = payload === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Local-Token': token},
      body: JSON.stringify(Object.assign({task}, payload)),
    };
    const response = await fetch('/api/review/' + path, options);
    const body = await response.json();
    if (g !== generation) throw stale;
    if (!response.ok) {
      const error = new Error(body.error || response.statusText);
      error.status = response.status;
      throw error;
    }
    return body;
  }
  function notice(text) { $('review-notice').textContent = text; $('review-notice').hidden = !text; }
  function failure(error, form) {
    if (error === stale) return;
    const text = (error.status === 409 ? '내용이 바뀌었습니다. 새로고침 후 다시 확인해주세요. ' : '') + errText(error);
    $('review-error').textContent = text;
    $('review-error').hidden = false;
    if (form) {
      // Show the reason next to the form the user just submitted, not only at the page top.
      let box = form.querySelector('.form-error');
      if (!box) { box = document.createElement('p'); box.className = 'form-error'; box.setAttribute('role', 'alert'); box.tabIndex = -1; form.append(box); }
      box.textContent = text; box.hidden = false; box.focus();
    }
  }
  function cleared(form) { const box = form && form.querySelector('.form-error'); if (box) box.hidden = true; }
  function renderFiles() {
    const list = $('file-list');
    list.replaceChildren();
    let count = 0;
    for (const module of data.modules) {
      const hidden = module.hidden || {};
      const hiddenText = [hidden.out_of_scope && `허용 범위 밖 ${hidden.out_of_scope}개`, hidden.private && `비공개·링크 ${hidden.private}개`, hidden.unsupported && `표시할 수 없는 이름 ${hidden.unsupported}개`].filter(Boolean).join(' · ');
      $('files-hidden').textContent = hiddenText ? '표시하지 않음: ' + hiddenText : '';
      for (const [stage, label] of Object.entries(data.stages)) {
        const files = module.files.filter(file => file.stage === stage);
        if (!files.length) continue;
        const group = node('div', undefined, 'file-group');
        group.append(node('h3', `${module.id} · ${label} ${files.length}개`));
        for (const file of files) {
          count += 1;
          const button = node('button', undefined, 'file-button');
          button.type = 'button';
          const current = selected && selected.module === module.id && selected.stage === stage && selected.path === file.path;
          button.setAttribute('aria-current', String(Boolean(current)));
          button.append(node('span', file.status, 'file-status'), node('span', file.old_path ? `${file.old_path} → ${file.path}` : file.path));
          button.title = statusLabels[file.status] || file.status;
          button.addEventListener('click', () => openFile(module.id, stage, file.path));
          group.append(button);
        }
        list.append(group);
      }
    }
    $('files-summary').textContent = count ? `${count}개 파일${data.complete ? '' : ' · 일부만 표시(검토 한도 초과)'}` : '등록된 허용 경로 안에 변경된 파일이 없습니다.';
  }
  function threadsFor(file) {
    return data.threads.filter(t => t.anchor.module === file.module && t.anchor.stage === file.stage && t.anchor.path === file.path);
  }
  async function openFile(module, stage, path) {
    const g = generation;
    selected = {module, stage, path};
    anchor = null;
    $('comment-form').hidden = true;
    renderFiles();
    $('diff-title').textContent = path;
    $('diff-meta').textContent = '불러오는 중…';
    $('diff-body').replaceChildren();
    try {
      const params = new URLSearchParams({task, module, stage, path});
      const diff = await request('diff?' + params, undefined, g);
      if (diff.review_digest !== data.review_digest) notice('목록을 불러온 뒤 변경 내용이 바뀌었습니다. 새로고침해주세요.');
      $('diff-meta').textContent = [data.stages[stage], statusLabels[diff.status] || diff.status, diff.binary ? '바이너리 파일 · 내용 생략' : '', diff.truncated ? '크기 한도로 일부만 표시' : ''].filter(Boolean).join(' · ');
      const lines = new Set(threadsFor(diff).map(t => t.anchor.side + ':' + t.anchor.line));
      if (!diff.hunks.length) $('diff-body').append(node('p', diff.binary ? '바이너리 변경은 내용을 표시하지 않습니다.' : '표시할 줄 변경이 없습니다.', 'review-meta'));
      for (const hunk of diff.hunks) {
        const block = node('div', undefined, 'diff-hunk');
        block.append(node('div', hunk.header, 'diff-hunk-header'));
        for (const line of hunk.lines) {
          const side = line.kind === 'del' ? 'old' : 'new';
          const number = side === 'old' ? line.old : line.new;
          const row = node('button', undefined, 'diff-line ' + line.kind + (lines.has(side + ':' + number) ? ' has-thread' : ''));
          row.type = 'button';
          row.setAttribute('aria-label', `${side === 'old' ? '이전' : '현재'} ${number}번 줄에 코멘트: ${line.text}`);
          row.append(node('span', line.old ?? '', 'num'), node('span', line.new ?? '', 'num'), node('span', {add: '+', del: '-', ctx: ' '}[line.kind]), node('span', line.text));
          row.addEventListener('click', () => startComment(diff, side, number));
          block.append(row);
        }
        $('diff-body').append(block);
      }
    } catch (error) {
      $('diff-meta').textContent = '';
      failure(error);
    }
  }
  function startComment(diff, side, line) {
    anchor = {module: diff.module, stage: diff.stage, path: diff.path, side, line};
    $('comment-anchor').textContent = `${diff.path} · ${side === 'old' ? '이전' : '현재'} ${line}번 줄`;
    $('comment-form').hidden = false;
    $('comment-body').focus();
  }
  function renderThreads() {
    const open = data.threads.filter(t => t.state === 'open').length;
    $('thread-count').textContent = `처리 필요 ${open}개 / 전체 ${data.threads.length}개`;
    threadList($('threads'), data.threads, '아직 코멘트가 없습니다. 변경 줄을 눌러 남길 수 있습니다.', thread => {
      const where = node('button', `${thread.anchor.path}:${thread.anchor.line}`, 'text-link');
      where.type = 'button';
      where.addEventListener('click', () => openFile(thread.anchor.module, thread.anchor.stage, thread.anchor.path));
      return where;
    });
  }
  function threadList(box, threads, empty, locate) {
    box.replaceChildren();
    if (!threads.length) box.append(node('p', empty, 'review-meta'));
    for (const thread of threads) {
      const item = node('article', undefined, 'thread');
      item.dataset.state = thread.display_state;
      const where = locate(thread);
      item.append(node('span', stateLabels[thread.display_state] || thread.display_state, 'badge ' + (thread.display_state === 'resolved' ? 'pass' : 'warn')), ' ', where, node('p', thread.body, 'thread-body'));
      if (thread.resolution) item.append(node('p', '처리: ' + thread.resolution, 'review-meta'));
      if (thread.display_state === 'stale') item.append(node('p', thread.anchor.capture_id ? '코멘트 이후 화면이나 소스가 바뀌었습니다. 다시 캡처해 확인하세요.' : '코멘트 이후 이 파일이 바뀌었습니다. 이전 위치 기준으로 확인하세요.', 'review-meta'));
      const form = node('form', undefined, 'form-actions');
      const input = node('input');
      input.placeholder = thread.state === 'open' ? '처리 내용' : '다시 열 이유 (선택)';
      input.setAttribute('aria-label', '코멘트 처리 내용');
      input.maxLength = 4000;
      const submit = node('button', thread.state === 'open' ? '처리 완료' : '다시 열기', 'secondary');
      submit.type = 'submit';
      form.append(input, submit);
      form.addEventListener('submit', async event => {
        event.preventDefault();
        try {
          await request('comment-state', {thread_id: thread.id, state: thread.state === 'open' ? 'resolved' : 'open', resolution: input.value, expected_version: thread.version});
          await load(true);
        } catch (error) { failure(error); }
      });
      item.append(form);
      box.append(item);
    }
  }
  function renderDecisions() {
    const box = $('decisions');
    box.replaceChildren();
    for (const decision of data.decisions) {
      const item = node('p', undefined, 'review-meta');
      item.append(node('span', decision.decision === 'approved' ? '승인' : '수정 요청', 'badge ' + (decision.decision === 'approved' ? 'pass' : 'fail')), ` ${decision.actor} · ${new Date(decision.created_at * 1000).toLocaleString('ko-KR')} · ${decision.current ? '현재 변경 기준' : '이전 변경 기준 (현재 결정 아님)'}`);
      if (decision.note) item.append(node('span', ' · ' + decision.note));
      box.append(item);
    }
  }
  let approvalReason='검증 근거를 확인해주세요.';
  function syncDecision(reason=approvalReason){
    approvalReason=reason;
    const approved=document.querySelector('input[name=decision][value=approved]');
    approved.disabled=!!reason;
    $('decision-reason').textContent=reason;
    $('decision-form').querySelector('button[type=submit]').disabled=approved.checked&&!!reason;
  }
  $('decision-form').addEventListener('change',()=>syncDecision());
  async function loadEvidence(g) {
    const box = $('verification');
    box.replaceChildren(node('p', '검증 근거를 확인하는 중…', 'review-meta'));
    try {
      const evidence = await request('evidence?' + new URLSearchParams({task}), undefined, g);
      const proof = evidence.verification;
      syncDecision(!data.complete?'변경 내용이 검토 한도를 넘어 승인할 수 없습니다.':data.threads.some(t=>t.state==='open')?'처리하지 않은 코멘트가 있습니다.':!proof.current?'현재 변경에 대한 검증 통과 기록이 필요합니다: '+proof.reason:'');
      const card = node('div', undefined, 'proof' + (proof.current ? ' current' : ''));
      card.append(node('strong', proof.current ? '현재 변경에 대한 검증 통과' : '현재 검증 근거 없음'), node('p', proof.reason, 'review-meta'));
      for (const gate of proof.gates || []) card.append(node('p', `${gate.gate}: ${gate.passed ? '통과' : '실패'}${gate.required ? ' · 필수' : ''}`, 'review-meta'));
      box.replaceChildren(card);
      const publish = $('publish');
      publish.replaceChildren();
      for (const row of evidence.publish) publish.append(node('p', `${row.repository} · PR ${row.pr_number || '없음'} · CI ${row.ci_state} · 병합 ${row.merge_state}. ${row.note}`, 'review-meta'));
    } catch (error) {
      if (error !== stale) {syncDecision('검증 근거를 불러오지 못했습니다. 새로고침해주세요.');box.replaceChildren(node('p', '검증 근거를 불러오지 못했습니다: ' + errText(error), 'review-meta'));}
    }
  }
  let previewData = null, prepared = null, session = null, shot = null, element = null;
  // Messages ending in "중…" mean the service or capture is still being prepared.
  function previewStatus(text) { $('preview-status').textContent = text; DevRosterProgress.busyAfter($('preview-status'), text.endsWith('중…'), '미리보기 준비 중'); }
  async function loadPreview(g) {
    try {
      previewData = await request('preview?' + new URLSearchParams({task}), undefined, g);
    } catch (error) { if (error !== stale) { $('preview-section').hidden=false;$('preview-empty').hidden=false;$('preview-empty').textContent='미리보기 정보를 불러오지 못했습니다. 새로고침 후 다시 확인해주세요.';$('preview-prepare').hidden=true;previewStatus('미리보기 정보를 불러오지 못했습니다: ' + errText(error)); } return; }
    $('preview-section').hidden = false;
    const services = previewData.services;
    $('preview-empty').hidden = services.length > 0;
    $('preview-prepare').hidden = !services.length;
    $('preview-service').replaceChildren(...services.map(s => new Option(`${s.id} · ${s.origin}`, s.id)));
    const active = previewData.sessions.filter(s => s.state === 'active');
    session = active.length ? active[0] : null;
    $('preview-session').hidden = !session;
    if (session) $('preview-session-text').textContent = `연결됨 · ${session.origin} · 세션 ${session.generation}${session.owned ? ' · 이 화면이 시작한 서비스' : ' · 기존 실행 서비스(종료하지 않음)'}`;
    if (!session && previewData.sessions[0] && previewData.sessions[0].process_lost) previewStatus('DevRoster가 다시 시작되어 이전에 시작한 미리보기 서비스가 종료되었습니다. 서비스를 다시 시작해주세요.');
    if (!previewData.browser_available) previewStatus('설치된 브라우저 실행기가 설정되지 않아 캡처할 수 없습니다. 변경 검토와 Studio 목업 검토는 계속 사용할 수 있습니다.');
    threadList($('preview-threads'), previewData.threads, '아직 화면 코멘트가 없습니다.', thread => node('span', `${thread.anchor.path} · ${thread.anchor.element.testid || thread.anchor.element.text || thread.anchor.element.path}`, 'review-meta'));
    $('preview-decisions').replaceChildren(...previewData.decisions.map(d => node('p', `${d.decision === 'approved' ? '화면 확정' : '수정 요청'} · ${d.actor} · ${d.current ? '최신 캡처 기준' : '이전 캡처 기준 (현재 결정 아님)'}${d.note ? ' · ' + d.note : ''}`, 'review-meta')));
    const latest = previewData.captures.find(c => c.current);
    if (latest && (!shot || shot.id !== latest.id)) showCapture(await request('preview-detail?' + new URLSearchParams({task, capture: latest.id}), undefined, g));
    else if (!latest) { shot = null; $('preview-capture-view').hidden = true; }
  }
  function showCapture(manifest) {
    shot = manifest;
    element = null;
    $('preview-comment').hidden = true;
    $('preview-capture-view').hidden = false;
    $('preview-image').src = '/api/review/preview-screenshot?' + new URLSearchParams({task, capture: manifest.id});
    $('preview-image').alt = `${manifest.path} 화면 캡처 (${manifest.viewport.width}×${manifest.viewport.height})`;
    const overlay = $('preview-overlay'), list = $('preview-elements');
    overlay.replaceChildren();
    list.replaceChildren();
    for (const item of manifest.elements) {
      const label = item.testid || item.text || item.path;
      const choose = () => selectElement(item);
      const hit = node('button', undefined, 'preview-hit');
      hit.type = 'button';
      hit.tabIndex = -1;
      hit.setAttribute('aria-hidden', 'true');
      Object.assign(hit.style, {left: item.rect.x / manifest.viewport.width * 100 + '%', top: item.rect.y / manifest.viewport.height * 100 + '%', width: item.rect.width / manifest.viewport.width * 100 + '%', height: item.rect.height / manifest.viewport.height * 100 + '%'});
      hit.addEventListener('click', choose);
      overlay.append(hit);
      const button = node('button', `${item.tag}${item.testid ? ' · ' + item.testid : ''} · ${label}`, 'secondary');
      button.type = 'button';
      button.dataset.index = item.index;
      button.addEventListener('click', choose);
      list.append(button);
    }
    previewStatus(`캡처 ${new Date(manifest.created_at * 1000).toLocaleString('ko-KR')} · ${manifest.path} · 요소 ${manifest.elements.length}개${manifest.blocked_origins.length ? ' · 차단한 외부 요청 ' + manifest.blocked_origins.length + '개' : ''}`);
  }
  function selectElement(item) {
    element = item;
    for (const el of document.querySelectorAll('#preview-elements button, .preview-hit')) el.setAttribute('aria-pressed', 'false');
    for (const el of document.querySelectorAll(`#preview-elements button[data-index="${item.index}"]`)) el.setAttribute('aria-pressed', 'true');
    $('preview-overlay').children[shot.elements.indexOf(item)]?.setAttribute('aria-pressed', 'true');
    $('preview-anchor').textContent = `선택한 요소: ${item.testid || item.text || item.path}`;
    $('preview-comment').hidden = false;
    $('preview-body').focus();
  }
  async function previewAction(action, payload, success, form) {
    try {
      const result = await request(action, payload);
      cleared(form);
      if (success) success(result);
      await loadPreview(generation);
    } catch (error) { failure(error, form); }
  }
  async function load(keepFile = false) {
    if (!task) { $('review-empty').hidden = false; return; }
    const g = ++generation;
    syncDecision('검증 근거를 확인하는 중입니다.');
    $('review-error').hidden = true;
    $('review-empty').hidden = true;
    $('review-loading').hidden = false;
    notice('');
    try {
      data = await request('bootstrap?' + new URLSearchParams({task}), undefined, g);
      token = data.token;
      $('review-task').textContent = `${data.task.id} · ${data.task.title}`;
      document.querySelector('.review-task-switch').open=false;$('review-layout').hidden = false; $('review-screen-tab').disabled=false;
      $('refresh').hidden = false;
      if (!data.complete) notice('변경 내용이 검토 한도(파일 100개·1MiB)를 넘어 일부만 표시합니다. 이 상태에서는 승인할 수 없습니다.');
      if (!keepFile) selected = null;
      renderFiles();
      renderThreads();
      renderDecisions();
      loadEvidence(g);
      loadPreview(g);
      const first = data.modules.flatMap(m => m.files.map(f => [m.id, f.stage, f.path]))[0];
      if (selected) openFile(selected.module, selected.stage, selected.path);
      else if (first) openFile(...first);
      else { $('diff-title').textContent = '변경 내용 없음'; $('diff-meta').textContent = ''; $('diff-body').replaceChildren(); }
    } catch (error) {
      $('review-layout').hidden = true;$('review-screen-tab').disabled=true;
      failure(error);
    } finally {
      if (g === generation) $('review-loading').hidden = true;
    }
  }
  syncDecision();
  $('task-form').addEventListener('submit', event => {
    event.preventDefault();
    task = $('task-input').value.trim();
    history.replaceState(null, '', '?' + new URLSearchParams({task}));
    load();
  });
  $('refresh').addEventListener('click', () => load(true));
  $('comment-cancel').addEventListener('click', () => { $('comment-form').hidden = true; anchor = null; });
  $('comment-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!anchor) return;
    try {
      await request('comment', Object.assign({body: $('comment-body').value, review_digest: data.review_digest}, anchor));
      $('comment-body').value = '';
      await load(true);
      cleared(event.target);
      notice('코멘트를 남겼습니다.');
    } catch (error) { failure(error, event.target); }
  });
  $('decision-form').addEventListener('submit', async event => {
    event.preventDefault();
    const decision = new FormData(event.target).get('decision');
    if(decision==='approved'&&approvalReason){failure(new Error(approvalReason),event.target);return;}
    try {
      await request('decision', {decision, note: $('decision-note').value, review_digest: data.review_digest});
      $('decision-note').value = '';
      await load(true);
      cleared(event.target);
      notice(decision === 'approved' ? '현재 변경 내용에 대한 승인을 기록했습니다.' : '수정 요청을 기록했습니다.');
    } catch (error) { failure(error, event.target); }
  });
  $('preview-prepare').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      prepared = await request('preview-prepare', {service: $('preview-service').value});
      $('preview-command').textContent = `실행 명령: ${prepared.service.argv.join(' ') || '(등록되지 않음)'} · 폴더: ${prepared.service.module}/${prepared.service.cwd} · 주소: ${prepared.service.origin} · 현재 ${prepared.running ? '실행 중' : '실행되지 않음'}. ${prepared.notice}`;
      $('preview-confirm').hidden = false;
    } catch (error) { failure(error); }
  });
  $('preview-start').addEventListener('submit', event => {
    event.preventDefault();
    if (!prepared) return;
    const mode = new FormData(event.target).get('mode');
    previewStatus(mode === 'launch' ? '서비스를 시작하고 준비 상태를 확인하는 중…' : '서비스에 연결하는 중…');
    previewAction('preview-start', {service: prepared.service.id, mode, confirm_digest: prepared.confirm_digest}, () => { $('preview-confirm').hidden = true; previewStatus('연결했습니다. 화면 경로를 입력해 캡처하세요.'); });
  });
  $('preview-capture').addEventListener('submit', event => {
    event.preventDefault();
    if (!session) return;
    previewStatus('화면을 캡처하는 중…');
    previewAction('preview-capture', {session_id: session.id, generation: session.generation, path: $('preview-path').value, viewport: {width: Number($('preview-width').value), height: Number($('preview-height').value)}}, result => { if (result.available) showCapture(result); else previewStatus(result.reason); });
  });
  $('preview-stop').addEventListener('click', () => { if (session) previewAction('preview-stop', {session_id: session.id}, () => previewStatus('연결을 종료했습니다.')); });
  $('preview-comment').addEventListener('submit', event => {
    event.preventDefault();
    if (!shot || !element) return;
    previewAction('preview-comment', {capture_id: shot.id, element: element.index, body: $('preview-body').value}, () => { $('preview-body').value = ''; $('preview-comment').hidden = true; previewStatus('화면 코멘트를 남겼습니다.'); }, event.target);
  });
  $('preview-decision').addEventListener('submit', event => {
    event.preventDefault();
    if (!shot) { previewStatus('먼저 화면을 캡처해주세요.'); return; }
    const decision = new FormData(event.target).get('screen-decision');
    previewAction('preview-decision', {capture_id: shot.id, decision, note: $('preview-note').value}, () => { $('preview-note').value = ''; previewStatus(decision === 'approved' ? '최신 캡처 기준으로 화면을 확정했습니다.' : '화면 수정 요청을 기록했습니다.'); }, event.target);
  });
  task = new URLSearchParams(location.search).get('task') || '';
  $('task-input').value = task;
  load();

  for(const kind of ['code','screen'])document.getElementById('review-'+kind+'-tab').addEventListener('click',()=>{document.body.dataset.reviewView=kind;for(const name of ['code','screen'])document.getElementById('review-'+name+'-tab').setAttribute('aria-selected',String(name===kind));});
  document.body.dataset.reviewView='code';
})();
