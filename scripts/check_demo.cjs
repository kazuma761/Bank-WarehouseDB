// Drives /demo in headless Chromium against a running API (FakeLLM in the gate). Prints "DEMO OK" only if every check passes.
const { chromium } = require('playwright');
(async () => {
  const [base, key] = process.argv.slice(2);
  const b = await chromium.launch(); const pg = await b.newPage();
  const errors = []; pg.on('console', m => { if (m.type() === 'error') errors.push(m.text()); }); pg.on('pageerror', e => errors.push(String(e)));
  await pg.goto(base + '/demo'); await pg.fill('#key', key); await pg.dispatchEvent('#key', 'change'); await pg.waitForTimeout(300);
  const asked = [];
  for (const q of ['How many loans are in debt?', 'only 1997', 'statement for account 3 in December 1998']) {
    const resp = pg.waitForResponse(r => r.url().endsWith('/v1/ask'));
    await pg.fill('#q', q); await pg.click('#go'); asked.push(JSON.parse((await resp).request().postData()));
    await pg.waitForFunction(() => !document.querySelector('#go').disabled);
  }
  const cards = await pg.locator('.turn').allInnerTexts();
  const fail = [];
  if (cards.length !== 3) fail.push('cards=' + cards.length);
  if (new Set(asked.map(a => a.session_id)).size !== 1) fail.push('session_id not reused');
  if (!cards[1].includes('understood as: How many loans in debt were granted in 1997?')) fail.push('follow-up not rewritten');
  for (const c of cards) for (const want of ['route:', 'per-stage latency']) if (!c.includes(want)) fail.push('missing ' + want);
  if (!cards[0].includes('SELECT') || (await pg.locator('.turn >> nth=0 >> table').count()) !== 1) fail.push('statement card lacks SQL/table');
  if (errors.length) fail.push('console errors: ' + errors.join(' | '));
  await b.close();
  console.log(fail.length ? 'DEMO FAIL ' + fail.join('; ') : 'DEMO OK cards=3 session reused, follow-up rewritten');
  process.exit(fail.length ? 1 : 0);
})();
