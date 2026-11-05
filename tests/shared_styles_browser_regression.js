'use strict';
// Optional installed-browser regression. The caller supplies its installed module and browser.
const assert = require('assert'), fs = require('fs');
const {chromium} = require(process.argv[3]);
(async () => {
  const browser = await chromium.launch({executablePath: process.argv[4], headless: true});
  try {
    const page = await browser.newPage();
    let views = 0;
    const measurements = [];
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 900});
      let reference;
      for (const route of ['studio', 'setup', 'agent', 'dashboard', 'workflows', 'integrations', 'references', 'source']) {
        const response = await page.goto(process.argv[2] + '/' + route);
        assert.equal(response.status(), 200);
        await page.locator('#app-menu-toggle').click();
        const menu = await page.locator('#app-menu').evaluate(panel => {
          const styles = element => {
            const css = getComputedStyle(element), rect = element.getBoundingClientRect();
            return Object.fromEntries(['padding','margin','border','font','color','backgroundColor','display','gap','flexWrap','flexDirection'].map(key => [key,css[key]]).concat([['width',rect.width],['height',rect.height]]));
          };
          return {
            panel: styles(panel), nav: styles(panel.querySelector('nav')),
            heading: styles(panel.querySelector('.app-menu-heading')),
            close: styles(panel.querySelector('.app-menu-close')),
            link: styles(panel.querySelector('nav a:not([aria-current])')),
            title: styles(panel.querySelector('nav a:not([aria-current]) span')),
            description: styles(panel.querySelector('nav a:not([aria-current]) small')),
            current: styles(panel.querySelector('nav a[aria-current]'))
          };
        });
        // Link text differs per page, so compare appearance rather than text-dependent dimensions.
        for (const key of ['link', 'title', 'description', 'current']) {
          delete menu[key].width; delete menu[key].height;
        }
        if (!reference) reference = menu;
        assert.deepStrictEqual(menu, reference, route + ' drawer must match Studio at ' + width + 'px');
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        await page.keyboard.press('Escape');
        measurements.push({route, width, ...menu});
        views++;
      }
    }
    if (process.argv[5]) fs.writeFileSync(process.argv[5], JSON.stringify(measurements, null, 2));
    console.log(JSON.stringify({views, drawerStyles: 'identical', overflow: 0}));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
