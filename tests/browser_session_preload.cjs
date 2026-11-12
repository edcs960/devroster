// Loaded into every node process the tests start (NODE_OPTIONS in conftest.py).
// The local screens only answer a browser that holds the app's session cookie; a browser
// test starts its own Chromium, so each new context gets the cookie of the test process.
const Module = require('module');
const secret = process.env.DEVROSTER_TEST_SESSION;
if (secret) {
  const cookies = ['127.0.0.1', 'localhost'].map(domain => ({
    name: 'devroster_session_test', value: secret, domain, path: '/', httpOnly: true, sameSite: 'Strict',
  }));
  const wrapped = new WeakSet();
  const withSession = browser => {
    const newContext = browser.newContext.bind(browser);
    browser.newContext = async options => {
      const context = await newContext(options);
      await context.addCookies(cookies);
      return context;
    };
    browser.newPage = async options => (await browser.newContext(options)).newPage();
    return browser;
  };
  const load = Module._load;
  Module._load = function (...args) {
    const exported = load.apply(this, args);
    try {
      const type = exported && typeof exported === 'object' && exported.chromium;
      if (type && typeof type.launch === 'function' && !wrapped.has(type)) {
        wrapped.add(type);
        const launch = type.launch.bind(type);
        type.launch = async options => withSession(await launch(options));
      }
    } catch (error) { /* not a browser module */ }
    return exported;
  };
}
