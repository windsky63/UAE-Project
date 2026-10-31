const { chromium } = require('playwright');
const { expect } = require('@playwright/test');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (['error', 'warning'].includes(message.type())) errors.push(message.text()); });
    let saved;
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      let body = { items: [] };
      if (url.pathname === '/api/overview') body = { tools: [{ id: 'sync', state: 'stopped', actions: [] }], tasks: [] };
      if (url.pathname === '/api/codex/options') body = { workspace: 'project', model: 'test-model' };
      if (url.pathname === '/api/config') {
        if (route.request().method() === 'POST') saved = route.request().postDataJSON().data;
        body = { revision: 'fixture', data: saved || { briefing_title: '测试简报', prepare_time_beijing: '08:30', send_time_beijing: '09:00', recipients: [], publish_text: false, publish_briefing: false,
          analysis_poll_seconds: 5, analysis_idle_log_seconds: 30, analysis_idle_exit_seconds: 0, analysis_auto_idle_exit_seconds: 120 } };
      }
      await route.fulfill({ json: body });
    });
    await page.goto(process.env.CONTROLS_UI_URL || 'http://127.0.0.1:5178/#codex');
    await page.locator('.codex-session-view .v-field').click();
    await page.getByRole('option', { name: '已归档', exact: true }).click();
    await expect(page.locator('.codex-session-view')).toContainText('已归档');
    await expect(page.locator('.codex-session-rows')).toContainText('暂无归档对话');
    await page.locator('.codex-mode-select .v-field').click();
    await page.getByRole('option', { name: '编辑 · 本项目', exact: true }).click();
    await expect(page.locator('.codex-mode-select')).toContainText('编辑 · 本项目');
    await page.locator('.codex-model').click();
    await page.locator('.codex-settings .v-select .v-field').click();
    await page.getByRole('option', { name: '极高', exact: true }).click();
    await expect(page.locator('.codex-settings')).toContainText('极高');
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByLabel('发送消息', { exact: true })).toBeInViewport();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('listitem').filter({ hasText: 'FusionLive 同步' }).click();
    await page.getByRole('button', { name: '配置设置', exact: true }).click();
    await page.getByLabel('新文件扫描间隔', { exact: true }).fill('60');
    await page.getByLabel('空闲状态提示间隔', { exact: true }).fill('90');
    await page.getByLabel('手动启动空闲退出时间', { exact: true }).fill('300');
    const time = page.getByRole('textbox', { name: '简报生成时间（北京）', exact: true });
    await time.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.locator('.v-time-picker')).toBeVisible();
    // Escape and cancel must discard the picker's draft.
    await dialog.locator('input').nth(0).fill('23');
    await dialog.locator('input').nth(1).fill('45');
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await expect(time).toHaveValue('08:30');
    await time.focus();
    await time.press('Enter');
    await dialog.locator('input').nth(0).fill('23');
    await dialog.locator('input').nth(1).fill('45');
    await dialog.getByRole('button', { name: '确定', exact: true }).click();
    await expect(time).toHaveValue('23:45');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect.poll(() => saved?.prepare_time_beijing).toBe('23:45');
    expect(saved.send_time_beijing).toBe('09:00');
    expect(saved.analysis_poll_seconds).toBe(60);
    expect(saved.analysis_idle_log_seconds).toBe(90);
    expect(saved.analysis_idle_exit_seconds).toBe(300);
    await page.setViewportSize({ width: 390, height: 844 });
    await time.click();
    await expect(dialog).toBeInViewport();
    expect(await dialog.evaluate(el => el.getBoundingClientRect().right <= innerWidth)).toBe(true);
    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
    expect(errors).toEqual([]);
    console.log('PASS: Vuetify selects, time confirmation/cancel, saved HH:mm, keyboard and mobile layout');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
