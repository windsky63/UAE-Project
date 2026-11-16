// Isolated built-UI regression: no real tools or cloud services are contacted.
const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const fs=require('fs'),path=require('path'),http=require('http'),os=require('os');
const dist=path.resolve(__dirname,'dist');
const output=path.join(os.tmpdir(),'project-feedback-ui');
const server=http.createServer((req,res)=>{
  const name=decodeURIComponent(new URL(req.url,'http://localhost').pathname);
  const file=path.resolve(dist,'.'+(name==='/'?'/index.html':name));
  if(!file.startsWith(dist+path.sep)||!fs.existsSync(file)){res.writeHead(404);return res.end()}
  res.setHeader('Content-Type',({'.html':'text/html','.js':'application/javascript','.css':'text/css','.woff2':'font/woff2'})[path.extname(file)]||'application/octet-stream');
  res.end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    fs.mkdirSync(output,{recursive:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('console',message=>{if(['error','warning'].includes(message.type()))errors.push(message.text())});
    let reject=false;
    await page.route('**/api/**',route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==='/api/action')return route.fulfill({json:reject?{detail:'测试失败，请检查配置后重试'}:{id:'fixture'},status:reject?400:200});
      if(pathname==='/api/overview')return route.fulfill({json:{time:'2026-11-12T15:00:00+08:00',tools:[{id:'sync',name:'FusionLive 同步工具',state:'stopped',actions:['start','stop'],description:'下载与后台解析',schedule:'按配置运行'}],tasks:[]}});
      return route.fulfill({json:{items:[],total:0}});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#overview`);
    await expect(page).toHaveTitle(/管理中心/);
    await expect(page.getByRole('heading',{name:'工作台',exact:true})).toBeVisible();
    const manage=page.getByRole('button',{name:'管理 FusionLive 同步工具',exact:true});
    await manage.focus();
    await expect(page.locator('.v-tooltip .v-overlay__content:visible')).toHaveText('管理 FusionLive 同步工具');
    await page.clock.install();
    await page.getByRole('button',{name:'模拟完整流程',exact:true}).click();
    const notices=page.locator('.ui-message-stack');
    await expect(notices).toContainText('已提交');
    await expect(notices.locator('.v-alert')).toHaveClass(/text-info/);
    await page.clock.fastForward(6500);
    await expect(notices.locator('.v-alert')).toHaveCount(0);
    reject=true;
    await page.getByRole('button',{name:'模拟完整流程',exact:true}).click();
    await expect(notices).toContainText('测试失败');
    await page.clock.fastForward(30000);
    await expect(notices).toContainText('请检查配置后重试');
    await page.clock.resume();
    for(const width of [1440,390]){
      await page.mouse.move(0,0);
      await expect(page.locator('.v-tooltip.v-overlay--active')).toHaveCount(0);
      await page.setViewportSize({width,height:1000});
      const close=notices.getByRole('button',{name:/关闭消息/});
      const alert=await notices.locator('.v-alert').boundingBox();
      const button=await close.boundingBox();
      expect(alert.x+alert.width-button.x-button.width).toBeLessThan(22);
      expect(button.y-alert.y).toBeLessThan(22);
      await close.hover();
      const tooltip=page.locator('.v-tooltip.v-overlay--active .v-overlay__content');
      await expect(tooltip).toContainText('关闭消息');
      await expect(tooltip).toHaveCSS('transform','none');
      // Make the tooltip hit-testable temporarily to verify actual paint order.
      expect(await tooltip.evaluate(el=>{
        const r=el.getBoundingClientRect(), message=document.querySelector('.ui-message').getBoundingClientRect();
        const left=Math.max(r.left,message.left),right=Math.min(r.right,message.right);
        const top=Math.max(r.top,message.top),bottom=Math.min(r.bottom,message.bottom);
        const x=left<right?(left+right)/2:(r.left+r.right)/2;
        const y=top<bottom?(top+bottom)/2:(r.top+r.bottom)/2;
        const old=el.style.pointerEvents;el.style.pointerEvents='auto';
        const above=el.contains(document.elementFromPoint(x,y));el.style.pointerEvents=old;
        return above;
      })).toBe(true);
      await page.screenshot({path:path.join(output,`message-close-tooltip-${width}.png`),animations:'disabled'});
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.screenshot({path:path.join(output,'top-error-desktop.png'),animations:'disabled'});
    await notices.getByRole('button',{name:/关闭消息/}).click();
    await expect(notices.locator('.v-alert')).toHaveCount(0);
    // Route changes keep session history and auxiliary buttons retain accessible names.
    await manage.click();
    await expect(page.getByRole('heading',{name:'任务操作',exact:true})).toBeVisible();
    await page.getByRole('button',{name:/查看本次消息/}).click();
    const dialog=page.getByRole('dialog');
    await expect(dialog).toContainText('已提交');
    await expect(dialog).toContainText('测试失败');
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});
      await expect(dialog).toBeVisible();
      await expect(dialog.locator('.v-overlay__content')).toHaveCSS('transform','none');
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
      await page.screenshot({path:path.join(output,`message-history-${width}.png`),animations:'disabled'});
    }
    await dialog.getByRole('button',{name:'清空本次消息',exact:true}).click();
    await expect(dialog).toContainText('本次会话暂无消息');
    await dialog.getByRole('button',{name:'关闭',exact:true}).click();
    await page.reload();
    await expect(page.getByRole('button',{name:'查看本次消息（0）',exact:true})).toBeVisible();
    // The deliberately rejected action produces one browser HTTP error; no app errors.
    expect(errors).toEqual(['Failed to load resource: the server responded with a status of 400 (Bad Request)']);
    console.log('PASS: icon focus tooltip, submission vs completion, transient info, persistent error, dismiss, cross-page session history, clear, reload and desktop/mobile layouts. '+output);
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(error=>{console.error(error);server.close();process.exitCode=1});
