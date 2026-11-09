// Built frontend with isolated APIs; never starts tasks or sends messages.
const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const http=require('http'),fs=require('fs'),path=require('path');
const dist=path.resolve(__dirname,'dist');
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
    const page=await browser.newPage({viewport:{width:1440,height:1100}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let settings={prepare_time_beijing:'10:40',send_time_beijing:'11:00',briefing_title:'测试简报',
      recipients:[],publish_text:false,publish_briefing:false,analysis_poll_seconds:5,
      analysis_idle_log_seconds:30,analysis_idle_exit_seconds:0,analysis_auto_idle_exit_seconds:120};
    const actions=[];
    await page.route('**/api/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/overview')return route.fulfill({json:{tools:[{id:'sync',name:'FusionLive 同步',state:'stopped',installed:true,actions:[]}],tasks:[]}});
      if(url.pathname==='/api/config'){
        if(route.request().method()==='POST'){
          const body=route.request().postDataJSON();expect(body.tool).toBe('sync');settings={...body.data};
        }
        return route.fulfill({json:{data:settings,revision:'fixture'}});
      }
      if(url.pathname==='/api/action')actions.push(route.request().postDataJSON());
      return route.fulfill({json:{items:[]}});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#sync`);
    await page.getByRole('button',{name:'配置设置',exact:true}).click();
    const text=page.getByRole('textbox',{name:'推送文发送时间（北京）',exact:true});
    const images=page.getByRole('textbox',{name:'管道简报发送时间（北京）',exact:true});
    await expect(text).toHaveValue('11:00');await expect(images).toHaveValue('11:00');
    async function pick(field,hour,minute){
      await field.click();const dialog=page.getByRole('dialog');
      await dialog.locator('input').nth(0).fill(hour);await dialog.locator('input').nth(1).fill(minute);
      await dialog.getByRole('button',{name:'确定',exact:true}).click();
    }
    await pick(text,'12','15');await pick(images,'12','30');
    await page.getByRole('button',{name:'保存配置',exact:true}).click();
    await expect.poll(()=>settings.text_send_time_beijing).toBe('12:15');
    expect(settings.briefing_send_time_beijing).toBe('12:30');
    await page.reload();await page.getByRole('button',{name:'配置设置',exact:true}).click();
    await expect(text).toHaveValue('12:15');await expect(images).toHaveValue('12:30');
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1100});
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
    }
    expect(actions).toEqual([]);expect(errors).toEqual([]);
    console.log('PASS: independent publication times, legacy inheritance, save/reload and responsive layout; no tasks sent');
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(error=>{console.error(error);server.close();process.exitCode=1});
