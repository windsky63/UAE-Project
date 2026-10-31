const { chromium } = require('playwright');
const { expect } = require('@playwright/test');
const { spawn } = require('child_process');
const path = require('path');
const readline = require('readline');

(async () => {
  const child = spawn(path.resolve(__dirname, '../../.venv-analysis/Scripts/python.exe'),
    ['-B', '-u', path.resolve(__dirname, '../tests/run_log_fixture.py')],
    { windowsHide:true, env:{...process.env, PYTHONIOENCODING:'utf-8'}, stdio:['pipe','pipe','pipe'] });
  let diagnostics = '';
  child.stderr.on('data', chunk => diagnostics += chunk.toString());
  const lines = readline.createInterface({ input: child.stdout });
  const session = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('Fixture start timeout: ' + diagnostics)), 15000);
    lines.once('line', line => { clearTimeout(timer); resolve(JSON.parse(line)); });
    child.once('exit', code => { clearTimeout(timer); reject(Error(`Fixture exit ${code}: ${diagnostics}`)); });
  });
  let browser;
  try {
    browser = await chromium.launch({ channel:'msedge', headless:true });
    const page = await browser.newPage({ viewport:{width:1440,height:1050} });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(session.url);
    await page.goto(new URL('/#logs', session.url).href);
    await expect(page.getByText('OLD_LOG_FIXTURE', {exact:true})).toBeVisible();
    await page.getByRole('button', {name:'清除日志',exact:true}).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('仅清除今天之前的日志');
    await dialog.getByRole('button', {name:'取消',exact:true}).click();
    await expect(page.getByText('OLD_LOG_FIXTURE', {exact:true})).toBeVisible();
    await page.getByRole('button', {name:'清除日志',exact:true}).click();
    await dialog.getByRole('button', {name:'确认清除',exact:true}).click();
    await expect(page.getByText('OLD_LOG_FIXTURE', {exact:true})).toHaveCount(0);
    await expect(page.getByText('TODAY_LOG_FIXTURE', {exact:true})).toBeVisible();
    await expect(page.locator('[data-testid="logs-panel"]')).toContainText('已清除 1 个日志文件');
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('button', {name:'清除日志',exact:true}).click();
    await dialog.locator('div[role="combobox"]').click();
    await page.getByRole('option', {name:'清除全部日志，包含今天',exact:true}).click();
    await dialog.getByRole('button', {name:'确认清除',exact:true}).click();
    await expect(page.getByText('TODAY_LOG_FIXTURE', {exact:true})).toHaveCount(0);
    await expect(page.getByText('暂无匹配的日志。', {exact:true})).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    if (errors.length) throw Error(errors.join('\n'));
    console.log(JSON.stringify({passed:true, checks:['cancel preserves logs','default deletes historical logs','today preserved','all removes today','business state preserved by fixture','mobile layout','no page errors']}));
  } finally {
    if (browser) await browser.close();
    const ended = new Promise(resolve => child.once('exit', code => resolve(code)));
    child.stdin.end('stop\n');
    const code = await ended;
    if (code !== 0) throw Error(`Fixture exit ${code}: ${diagnostics}`);
    lines.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
