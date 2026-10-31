const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const fs=require('fs');
const path=require('path');

(async()=>{
  const session=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../runtime/transport-browser-session.json'),'utf8'));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(session.url);
    await page.goto(new URL('/#codex',session.url).href);
    await page.getByTestId('codex-panel').waitFor();
    await page.locator('.codex-model').click();
    await page.getByLabel('模型连接方式',{exact:true}).press('ArrowDown');
    await page.getByRole('option',{name:'HTTPS 流式（跳过 WebSocket）',exact:true}).click();
    await page.getByLabel('发送给 Codex 的消息').fill('reconnect fixture');
    await page.getByLabel('发送消息',{exact:true}).click();
    await expect(page.locator('.codex-context')).toContainText('Codex 正在重连');
    await expect(page.locator('.codex-error')).toHaveCount(0);
    await expect(page.locator('.codex-turn-status')).toContainText('已完成');
    await expect(page.locator('.codex-context')).not.toContainText('正在重连');
    await expect(page.locator('.codex-error')).toHaveCount(0);
    const data=await page.evaluate(async()=>{
      const id=localStorage.getItem('project-codex-session');
      return (await fetch('/api/codex/session?id='+id)).json();
    });
    expect(data.session.transport).toBe('https');
    expect(data.events.some(e=>e.kind==='connection_status')).toBe(true);
    expect(data.events.some(e=>e.kind==='error')).toBe(false);
    await page.reload();
    await page.getByTestId('codex-panel').waitFor();
    await page.locator('.codex-model').click();
    await expect(page.locator('.codex-settings')).toContainText('HTTPS 流式');
    await page.setViewportSize({width:390,height:844});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    console.log(JSON.stringify({passed:true,checks:['transport persisted across reload','retry shown as connection status','no retry error banner','recovery clears reconnect status','mobile layout'],model_calls:0}));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
