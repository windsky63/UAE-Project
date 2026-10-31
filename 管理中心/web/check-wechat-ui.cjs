const { chromium } = require('playwright');
const { expect } = require('@playwright/test');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    let saved;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      let body = { items: [] };
      if (url.pathname === '/api/overview') body = { tools: [{ id: 'wechat', state: 'stopped', actions: [] }], tasks: [] };
      if (url.pathname === '/api/config') {
        if (route.request().method() === 'POST') saved = route.request().postDataJSON().data;
        body = { exists: !!saved, revision: saved ? 'saved' : '', data: saved || {
          chats: [], sync_sources: [], codex_command: ['codex'], codex_model: '', codex_timeout_seconds: 120,
          account_name: '', database_root: '', database_account: '', codex_enabled: false, allow_send: false,
        } };
      }
      await route.fulfill({ json: body });
    });
    await page.goto(process.env.WECHAT_UI_URL || 'http://127.0.0.1:5178/#wechat');
    await page.getByRole('button', { name: '配置设置', exact: true }).click();
    await expect(page.getByRole('heading', { name: '微信与 Codex 设置', exact: true })).toBeVisible();
    await expect(page.getByText('首次使用已载入默认配置。', { exact: false })).toBeVisible();
    await expect(page.getByText('尚未配置会话。', { exact: true })).toBeVisible();
    await page.getByLabel('微信显示名称', { exact: true }).fill('服务器测试账号');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect.poll(() => saved?.account_name).toBe('服务器测试账号');
    expect(saved.allow_send).toBe(false);
    expect(saved.codex_enabled).toBe(false);
    expect(saved.chats).toEqual([]);
    expect(errors).toEqual([]);
    console.log('PASS: first-use WeChat configuration renders and saves with replies/sending disabled');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
