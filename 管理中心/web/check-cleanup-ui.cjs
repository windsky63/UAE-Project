const { chromium } = require('playwright');
const { expect } = require('@playwright/test');
const http = require('http');
const fs = require('fs');
const path = require('path');

(async () => {
  const root = path.join(__dirname, 'dist');
  const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
  const server = http.createServer((req, res) => {
    const file = path.resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
    if (file !== root && !file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
    const target = file === root ? path.join(root, 'index.html') : file;
    if (!fs.existsSync(target) || !fs.statSync(target).isFile()) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
    fs.createReadStream(target).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
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
        body = { exists: true, revision: 'fixture', data: saved || {
          chats: [], sync_sources: [], codex_command: ['codex'], account_name: 'server',
          codex_access_dirs: [], codex_auto_access_dirs: false, codex_backend: 'exec',
          allow_send: false, codex_enabled: false, file_send_cleanup_days: 2,
        } };
      }
      await route.fulfill({ json: body });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#wechat`);
    await page.getByRole('button', { name: '配置设置', exact: true }).click();
    const input = page.getByLabel('发送暂存文件清理间隔', { exact: true });
    await expect(input).toHaveValue('2');
    await input.fill('7');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect.poll(() => saved?.file_send_cleanup_days).toBe(7);
    expect(saved.allow_send).toBe(false);
    expect(saved.account_name).toBe('server');
    await page.reload();
    await page.getByRole('button', { name: '配置设置', exact: true }).click();
    await expect(input).toHaveValue('7');
    await input.fill('0');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect.poll(() => saved?.file_send_cleanup_days).toBe(0);
    expect(errors).toEqual([]);
    console.log('PASS: built frontend shows default 2, saves 7, reloads, and disables with 0; other settings preserved.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
