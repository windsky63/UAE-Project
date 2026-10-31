const { chromium } = require('playwright');
const { expect } = require('@playwright/test');

(async () => {
  const target = process.env.LAN_UI_URL;
  if (!target) throw new Error('Set LAN_UI_URL to the Vite LAN address, including /#codex');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    let sent;
    page.on('pageerror', error => errors.push(error.message));
    // Only model/SSE traffic is simulated; the page runs on a real LAN HTTP origin.
    await page.addInitScript(() => { window.EventSource = class { close() {} }; });
    const session = { id: 'fixture', title: '测试对话', status: 'idle', mode: 'read-only', model: '', effort: 'medium', updated: new Date().toISOString() };
    await page.route('**/api/**', async route => {
      const { pathname } = new URL(route.request().url());
      let json = {};
      if (pathname === '/api/overview') json = { tools: [], tasks: [] };
      if (pathname === '/api/codex/options') json = { workspace: 'project', model: '' };
      if (pathname === '/api/codex/sessions') json = route.request().method() === 'POST' ? { session } : { items: [session] };
      if (pathname === '/api/codex/session') json = { session, messages: [], events: [], last_event_id: 0 };
      if (pathname === '/api/codex/send') sent = route.request().postDataJSON();
      await route.fulfill({ json });
    });
    await page.goto(target);
    expect(await page.evaluate(() => isSecureContext)).toBe(false);
    expect(await page.evaluate(() => typeof crypto.randomUUID)).toBe('undefined');
    await page.getByLabel('发送给 Codex 的消息', { exact: true }).fill('局域网发送测试');
    await page.getByLabel('发送消息', { exact: true }).click();
    await expect.poll(() => sent?.text).toBe('局域网发送测试');
    expect(sent.request_id).toMatch(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
    expect(errors).toEqual([]);
    console.log('PASS: Codex message submission on LAN HTTP without crypto.randomUUID; no model calls');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
