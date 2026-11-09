// Built-UI regression with isolated API data; never calls project services.
const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const http=require('http'),fs=require('fs'),path=require('path');
const dist=path.resolve(__dirname,'dist');
const screenshots=path.resolve(__dirname,'../../runtime/test-reports/overview-ui-screenshots');
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
    fs.mkdirSync(screenshots,{recursive:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    page.on('console',message=>{if(['error','warning'].includes(message.type()))errors.push(message.text())});
    const requests=[];
    const tools=[['sync','FusionLive 同步工具'],['nas','绿联云上传工具'],['wechat','微信与 Codex'],['ima','ima 知识库']].map(([id,name])=>({id,name,state:id==='sync'?'running':'stopped',icon:'mdi-tools',description:'测试工具说明',schedule:'按配置运行',actions:['start','stop']}));
    const info={time:'2026-11-02T15:00:00+08:00',tools,tasks:[],downloads:{done:10577},
      analysis:{done:5695,pending:1279,running:1,failed:1},local_downloads:true,
      file_inventory:{total:{files:19538},downloads:{files:14358},history:{files:5180},pending_publication:{files:222},changing:false}};
    await page.route('**/api/**',route=>{
      if(new URL(route.request().url()).pathname==='/api/actions/all'){
        const payload=route.request().postDataJSON();requests.push(payload);
        return route.fulfill({json:{items:payload.tools.map(tool=>({tool,state:'submitted'}))}});
      }
      return route.fulfill({json:info});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#overview`);
    const cards=page.locator('.overview-grid .metric-card');
    await expect(cards).toHaveCount(3);
    await expect(page).toHaveTitle(/管理中心/);
    await expect(cards.nth(0)).toContainText('1 / 4');
    await expect(page.locator('.section-subtitle').first()).toContainText('4 个工具');
    await expect(cards.nth(1)).toContainText('本地文件总量');
    await expect(cards.nth(1)).toContainText('19,538');
    await expect(cards.nth(1)).toContainText('待发布 222');
    await expect(cards.nth(1)).not.toContainText('历史下载版本');
    await expect(cards.nth(2)).toContainText('处理中 1');
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1000});
      const boxes=await cards.evaluateAll(nodes=>nodes.map(node=>{
        const box=node.getBoundingClientRect();return {height:box.height,width:box.width};
      }));
      expect(Math.max(...boxes.map(box=>box.height))-Math.min(...boxes.map(box=>box.height))).toBeLessThanOrEqual(1);
      expect(await cards.locator('.metric-note').count()).toBe(3);
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
      expect(await page.locator('.page-heading').evaluate(el=>{
        const identity=el.querySelector('.page-identity').getBoundingClientRect();
        const actions=el.querySelector(':scope > .d-flex').getBoundingClientRect();
        return identity.right<=actions.left||identity.bottom<=actions.top;
      })).toBe(true);
      await page.getByLabel('查看正式目录、历史目录和待发布文件说明',{exact:true}).focus();
      const tooltip=page.getByRole('tooltip').locator('.v-overlay__content');
      await expect(tooltip).toBeVisible();
      await expect(tooltip).toContainText('待发布缓存 222');
      await expect(tooltip).toContainText('历史目录 5,180');
      await expect(tooltip).toContainText('不定时轮询');
      await expect(tooltip).toContainText('14,358');
      await page.screenshot({path:path.join(screenshots,`overview-${width}.png`),fullPage:true});
      await page.locator('h1').click();
      await expect(tooltip).not.toBeVisible();
    }
    info.file_inventory.downloads.files++;
    info.file_inventory.total.files++;
    await expect(cards.nth(1)).toContainText('19,538');
    await page.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(cards.nth(1)).toContainText('19,539');
    await expect(cards.nth(1)).toContainText('正式 14,359');
    const dialog=page.getByRole('dialog');
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1000});
      await page.getByRole('button',{name:'一键启动工具',exact:true}).click();
      await expect(dialog).toContainText('已选 4 / 4 个工具');
      await expect(dialog.getByRole('checkbox',{name:'全选工具',exact:true})).toBeChecked();
      await dialog.getByRole('checkbox',{name:'ima 知识库'}).uncheck();
      await expect(dialog).toContainText('已选 3 / 4 个工具');
      await expect(dialog.getByRole('checkbox',{name:'全选工具',exact:true})).toHaveJSProperty('indeterminate',true);
      await dialog.getByRole('button',{name:'取消',exact:true}).click();
      expect(requests).toHaveLength(0);
      await page.getByRole('button',{name:'一键安全关闭工具',exact:true}).click();
      await expect(dialog).toContainText('已选 4 / 4 个工具');
      await dialog.getByRole('checkbox',{name:'全选工具',exact:true}).uncheck();
      await expect(dialog.getByRole('button',{name:'安全关闭所选工具',exact:true})).toBeDisabled();
      await expect(dialog).toContainText('请至少选择一个工具');
      await dialog.getByRole('checkbox',{name:'绿联云上传工具'}).check();
      await expect(dialog).toContainText('已选 1 / 4 个工具');
      expect(await dialog.evaluate(el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})).toBe(true);
      await dialog.screenshot({path:path.join(screenshots,`tool-selection-${width}.png`)});
      await page.keyboard.press('Escape');
      await expect(dialog).not.toBeVisible();
    }
    for(const [action,entry,submit,selected] of [
      ['start','一键启动工具','启动所选工具',['sync','wechat']],
      ['stop','一键安全关闭工具','安全关闭所选工具',['nas','ima']]
    ]){
      await page.getByRole('button',{name:entry,exact:true}).click();
      await dialog.getByRole('checkbox',{name:'全选工具',exact:true}).uncheck();
      for(const id of selected)await dialog.getByRole('checkbox',{name:tools.find(t=>t.id===id).name}).check();
      await dialog.getByRole('button',{name:submit,exact:true}).click();
      await expect(dialog).not.toBeVisible();
      expect(requests.at(-1)).toEqual({action,tools:selected});
      await expect(page.locator('.v-alert').first()).toContainText('请求已提交');
    }
    info.tools.push({...tools[1],id:'extra',name:'新增工具'});
    await page.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(cards.nth(0)).toContainText('1 / 5');
    await page.getByRole('button',{name:'一键启动工具',exact:true}).click();
    await expect(dialog).toContainText('已选 5 / 5 个工具');
    await dialog.getByRole('button',{name:'取消',exact:true}).click();
    expect(errors).toEqual([]);
    console.log('PASS: snapshot totals, dynamic tool counts, default selection, empty selection, cancel, selected start/stop payloads and layouts at 1440/768/390px');
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(error=>{console.error(error);server.close();process.exitCode=1});
