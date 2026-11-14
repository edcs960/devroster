/* Usage and progress dashboard: read-only GETs, refreshed while the page is visible. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const errText = error => globalThis.DevRosterUI ? globalThis.DevRosterUI.errorMessage(error) : (error?.message || String(error));
  const node = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; };
  const REFRESH_MS = 15000;
  const GROUPS = [
    ['in_progress', '진행 중', '실행·검증·용량 대기'],
    ['needs_user', '사용자 차례', '승인·검토·피드백·게시'],
    ['blocked', '차단', '원인 해결 필요'],
    ['planned', '계획됨', '아직 승인 전'],
    ['completed', '완료', ''],
    ['cancelled', '취소', ''],
  ];
  const GROUP_OF = Object.fromEntries([
    ['ready', 'in_progress'], ['running', 'in_progress'], ['verifying', 'in_progress'], ['waiting_for_capacity', 'in_progress'],
    ['approval_pending', 'needs_user'], ['user_review', 'needs_user'], ['feedback_required', 'needs_user'], ['pr_ready', 'needs_user'],
    ['blocked', 'blocked'], ['planned', 'planned'], ['completed', 'completed'], ['cancelled', 'cancelled'],
  ]);
  const PROVIDERS = {codex_cli: 'Codex CLI', claude_cli: 'Claude CLI', gemini_cli: 'Gemini CLI', openai_api: 'OpenAI API', anthropic_api: 'Claude API', gemini_api: 'Gemini API', xai_api: 'Grok API', exaone_api: 'K-EXAONE API', compatible_api: '호환 API'};
  const REASONS = {
    price_book_missing: '가격표가 없는 작업', price_missing: '가격표에 없는 모델', model_unknown: '모델 정보 없음',
    usage_unknown: '토큰 기록 없음', cached_price_missing: '캐시 입력 가격 없음', provider_cache_contract_unknown: '캐시 집계 방식을 모르는 도구',
  };
  const STUDIO_STEPS = {spec_requirements: 'Studio 요구사항', spec_documents: 'Studio 명세서 문서', specs: 'Studio 명세서', mock: 'Studio 화면 목업'};
  const roleLabel = role => role.startsWith('studio:') ? (STUDIO_STEPS[role.slice(7)] || 'Studio ' + role.slice(7)) : (role || '알 수 없음');
  const number = value => Number(value || 0).toLocaleString('ko-KR');
  const compact = value => {
    const n = Number(value || 0);
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e4) return (n / 1e3).toFixed(1) + 'K';
    return number(n);
  };
  const money = cost => {
    const parts = Object.entries(cost || {}).map(([currency, amount]) =>
      currency === 'KRW' ? '₩' + Math.round(amount).toLocaleString('ko-KR') : '$' + Number(amount).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: amount < 1 ? 4 : 2}));
    return parts.length ? parts.join(' · ') : '—';
  };
  const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString('ko-KR', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '—';
  const duration = s => s < 60 ? Math.round(s) + '초' : s < 3600 ? Math.floor(s / 60) + '분' : Math.floor(s / 3600) + '시간 ' + Math.floor(s % 3600 / 60) + '분';
  let generation = 0, timer = null, labels = {};

  function tile(parent, label, value, note, group) {
    const cell = node('div', undefined, 'usage-tile');
    if (group) cell.dataset.group = group;
    cell.append(node('span', label), node('strong', value));
    if (note) cell.append(node('small', note));
    parent.append(cell);
  }
  function stateChip(state) {
    const chip = node('span', labels[state] || state || '—', 'usage-state');
    chip.dataset.group = GROUP_OF[state] || '';
    return chip;
  }
  function cell(row, value, cls) { const td = node('td', value, cls); row.append(td); return td; }
  function taskLink(id, title) {
    const link = node('a', title || id); link.href = '/agent?' + new URLSearchParams({task: id});
    const wrap = document.createDocumentFragment(); wrap.append(link, node('small', id)); return wrap;
  }
  function emptyRow(body, columns, text) { const row = node('tr'); const td = cell(row, text); td.colSpan = columns; body.append(row); }

  function renderProgress(progress) {
    const groups = $('progress-groups'); groups.replaceChildren();
    for (const [key, label, note] of GROUPS) tile(groups, label, number(progress.groups[key]), note, key);
    $('progress-summary').textContent = '전체 작업 ' + number(progress.total) + '개 · 현재 기준';
    const rate = progress.completion_rate;
    const percent = rate == null ? 0 : Math.round(rate * 1000) / 10;
    $('progress-rate-fill').style.width = percent + '%';
    $('progress-rate').setAttribute('aria-valuenow', String(percent));
    const countable = progress.total - (progress.groups.cancelled || 0);
    $('progress-rate-label').textContent = rate == null ? '완료율: 아직 작업이 없습니다.' : '완료율 ' + percent + '% (완료 ' + number(progress.groups.completed) + ' / 취소 제외 ' + number(countable) + ')';
    const body = $('open-rows'); body.replaceChildren();
    for (const task of progress.open) {
      const row = node('tr');
      cell(row).append(stateChip(task.state));
      cell(row).append(taskLink(task.id, task.title));
      cell(row, when(task.updated_at), 'num');
      body.append(row);
    }
    $('open-table').hidden = !progress.open.length;
    $('open-empty').hidden = progress.open.length > 0;
    $('open-more').hidden = progress.open_total <= progress.open.length;
    $('open-more').textContent = '주의가 필요한 순서로 ' + progress.open.length + '개만 표시했습니다. 전체 ' + number(progress.open_total) + '개는 작업실에서 확인하세요.';
  }

  function renderChart(daily) {
    const chart = $('daily-chart'); chart.replaceChildren();
    const peak = Math.max(0, ...daily.map(d => d.total_tokens));
    for (const day of daily) {
      const bar = node('div', undefined, 'usage-bar');
      if (!day.total_tokens) bar.dataset.empty = '';
      if (day.total_tokens) bar.style.height = Math.max(2, Math.round(day.total_tokens / peak * 100)) + '%';
      bar.title = day.day + ' · 토큰 ' + number(day.total_tokens) + ' · 호출 ' + number(day.calls) + (Object.keys(day.cost).length ? ' · ' + money(day.cost) : '');
      chart.append(bar);
    }
    chart.setAttribute('aria-label', '일별 토큰 막대 그래프, ' + daily.length + '일, 최대 ' + number(peak) + ' 토큰. 아래 "일별 값 보기"에서 값을 표로 볼 수 있습니다.');
    $('daily-first').textContent = daily[0]?.day || '';
    $('daily-last').textContent = daily.length > 1 ? daily[daily.length - 1].day : '';
    const body = $('daily-rows'); body.replaceChildren();
    for (const day of [...daily].reverse()) {
      const row = node('tr');
      cell(row, day.day); cell(row, number(day.calls), 'num'); cell(row, number(day.total_tokens), 'num'); cell(row, money(day.cost), 'num');
      body.append(row);
    }
  }

  function renderUsage(data) {
    const totals = data.totals, cost = data.cost;
    $('usage-period-label').textContent = data.days ? '최근 ' + data.days + '일 (오늘 포함)' : '전체 기간';
    const tiles = $('usage-tiles'); tiles.replaceChildren();
    tile(tiles, 'AI 호출', number(totals.calls), '작업 ' + number(totals.task_calls) + ' · Studio ' + number(totals.studio_calls) + (totals.unmeasured_calls ? ' · 토큰 기록 없음 ' + number(totals.unmeasured_calls) : ''));
    tile(tiles, '입력 토큰', compact(totals.input_tokens), totals.cached_input_tokens ? '캐시 ' + compact(totals.cached_input_tokens) : '');
    tile(tiles, '출력 토큰', compact(totals.output_tokens));
    tile(tiles, '합계 토큰', compact(totals.total_tokens));
    tile(tiles, 'AI 실행 시간', duration(totals.runner_seconds || 0));
    tile(tiles, '예상 비용', money(totals.cost), cost.known_calls || cost.unknown_calls ? '산정 ' + number(cost.known_calls) + '회' + (cost.auto_calls ? ' (자동 가격표 ' + number(cost.auto_calls) + '회)' : '') + ' · 미산정 ' + number(cost.unknown_calls) + '회' : '');
    const reasons = Object.entries(cost.reasons || {}).map(([key, count]) => (REASONS[key] || key) + ' ' + number(count) + '회');
    const hint = $('cost-hint'); hint.replaceChildren();
    if (cost.unknown_calls) {
      hint.append('비용을 알 수 없는 호출은 합계에서 빠졌습니다: ' + reasons.join(', ') + '. ');
      if (cost.unpriced_tasks) {
        const link = node('a', '프로젝트 설정 → 예상 비용'); link.href = '/setup';
        hint.append('자동 가격표에 없는 모델과 구독형 CLI의 가격은 ', link, '에서 입력하며, 입력한 뒤 승인하는 작업부터 적용됩니다.');
      }
    }
    hint.hidden = !cost.unknown_calls;
    const none = !totals.calls;
    $('usage-none').hidden = !none;
    $('usage-detail').hidden = none;
    renderChart(data.daily);
    const models = $('model-rows'); models.replaceChildren();
    for (const row of data.by_model) {
      const tr = node('tr');
      const name = cell(tr, PROVIDERS[row.provider] || row.provider || '알 수 없음');
      name.append(node('small', row.model || '모델 정보 없음'));
      cell(tr, number(row.calls), 'num'); cell(tr, compact(row.input_tokens), 'num'); cell(tr, compact(row.output_tokens), 'num'); cell(tr, money(row.cost), 'num');
      models.append(tr);
    }
    if (!data.by_model.length) emptyRow(models, 5, '기록 없음');
    const roles = $('role-rows'); roles.replaceChildren();
    for (const row of data.by_role) {
      const tr = node('tr');
      cell(tr, roleLabel(row.role)); cell(tr, number(row.calls), 'num'); cell(tr, compact(row.total_tokens), 'num'); cell(tr, money(row.cost), 'num');
      roles.append(tr);
    }
    if (!data.by_role.length) emptyRow(roles, 4, '기록 없음');
    const features = $('feature-rows'); features.replaceChildren();
    const usageCell = (tr, bucket) => { const td = cell(tr, compact(bucket.total_tokens) + ' 토큰', 'num'); td.append(node('small', money(bucket.cost) + ' · ' + number(bucket.calls) + '회')); };
    for (const row of data.by_studio || []) {
      const tr = node('tr');
      const name = node('a', row.title); name.href = '/studio?' + new URLSearchParams({project_id: row.id});
      cell(tr).append(name, node('small', row.id));
      usageCell(tr, row.planning);
      const linked = cell(tr);
      if (!row.tasks.length) linked.append(node('small', '아직 개발 인계 전'));
      const taskLine = task => { const link = node('a', task.title); link.href = '/agent?' + new URLSearchParams({task: task.id}); const line = node('div'); line.append(link, ' ', stateChip(task.state)); return line; };
      row.tasks.slice(0, 5).forEach(task => linked.append(taskLine(task)));
      if (row.tasks.length > 5) {
        const more = node('details'); more.append(node('summary', '외 ' + (row.tasks.length - 5) + '개 작업'));
        row.tasks.slice(5).forEach(task => more.append(taskLine(task)));
        linked.append(more);
      }
      usageCell(tr, row.execution);
      usageCell(tr, row.total);
      features.append(tr);
    }
    $('feature-block').hidden = !(data.by_studio || []).length;
    const tasks = $('task-rows'); tasks.replaceChildren();
    for (const row of data.by_task) {
      const tr = node('tr');
      cell(tr).append(taskLink(row.id, row.title));
      cell(tr).append(stateChip(row.state));
      cell(tr, number(row.calls), 'num'); cell(tr, compact(row.total_tokens), 'num'); cell(tr, money(row.cost), 'num'); cell(tr, when(row.last_call_at), 'num');
      tasks.append(tr);
    }
    if (!data.by_task.length) emptyRow(tasks, 6, '기록 없음');
    $('usage-notes').textContent = cost.note + ' ' + data.studio_note + ' 마지막 갱신 ' + when(data.generated_at) + '.';
  }

  // Reviewer scorecard (domain/review_scorecard.py): findings by how the owner decided them.
  function renderReviewers(card, days) {
    const rows = card?.rows || [], minimum = card?.min_decisions || 0;
    const percent = value => Math.round(value * 100) + '%';
    $('reviewers-period').textContent = (days ? '최근 ' + days + '일' : '전체 기간') + '에 나온 지적 기준';
    $('reviewers-rule').textContent = '독립 검토(명세)와 드리프트 검사(코드)에서 각 AI가 낸 지적을 어떻게 처리했는지 셉니다. 맞음은 명세·코드에 반영했거나 위험을 수용한 지적, 틀림은 반박한 지적입니다. 적중률(맞음 ÷ 처리한 지적)은 처리한 지적이 ' + minimum + '건 이상일 때만 표시합니다. 독립 검토의 깊이를 줄이면 명세 검토 적중률(전체 기간)이 확인된 AI부터 씁니다.';
    $('reviewers-none').hidden = rows.length > 0;
    $('reviewers-wrap').hidden = !rows.length;
    const body = $('reviewer-rows'); body.replaceChildren();
    for (const row of rows) {
      const tr = node('tr');
      const name = cell(tr, PROVIDERS[row.provider] || row.provider || '알 수 없음');
      name.append(node('small', row.model || '모델 정보 없음'));
      if (row.groups.length) {
        const more = node('details'); more.append(node('summary', '관점·유형별 ' + row.groups.length + '개'));
        for (const group of row.groups) {
          const label = (card.sources[group.source] || group.source) + ' · ' + ((card.groups[group.source] || {})[group.group] || group.group || '기타');
          more.append(node('div', label + ': 지적 ' + number(group.found) + ' · 맞음 ' + number(group.confirmed) + ' · 틀림 ' + number(group.rebutted) + (group.precision == null ? '' : ' · 적중률 ' + percent(group.precision))));
        }
        name.append(more);
      }
      cell(tr, number(row.found), 'num'); cell(tr, number(row.confirmed), 'num'); cell(tr, number(row.rebutted), 'num'); cell(tr, number(row.open), 'num');
      const rate = cell(tr, row.precision == null ? '—' : percent(row.precision), 'num');
      if (row.precision == null) rate.append(node('small', '처리 ' + number(row.decided) + '/' + minimum + '건'));
      body.append(tr);
    }
  }

  async function load(initial) {
    const current = ++generation;
    const days = $('usage-period').value;
    try {
      const response = await fetch('/api/usage/' + (initial ? 'bootstrap' : 'summary') + '?' + new URLSearchParams({days}));
      const data = await response.json();
      if (current !== generation) return;
      if (!response.ok) throw new Error(data.error || '현황을 불러오지 못했습니다.');
      $('usage-loading').hidden = true;
      $('usage-error').hidden = true;
      if (!data.ready) { $('usage-empty').hidden = false; $('usage-content').hidden = true; return; }
      labels = data.state_labels || {};
      $('usage-empty').hidden = true;
      $('usage-content').hidden = false;
      renderProgress(data.progress);
      renderUsage(data);
      renderReviewers(data.reviewers, data.days);
    } catch (error) {
      if (current !== generation) return;
      $('usage-loading').hidden = true;
      $('usage-error').textContent = '현황을 불러오지 못했습니다. ' + errText(error) + ' 새로고침을 눌러 다시 시도하세요.';
      $('usage-error').hidden = false;
    }
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(() => { if (document.visibilityState !== 'hidden') load(false); }, REFRESH_MS);
  }
  $('usage-period').addEventListener('change', () => { load(false); schedule(); });
  $('usage-refresh').addEventListener('click', () => { load(false); schedule(); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(false); });
  load(true);
  schedule();
})();
