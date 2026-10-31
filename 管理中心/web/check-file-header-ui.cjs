const { chromium } = require('playwright');
const { expect } = require('@playwright/test');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', route => {
      const isFiles = new URL(route.request().url()).pathname === '/api/files';
      return route.fulfill({ json: isFiles ? {
        total: 20,
        items: Array.from({ length: 20 }, (_, i) => ({ id: String(i), name: `文件-${i}.txt`, state: 'ready' })),
      } : { tools: [], tasks: [] } });
    });
    await page.goto(process.env.FILE_HEADER_UI_URL || 'http://127.0.0.1:5178/#files');
    await expect(page.locator('.file-grid tbody tr')).toHaveCount(20);
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      const wrapper = page.locator('.file-grid .v-table__wrapper');
      await wrapper.evaluate(el => { el.scrollTop = 0; el.scrollLeft = 0; });
      const headers = page.locator('.file-grid thead th');
      const initial = await headers.evaluateAll(elements => elements.map(el => el.getBoundingClientRect().y));
      await wrapper.evaluate(el => { el.scrollTop = 500; });
      const scrolled = await headers.evaluateAll(elements => elements.map(el => el.getBoundingClientRect().y));
      initial.forEach((y, i) => expect(scrolled[i], `header ${i} stays at the top at width ${width}`).toBeCloseTo(y, 0));
      if (width === 1440) {
        const name = page.locator('.file-grid thead .file-pinned-name');
        const actions = page.locator('.file-grid thead .file-pinned-actions');
        const beforeName = await name.boundingBox();
        const beforeActions = await actions.boundingBox();
        await wrapper.evaluate(el => { el.scrollLeft = 700; });
        const afterName = await name.boundingBox();
        const afterActions = await actions.boundingBox();
        expect(afterName.x).toBeCloseTo(beforeName.x, 0);
        expect(afterActions.x).toBeCloseTo(beforeActions.x, 0);
        expect(afterName.y).toBeCloseTo(beforeName.y, 0);
        expect(afterActions.y).toBeCloseTo(beforeActions.y, 0);
        for (const header of [name, actions]) {
          expect(await header.evaluate(el => {
            const rect = el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
          })).toBe(true);
        }
      }
    }
    expect(errors).toEqual([]);
    console.log('PASS: all headers stay fixed during vertical scrolling; name/actions stay pinned during horizontal scrolling; desktop/mobile');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
