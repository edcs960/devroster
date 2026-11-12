/* External messenger settings: Slack token, channel, allowlist, listener control. */
(() => {
  'use strict';
// Readable failure text: network/parse errors become a Korean hint, server messages pass through.
  const errText = error => globalThis.DevRosterUI ? globalThis.DevRosterUI.errorMessage(error) : (error?.message || String(error));
  // Shared confirmation dialog (ui-feedback.js); falls back to the browser dialog if it is missing.
  const ask = (title, body, confirm, opts = {}) => globalThis.DevRosterUI ? globalThis.DevRosterUI.confirmAction(title, body, confirm, opts) : Promise.resolve(window.confirm(title + '\n' + body));
  const $ = id => document.getElementById(id);
  const node = (tag, value, className) => { const el = document.createElement(tag); if (value !== undefined) el.textContent = value; if (className) el.className = className; return el; };
  const KIND = {help: '도움말', request: '요청', status: '상태', run: '실행', approve: '승인', unknown: '알 수 없음', invalid: '형식 오류', notify: '상태 알림'};
  const SOCKET = {off: '멈춤', connecting: '연결 중', connected: '연결됨 · 메시지를 바로 받습니다', retrying: '다시 연결하는 중 · 그동안 조회 주기대로 확인'};
  let token = '';
  const ids = value => value.split(',').map(v => v.trim()).filter(Boolean);
  const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : '없음';

  async function call(path, payload) {
    const options = payload === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Local-Token': token}, body: JSON.stringify(payload)};
    const response = await fetch('/api/messengers/' + path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    if (data.token) token = data.token;
    return data;
  }
  function say(message, error) { $('messengers-result').textContent = error ? '' : message; $('messengers-error').hidden = !error; if (error) $('messengers-error').textContent = message; }

  function render(data) {
    const s = data.settings;
    const from = key => { const source = (data.token_sources || {})[key]; return source ? ` 환경변수 ${source.env}에서 읽습니다${source.present ? '.' : ' · 지금 설정되지 않음. ' + source.hint}` : ''; };
    $('token-state').textContent = (data.token_saved ? '봇 토큰이 저장되어 있습니다.' : '봇 토큰이 아직 없습니다.') + from('SLACK_BOT_TOKEN');
    $('app-token-state').textContent = (data.app_token_saved ? '앱 수준 토큰이 저장되어 있습니다(Socket Mode).' : '앱 수준 토큰이 없습니다. Socket Mode를 쓸 때만 필요합니다.') + from('SLACK_APP_TOKEN');
    $('channel-id').value = s.channel_id; $('allowed-users').value = s.allowed_user_ids.join(', '); $('allowed-bots').value = s.allowed_bot_ids.join(', ');
    $('prefix').value = s.prefix; $('poll-seconds').value = s.poll_seconds; $('enabled').checked = s.enabled; $('notify').checked = s.notify; $('socket-mode').checked = s.socket_mode;
    for (const box of document.querySelectorAll('[data-command]')) box.checked = !!s.commands[box.dataset.command];
    for (const box of document.querySelectorAll('[data-bot-command]')) box.checked = !!s.bot_commands[box.dataset.botCommand];
    const facts = $('listener-state'); facts.replaceChildren();
    const fact = (label, value) => facts.append(node('dt', label), node('dd', value));
    fact('이 앱의 리스너', data.running ? '동작 중' : '멈춤');
    fact('다른 곳의 리스너', !data.running && data.owner_active ? '동작 중 (터미널 등)' : '없음');
    if (s.socket_mode) fact('실시간 연결 (Socket Mode)', (SOCKET[data.socket.state] || data.socket.state) + (data.socket.error ? ' · ' + data.socket.error : ''));
    fact('마지막 조회', when(data.last_poll)); fact('처리한 메시지', String(data.handled)); fact('진행 중 작업', String(data.pending));
    if (data.last_error) fact('마지막 오류', data.last_error);
    const recent = $('recent'); recent.replaceChildren(...data.recent.map(row => node('li', `${when(row.at)} · ${row.author} · ${KIND[row.kind] || row.kind} — ${row.reply}`)));
    if (!data.recent.length) recent.append(node('li', '아직 처리한 메시지가 없습니다.'));
  }
  async function load() { try { render(await call('bootstrap')); } catch (error) { say(errText(error), true); } }

  async function act(button, action, message) {
    button.disabled = true;
    try { const data = await action(); render(data); say(message(data)); } catch (error) { say(errText(error), true); } finally { button.disabled = false; }
  }
  $('token-save').addEventListener('click', () => act($('token-save'), async () => { const data = await call('token', {value: $('token-value').value}); $('token-value').value = ''; return data; }, () => '봇 토큰을 저장했습니다.'));
  $('token-delete').addEventListener('click', async () => { if (await ask('슬랙 봇 토큰을 삭제할까요?', '삭제하면 리스너가 슬랙 메시지를 읽거나 답할 수 없습니다. 다시 쓰려면 토큰을 새로 저장해야 합니다.', '토큰 삭제')) act($('token-delete'), () => call('token', {delete: true}), () => '봇 토큰을 삭제했습니다.'); });
  $('app-token-save').addEventListener('click', () => act($('app-token-save'), async () => { const data = await call('app-token', {value: $('app-token-value').value}); $('app-token-value').value = ''; return data; }, () => '앱 수준 토큰을 저장했습니다. 리스너를 다시 시작하면 Socket Mode로 연결합니다.'));
  $('app-token-delete').addEventListener('click', async () => { if (await ask('슬랙 앱 수준 토큰을 삭제할까요?', '삭제하면 Socket Mode 연결을 쓸 수 없고, 리스너는 조회 주기대로 확인합니다.', '앱 토큰 삭제')) act($('app-token-delete'), () => call('app-token', {delete: true}), () => '앱 수준 토큰을 삭제했습니다.'); });
  $('settings-form').addEventListener('submit', event => { event.preventDefault(); act($('settings-save'), () => call('save', {settings: {
    enabled: $('enabled').checked, notify: $('notify').checked, socket_mode: $('socket-mode').checked, channel_id: $('channel-id').value.trim(), allowed_user_ids: ids($('allowed-users').value), allowed_bot_ids: ids($('allowed-bots').value),
    prefix: $('prefix').value.trim(), poll_seconds: Number($('poll-seconds').value),
    commands: Object.fromEntries([...document.querySelectorAll('[data-command]')].map(box => [box.dataset.command, box.checked])),
    bot_commands: Object.fromEntries([...document.querySelectorAll('[data-bot-command]')].map(box => [box.dataset.botCommand, box.checked])),
  }}), () => '설정을 저장했습니다.'); });
  $('connection-test').addEventListener('click', () => act($('connection-test'), () => call('test', {}), data => {
    const c = data.check; const channel = c.channel ? ` · 채널 #${c.channel.name}${c.channel.is_member ? '' : ' (봇이 아직 채널에 없습니다. /invite로 초대해주세요)'}` : ' · 채널 ID를 아직 저장하지 않았습니다';
    return `연결됨: 워크스페이스 ${c.team}, 봇 ${c.bot_user}${channel}`; }));
  $('listener-start').addEventListener('click', () => act($('listener-start'), () => call('listener', {action: 'start'}), () => '리스너를 시작했습니다.'));
  $('listener-stop').addEventListener('click', () => act($('listener-stop'), () => call('listener', {action: 'stop'}), () => '리스너를 중지했습니다.'));
  $('listener-refresh').addEventListener('click', load);
  load();
})();
