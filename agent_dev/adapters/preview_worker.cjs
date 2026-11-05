// Fixed capture worker for registered previews. Reads one JSON request on stdin,
// opens an isolated browser context limited to the registered origins, takes a
// screenshot and returns safe element metadata. It accepts no script or HTML.
'use strict';
const fs = require('node:fs');

function reply(value, code = 0) {
  process.stdout.write(JSON.stringify(value) + '\n');
  process.exitCode = code;
}

(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const {chromium} = require(input.module);
  const allowed = new Set([input.origin, ...(input.allowed_origins || [])]);
  const blocked = new Set();
  let popups = 0;
  const browser = await chromium.launch({executablePath: input.executable, headless: true});
  try {
    const context = await browser.newContext({
      viewport: input.viewport,
      serviceWorkers: 'block',
      acceptDownloads: false,
      storageState: undefined,
    });
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.protocol === 'data:' || url.protocol === 'blob:' || allowed.has(url.origin)) return route.continue();
      blocked.add(url.origin);
      return route.abort();
    });
    const page = await context.newPage();
    context.on('page', other => { if (other !== page) { popups += 1; other.close().catch(() => {}); } });
    page.on('download', download => download.cancel().catch(() => {}));
    await page.goto(input.url, {waitUntil: 'load', timeout: input.timeout_ms});
    if (new URL(page.url()).origin !== input.origin) {
      reply({error: '등록된 origin 밖으로 이동했습니다.'}, 2);
      return;
    }
    const elements = await page.evaluate(limit => {
      const selector = '[data-testid],button,a[href],input,select,textarea,h1,h2,h3,[role]';
      const path = el => {
        const parts = [];
        for (let node = el; node && node.nodeType === 1 && parts.length < 8; node = node.parentElement) {
          let index = 1;
          for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) if (sib.tagName === node.tagName) index += 1;
          parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
        }
        return parts.join(' > ');
      };
      const label = el => {
        if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
          return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '';
        }
        return (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
      };
      return [...document.querySelectorAll(selector)].slice(0, limit).flatMap(el => {
        const rect = el.getBoundingClientRect();
        if (!rect.width || !rect.height) return [];
        return [{
          testid: el.getAttribute('data-testid') || '',
          tag: el.tagName.toLowerCase(),
          role: el.getAttribute('role') || '',
          text: el instanceof HTMLInputElement && el.type === 'password' ? '' : label(el).slice(0, 120),
          path: path(el),
          rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        }];
      });
    }, input.max_elements);
    await page.screenshot({path: input.screenshot, fullPage: false});
    reply({final_url: page.url(), elements, blocked: [...blocked], popups});
  } finally {
    await browser.close();
  }
})().catch(error => reply({error: String(error && error.message || error).slice(0, 300)}, 1));
