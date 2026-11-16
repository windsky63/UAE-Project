// Exercise the built UI against mocked APIs; never export real project data.
const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const http=require('http'),fs=require('fs'),path=require('path');
const dist=path.resolve(__dirname,'dist');
const mime={'.html':'text/html','.js':'application/javascript','.css':'text/css','.woff2':'font/woff2'};
const server=http.createServer((req,res)=>{
  const name=decodeURIComponent(new URL(req.url,'http://localhost').pathname);
  const file=path.resolve(dist,'.'+(name==='/'?'/index.html':name));
  if(!file.startsWith(dist+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return}
  res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');
  res.end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[],actions=[],downloads=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('download',d=>downloads.push(d));
    let submitted=false,polls=0;
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      let body={};
      if(url.pathname==='/api/overview')body={tools:[{id:'sync',name:'FusionLive 同步工具',state:'stopped',actions:['start','stop','check','generate-text','export-ledger']},{id:'analysis',name:'资料解析及管理工具',state:'stopped',actions:['start','stop','check','analysis','stop-analysis','briefing','clean-analysis-cache']}]};
      else if(url.pathname==='/api/tasks')body={items:submitted&&url.searchParams.get('tool')!=='analysis'?[{id:'export-test',state:++polls===1?'running':'succeeded'}]:[]};
      else if(url.pathname==='/api/action'){
        actions.push(route.request().postDataJSON());submitted=true;body={id:'export-test',state:'running'};
      }else body={items:[]};
      return route.fulfill({json:body});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#sync`);
    await expect(page.getByRole('listitem').filter({hasText:'文件资料'})).toHaveCount(0);
    const button=page.getByRole('button',{name:'导出文件台账',exact:true});
    await expect(button).toBeVisible();
    await expect(page.getByLabel('任务操作',{exact:true}).locator('.v-card')).toHaveCount(0);
    await button.click();
    await expect(button).toBeDisabled();
    await expect(page.getByLabel('文件台账导出')).toContainText('文件台账已在原导出目录重新生成');
    await expect(button).toBeEnabled();
    expect(actions).toEqual([{tool:'sync',action:'export-ledger'}]);
    await expect(page.getByRole('button',{name:'导出文件台账',exact:true})).toHaveCount(1);
    expect(page.url()).toContain('#sync');expect(await page.title()).toBeTruthy();
    await page.screenshot({path:path.resolve(__dirname,'../../exports/ledger-export-sync.png'),fullPage:true});
    expect(downloads).toHaveLength(0);
    await page.setViewportSize({width:390,height:844});
    await expect(button).toBeVisible();
    await page.setViewportSize({width:1440,height:1000});
    await page.goto(`http://127.0.0.1:${server.address().port}/#analysis`);
    await expect(page.getByRole('heading',{name:'任务操作',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'导出文件台账',exact:true})).toHaveCount(0);
    const operationCard=page.locator('.v-card').filter({has:page.getByRole('heading',{name:'任务操作',exact:true})});
    const names=await operationCard.getByRole('button').allTextContents();
    expect(names.length).toBeGreaterThan(0);expect(names.every(name=>name.trim().length>0)).toBeTruthy();
    await page.screenshot({path:path.resolve(__dirname,'../../exports/analysis-actions-clean.png'),fullPage:true});
    expect(errors).toEqual([]);
    console.log('PASS: removed file navigation; sync export button, progress and completion; no browser download; desktop/mobile');
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(error=>{console.error(error);server.close();process.exitCode=1});
