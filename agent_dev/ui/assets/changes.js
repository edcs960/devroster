/* Every request and submit is bound to the complete selected input generation. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const stale = Symbol('superseded');
  let generation = 0, detailGeneration = 0, detailId = '', token = '', selectedProject = '';
  let context = {}, preview = null, frozenPreviewInput = null;
  const show = (id, visible) => { $(id).hidden = !visible; };
  const node = (tag, value, className) => {
    const el = document.createElement(tag);
    if (value !== undefined) el.textContent = value;
    if (className) el.className = className;
    return el;
  };
  function invalidate() {
    generation += 1;
    preview = frozenPreviewInput = null;
    $('form-propose-btn').disabled = true;
    show('form-preview-result', false);
    show('form-error', false);
    return generation;
  }
  function valid(g, d) { return g === generation && (d === undefined || d === detailGeneration); }
  async function request(action, payload, g, d = detailGeneration) {
    const options = payload === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Local-Token': token},
      body: JSON.stringify(payload),
    };
    let response, data;
    try {
      response = await fetch('/api/changes/' + action, options);
      if (!valid(g, d)) throw stale;
      data = await response.json();
      if (!valid(g, d)) throw stale;
    } catch (error) {
      if (!valid(g, d)) throw stale;
      throw error;
    }
    if (!response.ok) {
      const error = new Error((response.status === 409 ? '버전이 변경되었습니다. 입력을 유지하고 새 미리보기를 실행하세요. ' : '') + (data.error || response.statusText));
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function errorAt(id, error, g, d) {
    if (error === stale || !valid(g, d)) return;
    $(id).textContent = error.message || String(error);
    show(id, true);
  }
  function options(select, entries, value, empty = '선택하세요') {
    select.replaceChildren(new Option(empty, ''), ...entries.map(([id, label]) => new Option(label, id)));
    if (entries.some(e => e[0] === value)) select.value = value;
  }
  function profile() {
    const row = (context.profiles || []).find(p => String(p.version) === $('form-profile').value);
    return row ? JSON.parse(row.content_json) : {};
  }
  function source() { return (context.sources || []).find(s => s.id === $('form-source').value); }
  function oldImport() { return (context.imports || []).find(i => i.id === $('form-import').value); }
  const stateLabel = s => ({proposed:'제안됨', applied:'적용됨', rejected:'거부됨', unknown:'미확정', changed:'변경', removed:'삭제', excluded:'이유 있는 제외'})[s] || s;
  function deltaTable(container, delta) {
    container.replaceChildren();
    for (const [id, state] of Object.entries(delta || {})) {
      const row = node('div', undefined, 'delta-row');
      row.append(node('strong', id), node('span', ({added:'추가',unchanged:'동일'})[state] || stateLabel(state), 'delta-badge ' + state));
      container.append(row);
    }
  }
  function selectedTasks() {
    const src = source();
    if (!src) return [];
    return [...$('form-task-mapping').children].filter(row => row.querySelector('[data-selected]').checked).map(row => {
      const original = src.handoff.tasks.find(t => t.id === row.dataset.taskId);
      const checks = kind => [...row.querySelectorAll(`[data-${kind}]:checked`)].map(el => el.value);
      const mappings = [...row.querySelectorAll('[data-selector]')].filter(el => el.value).map(el => ({
        requirement_id: el.dataset.req, criteria_ids: [el.dataset.criterion], gate: el.dataset.gate, target: el.value,
      }));
      return {...original, module_id: row.querySelector('[data-module]').value,
        verification_gates: checks('gate'), allowed_paths: checks('path'), verification_mapping: mappings};
    });
  }
  function drawTaskScope(row, task, moduleId) {
    const scope = row.querySelector('[data-scope]');
    scope.replaceChildren();
    const config = profile();
    const gates = (config.verification || {}).commands || [];
    for (const path of ((config.repositories || {})[moduleId] || {}).allowed_paths || []) {
      const label = node('label', undefined, 'check-label'), check = document.createElement('input');
      check.type = 'checkbox'; check.dataset.path = ''; check.value = path; check.checked = true;
      label.append(check, node('span', '허용 경로: ' + path)); scope.append(label);
    }
    for (const gate of gates.filter(g => g.repository === moduleId)) {
      const label = node('label', undefined, 'check-label'), check = document.createElement('input');
      check.type = 'checkbox'; check.dataset.gate = ''; check.value = gate.name; check.checked = true;
      label.append(check, node('span', '검증: ' + gate.name)); scope.append(label);
      const selectors = gate.command.filter(t => /\.py::/.test(t));
      const req = source().requirements.find(r => task.requirement_ids.includes(r.id));
      for (let i = 0; i < (req.criteria_ids || []).length; i++) {
        const label = node('label', `${req.criteria_ids[i]} · ${req.acceptance[i]} → ${gate.name}`);
        const select = document.createElement('select');
        select.dataset.selector = ''; select.dataset.req = req.id; select.dataset.criterion = req.criteria_ids[i]; select.dataset.gate = gate.name;
        options(select, selectors.map(t => [t,t]), '', '테스트 연결 없음 (현재 검증 근거 아님)');
        label.append(select); scope.append(label);
      }
    }
  }
  function drawTasks() {
    const src = source(), imp = oldImport();
    const container = $('form-task-mapping'); container.replaceChildren();
    if (!src || !src.finalized || !imp) return;
    const old = Object.fromEntries((context.import_requirements || []).filter(r => r.import_id === imp.id).map(r => [r.requirement_id, r.content_digest]));
    const repos = Object.keys(profile().repositories || {});
    for (const task of src.handoff.tasks) {
      const req = src.requirements.find(r => task.requirement_ids.includes(r.id));
      const row = node('fieldset', undefined, 'task-mapping'); row.dataset.taskId = task.id;
      const legend = node('legend'), check = document.createElement('input');
      check.type = 'checkbox'; check.dataset.selected = ''; check.checked = old[req.id] !== src.digests[req.id];
      legend.append(check, node('span', `${req.id} · ${req.title}`)); row.append(legend);
      row.append(node('small', `작업 ${task.id} · ${old[req.id] === src.digests[req.id] ? '동일 요구사항' : old[req.id] ? '변경 요구사항' : '추가 요구사항'}`));
      const label = node('label', '모듈'), module = document.createElement('select'); module.dataset.module = '';
      options(module, repos.map(r => [r,r]), $('form-module').value); label.append(module); row.append(label);
      const scope = node('div'); scope.dataset.scope = ''; row.append(scope); container.append(row);
      drawTaskScope(row, task, module.value);
      module.addEventListener('change', () => { invalidate(); drawTaskScope(row, task, module.value); });
    }
  }
  function chooseImport() {
    const imp = oldImport(), sources = (context.sources || []).filter(s => !imp || s.id === imp.source_studio_id);
    options($('form-source'), sources.map(s => [s.id, s.id + (s.finalized ? ` · 명세 ${s.source.spec_revision} / 목업 ${s.source.mock_revision} / v${s.source.project_version}` : ' · 확정 필요')]), sources[0]?.id);
    chooseSource();
  }
  function chooseSource() {
    const src = source();
    $('form-source-status').textContent = src ? src.finalized ? `현재 확정 revision · bundle ${src.bundle_digest.slice(0, 12)} · 전체 화면 승인 / 미해결 의견 0 / 활성 생성 작업 0` : src.error : 'Studio 원본을 선택하세요.';
    drawTasks(); $('form-impact-list').replaceChildren();
  }
  async function allBootstrap(projectId, g, d = detailGeneration) {
    const merged = {projects:[],imports:[],profiles:[],tasks:[],sources:[],import_requirements:[],changes:[]};
    let offset = 0;
    do {
      const data = await request('bootstrap?' + new URLSearchParams({project_id:projectId, limit:'100', offset:String(offset)}), undefined, g, d);
      if (!valid(g,d)) throw stale;
      token = data.token || token;
      for (const key of Object.keys(merged)) merged[key].push(...(data[key] || []));
      offset = ['projects','imports','profiles','tasks','sources'].some(k => data[k + '_next_offset'] !== null && data[k + '_next_offset'] !== undefined) || data.next_offset !== null ? offset + 100 : null;
    } while (offset !== null);
    return merged;
  }
  async function loadProject(projectId, initial = false) {
    selectedProject = projectId; const g = invalidate(); ++detailGeneration; detailId = '';
    show('changes-detail', false); $('change-detail-placeholder').hidden=false; show('changes-loading', true); show('changes-error', false); show('changes-retry', false);
    $('changes-result').textContent = ''; $('changes-result-links').replaceChildren();
    try {
      const data = await allBootstrap(projectId, g);
      if (!valid(g)) return;
      context = data;
      if (initial) {
        for (const id of ['changes-project-select','form-project']) options($(id), data.projects.map(p => [p.id,`${p.name} · ${p.id}`]), projectId);
      } else {
        $('changes-project-select').value = $('form-project').value = projectId;
      }
      options($('form-import'), data.imports.filter(i => i.project_id === projectId).map(i => [i.id,`${i.source_studio_id} · ${i.design_id} · ${new Date(i.created_at*1000).toLocaleString()} · ${i.id.slice(0,8)}`]), '');
      options($('form-profile'), data.profiles.map(p => [String(p.version),`v${p.version} · ${p.digest.slice(0,12)}${p.version === data.profiles[0].version ? ' · 현재' : ' · 이전 (읽기용)'}`]), String(data.profiles[0]?.version || ''));
      options($('form-module'), Object.keys(profile().repositories || {}).map(r => [r,r]), Object.keys(profile().repositories || {})[0]);
      chooseImport(); renderList(data.changes); show('changes-section', true); show('changes-loading', false);
      show('changes-empty', !projectId && !data.projects.length);
    } catch (error) {
      errorAt('changes-error', error, g); if (valid(g)) { show('changes-loading', false); show('changes-retry', true); }
    }
  }
  function resultLinks(change) {
    const target = $('changes-result-links'); target.replaceChildren();
    const links = change.next_actions || (change.target_import_id ? [
      {href:'/agent?' + new URLSearchParams({operation:'design.approve',design_id:change.design_id,version:'1'}),label:'1. 새 설계 승인'},
      {href:'/agent?' + new URLSearchParams({operation:'plan.approve',plan_id:change.plan_id}),label:'2. 설계 승인 후 새 계획 승인'},
      {href:'/traceability?' + new URLSearchParams({project_id:change.project_id,import_id:change.target_import_id}),label:'후속 작업 · 실제 검증 근거'},
    ] : []);
    for (const link of links) { const a = node('a',link.label); a.href = link.href; target.append(a); }
  }
  function renderList(changes) {
    const list = $('changes-list'); list.replaceChildren();
    const filtered = changes.filter(c => (!$('changes-state-filter').value || c.state === $('changes-state-filter').value) && (!selectedProject || c.project_id === selectedProject));
    show('changes-list-empty', !filtered.length && !!context.projects?.length);
    $('changes-list-empty').textContent=$('changes-state-filter').value?'조건에 맞는 변경 요청이 없습니다. 상태 필터를 바꿔보세요.':'아직 변경 요청이 없습니다. 아래에서 새 요구사항 변경 요청을 준비하세요.';
    for (const change of filtered) {
      const card = node('article',undefined,'change-card'); card.dataset.state = change.state;card.dataset.changeId=change.id;card.setAttribute('aria-current',String(change.id===detailId)); card.setAttribute('role','listitem');
      card.append(node('h3',change.reason),node('p',`${stateLabel(change.state)} · ${change.actor} · v${change.version}`));
      const actions = node('div',undefined,'change-card-actions');
      const detail = node('button','상세 보기'); detail.onclick = () => loadDetail(change.id,change.project_id); actions.append(detail);
      if (change.state === 'proposed') {
        const apply = node('button','변경 적용'); apply.onclick = () => mutateChange('apply',change);
        const reject = node('button','거부'); reject.className = 'secondary'; reject.onclick = () => { loadDetail(change.id,change.project_id); $('reject-reason').focus(); };
        const confirm = node('button','거부 확정'); confirm.className = 'secondary'; confirm.onclick = () => mutateChange('reject',change);
        actions.append(apply,reject,confirm);
      }
      card.append(actions); list.append(card);
    }
  }
  async function refreshList(g) {
    const data = await allBootstrap(selectedProject, g);
    if (!valid(g)) return;
    context.changes = data.changes; renderList(data.changes);
  }
  function renderImpacts(container, impacts, editable = false) {
    container.replaceChildren();
    if (!impacts.length) container.append(node('p','기록된 변경·삭제 영향 없음. 추가 요구사항은 새 후속 계획에서 검증합니다.'));
    for (const impact of impacts) {
      const item = node('fieldset',undefined,'impact-item'); item.dataset.requirement = impact.requirement_id;
      item.dataset.task = impact.task_id || ''; item.dataset.evidence = impact.evidence_digest;
      item.append(node('legend',`${impact.requirement_id} → ${impact.task_id || '실제 작업 연결 없음 · 미확정'}`));
      item.append(node('p',`${stateLabel(impact.disposition)} · ${impact.completion || ''} · 근거 ${(impact.evidence || []).length}개 · ${impact.evidence_digest.slice(0,12)}`));
      const evidence = node('details'), summary = node('summary','실제 연결 근거 / 추론 후보'); evidence.append(summary);
      for (const e of impact.evidence || []) evidence.append(node('p',`${e.kind}: ${e.target_ref} · ${e.current_state}`));
      evidence.append(node('p','추론 후보는 실제 요구사항 검증 범위에 포함하지 않습니다.'));
      for (const candidate of impact.inferred_candidates || []) evidence.append(node('p',`추론: ${candidate.task_id} · ${candidate.reason}`));
      item.append(evidence);
      if (editable) {
        const taskLabel = node('label','실제 영향 작업'), task = document.createElement('select'); task.dataset.impactTask = '';
        const authentic = context.tasks.filter(t => { const d = JSON.parse(t.definition_json); return d.requirement_ids?.includes(impact.requirement_id) && t.plan_id === oldImport().plan_id; });
        options(task,authentic.map(t => [t.id,`${t.title} · ${t.id}`]),impact.task_id || '','실제 작업 없음 · 미확정');
        if (impact.task_id) task.disabled = true;
        taskLabel.append(task); item.append(taskLabel);
        const label = node('label','영향 판단'), select = document.createElement('select'); select.dataset.disposition = '';
        options(select,[[preview?.delta[impact.requirement_id] === 'removed' ? 'removed' : 'changed','변경/삭제 영향 확정'],['unknown','미확정'],['excluded','이유를 남겨 제외']],impact.disposition); label.append(select); item.append(label);
        const reasonLabel = node('label','제외 이유'), reason = document.createElement('input'); reason.dataset.exclusion = ''; reason.maxLength = 500; reason.value = impact.exclusion_reason; reasonLabel.append(reason); item.append(reasonLabel);
        const followupLabel = node('label','새 계획의 후속 작업'), followup = document.createElement('select'); followup.dataset.followup = '';
        options(followup,selectedTasks().map(t => [t.id,`${t.title} · ${t.id}`]),impact.planned_followup_task_id,'후속 작업 선택'); followupLabel.append(followup); item.append(followupLabel);
      } else if (impact.followup_task_id) {
        const a = node('a',`후속 ${impact.followup_task_id} · ${impact.followup_state}`); a.href = '/traceability?' + new URLSearchParams({project_id:selectedProject,task_id:impact.followup_task_id}); item.append(a);
      }
      container.append(item);
    }
  }
  async function loadDetail(id, projectId) {
    const g = generation, d = ++detailGeneration, focusOrigin = document.activeElement; detailId = id;
    show('changes-error',false);
    try {
      const data = await request('detail?' + new URLSearchParams({change_id:id,project_id:projectId}),undefined,g,d);
      if (!valid(g,d) || detailId !== id) return;
      for(const card of $('changes-list').children)card.setAttribute('aria-current',String(card.dataset.changeId===id));
      const content = $('changes-detail-content'); content.replaceChildren(node('h3',data.change.reason),node('p',`${stateLabel(data.change.state)} · v${data.change.version} · ${data.change.actor}`));
      const proposal=JSON.parse(data.change.proposal_json);const comparison=node('section',undefined,'change-comparison');comparison.append(node('h3','원본 ↔ 제안 비교'));const columns=node('div',undefined,'change-original-columns');for(const [label,digests] of [['확정 원본',proposal.before_digests],['제안',proposal.after_digests]]){const panel=node('section');panel.append(node('h4',label));for(const [rid,dg] of Object.entries(digests||{})){const old=context.import_requirements.find(r=>r.import_id===data.change.source_import_id&&r.requirement_id===rid);const content=old?JSON.parse(old.content_json):null;const mapping=proposal.handoff?.mappings?.find(r=>r.requirement_id===rid);panel.append(node('strong',rid+' · '+(label==='확정 원본'?content?.title||'원본 자료 없음':mapping?.title||'제안 자료 없음')),node('p','digest '+String(dg).slice(0,16)));}columns.append(panel);}comparison.append(columns,node('p','현재 응답의 요구사항 제목·digest 비교입니다. 원문 코드 diff나 변경 적용 결과를 의미하지 않습니다.','changes-meta'));content.append(comparison);const delta = node('div',undefined,'delta-table'); deltaTable(delta,data.delta); content.append(node('h3','변경 분류'),delta,node('h3','영향 받는 작업·근거'));
      const impacts = node('div'); renderImpacts(impacts,data.impacts); content.append(impacts);
      resultLinks(data.change); show('changes-detail',true); $('change-detail-placeholder').hidden=true;
      if (document.activeElement === focusOrigin || document.activeElement === document.body) $('changes-detail').focus();
    } catch (error) { errorAt('changes-error',error,g,d); }
  }
  function captureInput() {
    const src = source();
    if (!selectedProject || !oldImport() || !src?.finalized || !$('form-module').value || !$('form-reason').value.trim() || !$('form-actor').value.trim()) throw new Error('프로젝트·기존 인계·새 확정 원본·프로파일·모듈·이유·작성자를 선택하세요.');
    const decisions = [], followups = [];
    for (const row of $('form-impact-list').children) {
      if (!row.dataset.requirement) continue;
      const tid = row.querySelector('[data-impact-task]').value || null;
      const disposition = row.querySelector('[data-disposition]').value;
      const decision = {requirement_id:row.dataset.requirement, task_id:tid, disposition, exclusion_reason:row.querySelector('[data-exclusion]').value.trim()};
      if (tid === (row.dataset.task || null)) decision.evidence_digest = row.dataset.evidence;
      decisions.push(decision);
      const target = row.querySelector('[data-followup]').value;
      if (target) followups.push({requirement_id:row.dataset.requirement,task_id:tid,followup_task_id:target});
    }
    return {project_id:selectedProject,source_import_id:oldImport().id,new_studio_id:src.id,reason:$('form-reason').value.trim(),actor:$('form-actor').value.trim(),
      handoff:{...src.handoff,profile_version:Number($('form-profile').value),tasks:selectedTasks()},
      impact_decisions:decisions,followup_mapping:followups,unchanged_followup_reason:$('form-unchanged-reason').value.trim()};
  }
  async function previewChange() {
    const g = invalidate();
    try {
      const payload = captureInput();
      const data = await request('preview',payload,g);
      if (!valid(g)) return;
      preview = data; frozenPreviewInput = JSON.parse(JSON.stringify(data.selection));
      deltaTable($('form-delta-table'),data.delta); renderImpacts($('form-impact-list'),data.impacts,true);
      $('form-preview-digest').textContent = '미리보기: ' + data.preview_digest;
      show('form-preview-result',true); $('form-propose-btn').disabled = false;
    } catch (error) { errorAt('form-error',error,g); }
  }
  async function proposeChange() {
    if (!preview || !frozenPreviewInput) return;
    const g = generation;
    const payload = {...JSON.parse(JSON.stringify(frozenPreviewInput)),preview_digest:preview.preview_digest,request_id:'PROP-' + crypto.randomUUID()};
    $('form-propose-btn').disabled = true;
    try {
      const result = await request('propose',payload,g);
      if (!valid(g)) return;
      invalidate(); const next = generation;
      $('changes-result').textContent = '변경 제안 저장됨. 현재 작업·승인·실행에는 영향이 없습니다.';
      await refreshList(next);
      if (!valid(next)) return;
      await loadDetail(result.change_id,result.project_id);
      if (!valid(next)) return;
    } catch (error) { if (valid(g)) { preview = frozenPreviewInput = null; $('form-propose-btn').disabled = true; show('form-preview-result',false); } errorAt('form-error',error,g); }
  }
  async function mutateChange(action, change) {
    const g = generation, d = detailGeneration;
    const payload = {project_id:change.project_id,change_id:change.id,expected_version:change.version,request_id:action.toUpperCase() + '-' + crypto.randomUUID()};
    if (action === 'reject') { payload.reason = $('reject-reason').value.trim(); payload.actor = $('reject-actor').value.trim(); }
    try {
      const result = await request(action,payload,g,d);
      if (!valid(g,d)) return;
      $('changes-result').textContent = action === 'apply' ? '변경 적용됨. 새 설계 승인 후 계획을 승인해야 후속 작업이 생성됩니다.' : '변경 요청이 거부되었습니다.';
      resultLinks(result);
      await refreshList(g);
      if (!valid(g,d)) return;
      await loadDetail(change.id,change.project_id);
      if (!valid(g)) return;
    } catch (error) { errorAt('changes-error',error,g,d); }
  }
  $('changes-form').addEventListener('submit',event => event.preventDefault());
  $('changes-form').addEventListener('input',invalidate);
  $('changes-form').addEventListener('change',event => { if (!['form-project','form-import','form-source','form-profile','form-module'].includes(event.target.id)) invalidate(); });
  for (const id of ['changes-project-select','form-project']) $(id).addEventListener('change',() => { try { if ($(id).value) localStorage.setItem('devroster.home.project',$(id).value); } catch {} loadProject($(id).value); });
  $('form-import').addEventListener('change',() => { invalidate(); chooseImport(); });
  $('form-source').addEventListener('change',() => { invalidate(); chooseSource(); });
  $('form-profile').addEventListener('change',() => { invalidate(); options($('form-module'),Object.keys(profile().repositories || {}).map(r => [r,r]),Object.keys(profile().repositories || {})[0]); drawTasks(); });
  $('form-module').addEventListener('change',() => { invalidate(); drawTasks(); });
  $('changes-state-filter').addEventListener('change',() => { invalidate(); renderList(context.changes || []); });
  $('changes-filter-apply').addEventListener('click',() => renderList(context.changes || []));
  $('form-preview-btn').onclick = previewChange; $('form-propose-btn').onclick = proposeChange;
  $('changes-detail-close').onclick = () => { ++detailGeneration; detailId = ''; show('changes-detail',false); $('change-detail-placeholder').hidden=false; };
  for (const id of ['reject-reason','reject-actor']) $(id).addEventListener('input',() => { ++generation; });
  $('changes-retry').onclick = () => loadProject(selectedProject,!$('form-project').options.length);
  // Open on the project the user worked with last (shared with home), else the first one.
  const remembered = (() => { try { return localStorage.getItem('devroster.home.project') || ''; } catch { return ''; } })();
  const requested = new URLSearchParams(location.search).get('project_id') || '';
  loadProject('',true).then(() => {
    const ids = (context.projects || []).map(p => p.id);
    const pick = [requested, remembered, ids[0]].find(id => id && ids.includes(id));
    if (pick && !selectedProject) return loadProject(pick);
  });
})();
