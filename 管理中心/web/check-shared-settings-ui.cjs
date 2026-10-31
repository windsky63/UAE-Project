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
    await page.goto(new URL('/#wechat',session.url).href);
    await page.getByRole('button',{name:'配置设置',exact:true}).click();
    await expect(page.getByLabel('Codex 可访问目录',{exact:true})).toBeDisabled();
    const original=await page.evaluate(async()=>(await fetch('/api/config?tool=wechat')).json());
    expect(original.data.codex_auto_access_dirs).toBe(true);
    const shared=original.data.codex_access_dirs.find(p=>p.endsWith('shared-documents'));
    expect(shared).toBeTruthy();
    await page.getByLabel('共享 Codex 连接方式',{exact:true}).press('ArrowDown');
    await page.getByRole('option',{name:'HTTPS 流式（跳过 WebSocket）',exact:true}).click();
    await page.getByLabel('自动包含同级目录',{exact:true}).uncheck({force:true});
    const directories=page.getByLabel('Codex 可访问目录',{exact:true});
    await expect(directories).toBeEnabled();
    const subset=path.join(shared,'subset');
    await directories.fill(subset);
    await directories.press('Enter');
    await page.getByRole('button',{name:'保存配置',exact:true}).click();
    await expect.poll(async()=>page.evaluate(async()=>(await(await fetch('/api/config?tool=wechat')).json()).data.codex_transport)).toBe('https');
    const saved=await page.evaluate(async()=>(await(await fetch('/api/config?tool=wechat')).json()).data);
    expect(saved.codex_auto_access_dirs).toBe(false);
    expect(saved.codex_access_dirs).toContain(subset);
    expect(saved.allow_send).toBe(false);
    expect(saved.codex_enabled).toBe(false);
    await page.getByRole('listitem').filter({hasText:'Codex 对话'}).click();
    await page.getByTestId('codex-panel').waitFor();
    await page.locator('.codex-model').click();
    await expect(page.locator('.codex-settings')).toContainText('继承微信与 Codex 设置');
    await page.locator('.codex-settings summary').click();
    await expect(page.locator('.codex-settings')).toContainText(subset);
    await page.getByLabel('发送给 Codex 的消息').fill('read fixture');
    await page.getByLabel('发送消息',{exact:true}).click();
    await expect(page.locator('.codex-turn-status')).toContainText('已完成');
    const data=await page.evaluate(async()=>{
      const id=localStorage.getItem('project-codex-session');
      return (await fetch('/api/codex/session?id='+id)).json();
    });
    expect(data.session.transport).toBe('inherit');
    const connected=data.events.findLast(e=>e.kind==='connected').payload;
    expect(connected.transport).toBe('https');
    expect(connected.access_directories).toEqual(saved.codex_access_dirs);
    await page.setViewportSize({width:390,height:844});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    console.log(JSON.stringify({passed:true,checks:['sibling directories visible','manual directories persisted','shared transport persisted','web conversation inherits shared HTTPS and directories','sending stays disabled','mobile layout'],model_calls:0,wechat_sent:0}));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
