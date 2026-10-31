const { chromium } = require('playwright');
const { expect } = require('@playwright/test');
const path = require('path');
const os = require('os');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()); });
    const name = '长文件名与文档编号-'.repeat(15) + '.txt';
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      let body = {};
      if (url.pathname === '/api/overview') body = { tools: [], tasks: [] };
      if (url.pathname === '/api/files') body = { total: 1, items: [{ id: 'fixture', name, state: 'ready', path: 'D:/' + 'long-directory/'.repeat(80), error: '详细信息\n'.repeat(60) }] };
      if (url.pathname === '/api/preview') body = { name, kind: 'text', text: '预览长行：' + 'x'.repeat(500) + '\n' + '文件内容\n'.repeat(200), truncated: true };
      await route.fulfill({ json: body });
    });
    await page.goto(process.env.FILE_DIALOG_UI_URL || 'http://127.0.0.1:5178/#files');
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      for (const preview of [false, true]) {
        if (preview) await page.locator('.file-grid button[aria-label^="查看 "]').click();
        else await page.locator('.file-name-link').click();
        const dialog = page.getByRole('dialog');
        const card = dialog.locator('.file-dialog-card');
        const body = dialog.locator('.file-dialog-body');
        const close = page.getByRole('button', { name: preview ? '关闭预览' : '关闭文件详情', exact: true });
        await expect(body).toBeVisible();
        await close.click({ trial: true });
        const before = await close.boundingBox();
        const metrics = await body.evaluate(el => {
          el.scrollTop = el.scrollHeight;
          return { scrollable: el.scrollHeight > el.clientHeight, scrolled: el.scrollTop > 0, horizontal: el.scrollWidth > el.clientWidth };
        });
        expect(metrics).toEqual({ scrollable: true, scrolled: true, horizontal: false });
        const after = await close.boundingBox();
        expect(after.y).toBeCloseTo(before.y, 0);
        await expect(close).toBeInViewport();
        expect(await card.evaluate(el => el.scrollTop)).toBe(0);
        const bounds = await card.boundingBox();
        expect(bounds.x).toBeGreaterThanOrEqual(0);
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
        await page.screenshot({ path: path.join(os.tmpdir(), `file-${preview ? 'preview' : 'details'}-${width}.png`) });
        await close.click();
        await expect(dialog).toBeHidden();
      }
    }
    await page.locator('.file-name-link').click();
    await page.getByRole('button', { name: '关闭文件详情', exact: true }).click({ trial: true });
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toBeHidden();
    expect(errors).toEqual([]);
    console.log('PASS: fixed close button, body-only scrolling, long filenames/content, desktop/mobile, Escape, no browser errors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
