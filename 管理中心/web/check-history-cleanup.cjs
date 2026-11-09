// Isolated UI proof: all actions are mocked; no real cleanup or services run.
const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const http=require('http'),fs=require('fs'),path=require('path'),os=require('os');
const dist=path.resolve(__dirname,'dist');
const screenshots=fs.mkdtempSync(path.join(os.tmpdir(),'history-cleanup-ui-'));
const server=http.createServer((req,res)=>{
  const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);
  const file=path.resolve(dist,'.'+(pathname==='/'?'/index.html':pathname));
  if(!file.startsWith(dist+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return}
  const mime={'.html':'text/html','.js':'application/javascript','.css':'text/css','.woff2':'font/woff2'};
  res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');res.end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const errors=[],actions=[],expectedErrors=[];
  let status={state:'idle',manual_only:true},sequence=0,cacheTask=null,cacheBlocked=false;
  const info={tools:[{id:'sync',name:'FusionLive 同步工具',state:'stopped',schedule:'每天 02:00—10:30',description:'平台资料下载与解析',actions:['start','stop','check','clean-history','stop-history-cleanup','clean-analysis-cache']}],tasks:[],downloads:{done:15027},analysis:{done:0},file_inventory:null};
  async function prepare(page){
    page.on('pageerror',error=>errors.push(error.message));
    page.on('console',message=>{
      if(!['error','warning'].includes(message.type()))return;
      if(cacheBlocked&&message.text().includes('409 (Conflict)')&&message.location().url.endsWith('/api/action'))expectedErrors.push(message.text());
      else errors.push(message.text());
    });
    await page.route('**/api/**',route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==='/api/history-cleanup')return route.fulfill({json:status});
      if(pathname==='/api/tasks')return route.fulfill({json:{items:cacheTask?[cacheTask]:[]}});
      if(pathname==='/api/action'){
        const body=route.request().postDataJSON();actions.push(body);
        if(body.action==='clean-analysis-cache'){
          if(cacheBlocked)return route.fulfill({status:409,json:{detail:'后台解析正在运行，请安全停止后再手动清理缓存。'}});
          cacheTask={id:'cache-job',tool:'sync',action:'clean-analysis-cache',state:'running',label:'清理旧解析缓存'};
          return route.fulfill({json:cacheTask});
        }
        if(body.action==='clean-history')status={job_id:'job-'+(++sequence),state:'running',phase:'核验保留副本',planned_files:40286,deleted_files:0,deleted_bytes:0,skipped_files:0};
        else status={...status,state:'stopped',message:'已安全停止'};
        return route.fulfill({json:{id:status.job_id,state:'running'}});
      }
      return route.fulfill({json:info});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#sync`);
    await expect(page).toHaveTitle(/管理中心/);
    await expect(page.getByRole('heading',{name:'历史文件清理',exact:true})).toBeVisible();
  }
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050}});
    await prepare(page);
    const panel=page.getByLabel('历史文件手动清理');
    await expect(panel).toContainText('仅手动执行');
    expect(actions).toHaveLength(0);
    await panel.getByRole('button',{name:'清理历史文件',exact:true}).click();
    await expect(panel.getByRole('button',{name:'清理历史文件',exact:true})).toBeDisabled();
    await expect(panel).toContainText('40,286');
    status={...status,deleted_files:2,deleted_bytes:20971520};
    await expect(panel).toContainText('20.0 MB');
    await page.screenshot({path:path.join(screenshots,'desktop-running.png'),fullPage:true});
    await panel.getByRole('button',{name:'停止清理',exact:true}).click();
    await expect(panel).toContainText('已停止');
    await expect(panel.getByRole('button',{name:'清理历史文件',exact:true})).toBeEnabled();
    expect(actions.map(x=>x.action)).toEqual(['clean-history','stop-history-cleanup']);
    await panel.getByRole('button',{name:'清理历史文件',exact:true}).click();
    await expect.poll(()=>sequence).toBe(2);
    status={...status,state:'succeeded',deleted_files:8,deleted_bytes:125829120,message:'手动清理完成；仍被引用或无法核验的文件保留。'};
    await expect(panel).toContainText('已完成');
    await expect(panel).toContainText('120.0 MB');
    await page.screenshot({path:path.join(screenshots,'desktop-completed.png'),fullPage:true});
    const cachePanel=page.getByLabel('旧解析缓存清理');
    await expect(cachePanel).toContainText('手动执行一次解析工作流中的旧任务校验');
    await expect(cachePanel.getByRole('button',{name:'清理旧解析缓存',exact:true})).toBeEnabled();
    expect(actions.filter(x=>x.action==='clean-analysis-cache')).toHaveLength(0);
    await cachePanel.getByRole('button',{name:'清理旧解析缓存',exact:true}).click();
    await expect(cachePanel.getByRole('button',{name:'清理旧解析缓存',exact:true})).toBeDisabled();
    await expect(cachePanel).toContainText('正在核对旧解析任务');
    cacheTask={...cacheTask,state:'succeeded'};
    await expect(cachePanel).toContainText('校验清理已完成');
    cacheBlocked=true;
    await cachePanel.getByRole('button',{name:'清理旧解析缓存',exact:true}).click();
    await expect(cachePanel).toContainText('后台解析正在运行');
    await expect(cachePanel.getByRole('button',{name:'清理旧解析缓存',exact:true})).toBeEnabled();
    await page.screenshot({path:path.join(screenshots,'desktop-cache-cleanup.png'),fullPage:true});
    const mobile=await browser.newPage({viewport:{width:390,height:1000}});
    await prepare(mobile);
    await expect(mobile.getByLabel('历史文件手动清理')).toContainText('120.0 MB');
    await expect(mobile.getByLabel('旧解析缓存清理')).toContainText('校验清理已完成');
    expect(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
    await mobile.screenshot({path:path.join(screenshots,'mobile-completed.png'),fullPage:true});
    expect(expectedErrors).toHaveLength(1); // The deliberately rejected busy-worker POST.
    expect(errors).toEqual([]);
    console.log(JSON.stringify({passed:true,flow:'sync -> manual cleanup -> progress -> safe stop -> completion',viewports:[1440,390],browser:'Browser plugin not available; Playwright/msedge',screenshots,actions}));
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(error=>{console.error(error);process.exitCode=1});
