'use strict';
// Studio document preview shows Mermaid source with a copy button, without CSP violations.
// Usage: node tests/mermaid_view_browser.cjs PLAYWRIGHT_MODULE BROWSER BASE_URL PROJECT_ID
const assert = require('node:assert/strict');
const [modulePath, executable, base, project] = process.argv.slice(2);
const {chromium} = require(modulePath);

(async () => {
  const browser = await chromium.launch({executablePath: executable, headless: true});
  const checks = [];
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({viewport: {width, height: 900}});
      await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: base});
      const page = await context.newPage();
      page.setDefaultTimeout(10000);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => { if (/Content Security Policy/.test(message.text())) errors.push(message.text()); });
      await page.goto(base + '/studio?project_id=' + project);
      await page.waitForFunction(id => document.getElementById('projects').value === id, project);
      await page.locator('#steps button[data-step="spec"]').click();
      await page.locator('#documents button', {hasText: '시퀀스다이어그램'}).click();
      await page.locator('#document-toggle').click();
      const block = page.locator('#document-preview .mermaid-block');
      await block.waitFor();
      assert.equal(await block.count(), 1);
      assert.match(await block.locator('pre code').textContent(), /^sequenceDiagram\n {2}participant U as 사용자/);
      assert.equal(await page.locator('#document-preview svg').count(), 0, 'source only, no rendered SVG');
      await block.getByRole('button', {name: '코드 복사'}).click();
      await page.waitForFunction(() => navigator.clipboard.readText().then(text => text.startsWith('sequenceDiagram')));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'fits ' + width);
      assert.deepEqual(errors, []);
      checks.push({width, block: true, copy: true, overflow: false});
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(checks));
})().catch(error => { console.error(error); process.exitCode = 1; });
