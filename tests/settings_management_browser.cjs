'use strict';
// Shared settings screen in a real browser at 1280px and 390px.
// Usage: node tests/settings_management_browser.cjs BASE ROOT MOVE_PARENT PLAYWRIGHT_MODULE BROWSER OUT
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const USAGE = 'usage: node tests/settings_management_browser.cjs BASE_URL PROJECT_ROOT MOVE_PARENT PLAYWRIGHT_MODULE BROWSER_EXECUTABLE OUT_DIR\n' +
  'Checks shared defaults, override/inherit, stale-save conflict, shared MCP bind/disable, storage plan/apply/rollback,\n' +
  'keyboard operation and horizontal overflow at 1280px and 390px. Installs nothing; pass an existing Playwright module and browser.';
if (process.argv.includes('--help') || process.argv.length < 8) {
  console.log(USAGE);
  process.exit(process.argv.includes('--help') ? 0 : 2);
}
const [base, root, moveParent, modulePath, executable, out] = process.argv.slice(2);
const {chromium} = require(modulePath);

async function status(page, id, pattern) {
  await page.waitForFunction(([id, source]) => new RegExp(source).test(document.getElementById(id).textContent), [id, pattern.source]);
  return page.locator('#' + id).textContent();
}

async function waitOrigin(page, label, origin) {
  // The table re-renders after each save/apply; wait for the new origin, not for any reload.
  await page.waitForFunction(([label, origin]) => [...document.querySelectorAll('#effective-table tbody tr')]
    .some(row => row.querySelector('th').textContent === label && row.querySelectorAll('td')[1].textContent === origin), [label, origin]);
}

(async () => {
  const browser = await chromium.launch({executablePath: executable, headless: true});
  fs.mkdirSync(out, {recursive: true});
  const checks = [];
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      page.setDefaultTimeout(10000);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base + '/settings');
      await status(page, 'notice', /불러왔습니다/);
      if (width === 1280) assert.match(await page.locator('#connection-list').textContent(), /등록한 공통 연결이 없습니다/);

      // Shared default, then override with an explicit empty value, then return to inheritance.
      await page.locator('#global-model').fill('shared-model-' + width);
      // Saving reloads the effective table; wait for that reload, not just the status text, or a
      // late re-render can replace the controls this step is about to edit.
      const reloaded = page.waitForResponse(response => response.url().includes('/api/settings/effective'));
      await page.locator('#global-save').click();
      await status(page, 'global-status', /저장했습니다/);
      await reloaded;
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await waitOrigin(page, '모델', '공통값');
      const modelRow = page.locator('#effective-table tbody tr', {has: page.locator('th', {hasText: '모델'})});
      await modelRow.locator('select').selectOption('project');
      await modelRow.locator('input').fill('');
      await page.locator('#apply-global').focus();
      await page.keyboard.press('Enter');  // keyboard operation
      await status(page, 'effective-status', /새 프로젝트 프로필/);
      await waitOrigin(page, '모델', '프로젝트 지정');
      const again = page.locator('#effective-table tbody tr', {has: page.locator('th', {hasText: '모델'})});
      await again.locator('select').selectOption('inherit');
      await page.locator('#apply-global').click();
      await status(page, 'effective-status', /새 프로젝트 프로필/);
      await waitOrigin(page, '모델', '공통값');

      // A save from another screen makes this screen's next save a visible conflict.
      const other = await browser.newPage();
      await other.goto(base + '/settings');
      await status(other, 'notice', /불러왔습니다/);
      await other.locator('#global-effort').fill('high');
      await other.locator('#global-save').click();
      await status(other, 'global-status', /저장했습니다/);
      await other.close();
      await page.locator('#global-effort').fill('low');
      await page.locator('#global-save').click();
      assert.match(await status(page, 'global-status', /다른 화면/), /다른 화면/);
      await page.reload();
      await status(page, 'notice', /불러왔습니다/);

      // Shared MCP: register, bind narrowly, refuse a wider scope, disable for everyone.
      const id = 'gh' + width;
      await page.locator('#connection-register summary').click();
      await page.locator('#connection-id').fill(id);
      await page.locator('#connection-provider').fill('github');
      await page.locator('#connection-scopes').fill('repo-A');
      await page.locator('#connection-form button[type=submit]').click();
      await status(page, 'connection-status', /등록했습니다/);
      await page.locator('#bind-connection option[value="' + id + '"]').waitFor({state: 'attached'});
      await page.locator('#connection-bind summary').click();
      await page.locator('#bind-connection').selectOption(id);
      await page.locator('#bind-local').fill('github' + width);
      await page.locator('#bind-scopes').fill('repo-B');
      await page.locator('#bind-operations').fill('get_me');
      await page.locator('#bind-form button[type=submit]').click();
      assert.match(await status(page, 'connection-status', /허용한/), /허용한/);  // permission error
      await page.locator('#bind-scopes').fill('repo-A');
      await page.locator('#bind-form button[type=submit]').click();
      await status(page, 'connection-status', /지정했습니다/);
      await page.locator('#connection-list li', {hasText: id}).locator('button').click();
      await page.locator('dialog[open] [data-ok]').click();  // shared confirmation dialog
      await status(page, 'connection-status', /비활성화했습니다/);
      await page.waitForFunction(id => /비활성화됨/.test([...document.querySelectorAll('#connection-list li')].find(li => li.textContent.includes(id)).textContent), id);

      // Storage: a nested destination is refused before anything runs; a valid move is reversible.
      await page.locator('#move-destination').fill(path.join(root, 'src', 'config'));
      await page.locator('#move-plan').click();
      await status(page, 'move-status', /조치가 필요한/);
      assert.equal(await page.locator('#move-apply').isDisabled(), true);
      await page.locator('#move-recover summary').click();
      await page.locator('#recover-id').fill('0'.repeat(32));
      await page.locator('#recover-run').click();
      assert.match(await status(page, 'move-status', /찾을 수 없습니다/), /찾을 수 없습니다/);  // failed recovery
      await page.locator('#move-destination').fill(path.join(moveParent, 'moved-' + width));
      await page.locator('#move-plan').click();
      await status(page, 'move-status', /통과했습니다/);
      assert.match(await page.locator('#move-impact').textContent(), /원본은 그대로/);
      await page.locator('#move-apply').click();
      await status(page, 'move-status', /completed/);
      await page.waitForFunction(() => /external/.test(document.getElementById('location-details').textContent));
      await page.locator('#recover-action').selectOption('rollback');
      await page.locator('#recover-run').click();
      await status(page, 'move-status', /rolled_back/);

      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.deepEqual(errors, []);
      await page.screenshot({path: path.join(out, 'settings-' + width + '.png'), fullPage: true});
      checks.push({width, inherit: true, override: true, conflict: true, sharedBind: true, sharedDisable: true, permissionError: true, moveRefused: true, moveApplied: true, rollback: true, recoveryError: true, keyboard: true, overflow: false, pageErrors: errors});
      await page.close();
    }
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(out, 'browser.json'), JSON.stringify(checks, null, 2));
  }
  console.log(JSON.stringify(checks));
})().catch(error => { console.error(error); process.exitCode = 1; });
