// Real browser check of the session flow: private file -> /launch -> page (see test_web_session.py).
const [base, fileUrl, otherBase, modulePath, executablePath] = process.argv.slice(2);
const { chromium } = require(modulePath);
const assert = require('assert');

(async () => {
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const port = new URL(base).port;
    const context = await browser.newContext();
    const page = await context.newPage();

    // A typed address or a bookmark: no session yet.
    let response = await page.goto(base + '/setup');
    assert.strictEqual(response.status(), 403);
    assert.ok((await page.textContent('h1')).includes('다시 열어주세요'));

    // What the app does: open the private file, which forwards to /launch, which forwards to the page.
    await page.goto(fileUrl);
    await page.waitForURL(base + '/setup', { timeout: 15000 });
    assert.ok(await page.title());
    const status = await page.evaluate(async () => (await fetch('/api/setup/bootstrap')).status);
    assert.strictEqual(status, 200);

    const cookie = (await context.cookies(base)).find(item => item.name === 'devroster_session_' + port);
    assert.ok(cookie, 'session cookie missing');
    assert.strictEqual(cookie.httpOnly, true);
    assert.strictEqual(cookie.sameSite, 'Strict');
    assert.ok(!(await page.evaluate(() => document.cookie)).includes('devroster_session'));

    // The address bar no longer holds the key, and the key does not work a second time.
    assert.ok(!page.url().includes('key='));

    // A server the same app opened for another project accepts this browser.
    response = await page.goto(otherBase + '/setup');
    assert.strictEqual(response.status(), 200);

    // localhost is another site for the browser: the cookie is not sent there.
    response = await page.goto(`http://localhost:${port}/setup`);
    assert.strictEqual(response.status(), 403);

    // Another browser profile has nothing.
    const stranger = await (await browser.newContext()).newPage();
    response = await stranger.goto(base + '/api/setup/bootstrap');
    assert.strictEqual(response.status(), 403);
    // The server removed the private file when the key was used: nothing left to open or read.
    await assert.rejects(stranger.goto(fileUrl), /ERR_FILE_NOT_FOUND/);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
