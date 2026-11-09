// Built frontend with isolated API fixtures. Never starts real tools or contacts ima.
const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const http=require('http'),fs=require('fs'),path=require('path'),os=require('os');
const dist=path.resolve(__dirname,'dist');
const screenshots=process.env.IMA_UI_SCREENSHOTS||path.join(os.tmpdir(),'ima-ui-redesign-check');
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
    fs.mkdirSync(screenshots,{recursive:true});
    const page=await browser.newPage({viewport:{width:1440,height:1080}});
    const errors=[],actions=[],operations=[],recordRequests=[],clears=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('console',message=>{if(['error','warning'].includes(message.type()))errors.push(message.text())});
    const settings={source:'D:/资料/待同步',kb:'fake-kb',folder:'',recursive:true,collect_enabled:false,auto_sync_enabled:true,
      database_path:'D:/同步工具/runtime/ledger.sqlite3',downloads_path:'D:/同步工具/downloads',interval_seconds:60,sync_time:'13:00'};
    const planFiles=Array.from({length:625},(_,i)=>({path:'D:/资料/待同步/PP-'+String(i).padStart(4,'0')+'.pdf',
      name:'PP-'+String(i).padStart(4,'0')+'.pdf',relative:'项目管理规定/02_质量管理/PP-'+String(i).padStart(4,'0')+'.pdf',
      target_path:'项目管理规定/02_质量管理/PP-'+String(i).padStart(4,'0')+'.pdf',status:i===3?'cloud_existing':i%5===0?'ready_update':i%5===1?'unchanged':i%5===2?'conflict':'ready',reason:i%5===0?'核验新版后归档旧版':i%5===2?'云端已有同名文件，需核对':'根据本地文件生成的只读计划'}));
    const datasets={
      plan:planFiles, collection:Array.from({length:65},(_,i)=>({name:'提取-'+i+'.pdf',status:i%3===0?'error':'copied',reason:i%3===0?'文件缺失':'',record_source:'database',project:'p',file_id:'file-'+i})),
      tasks:Array.from({length:65},(_,i)=>({id:'old-'+i,label:'历史预览任务 '+i,action:'plan',state:i%2?'succeeded':'failed',started:'2026-11-04T19:00:00+08:00',exit_code:i%2?0:1,log:'task-'+i+'.log'})),
      history:Array.from({length:261},(_,i)=>({path:'历史文件-'+i+'.pdf',status:'done',updated:1793790000,note:'已确认入库'})),
      journal:Array.from({length:85},(_,i)=>({time:'2026-11-04 19:00:00',operation:i%2?'上传':'提取新增',path:'每日文件-'+i+'.pdf',status:'成功',reason:i%2?'目标=kb/根目录':'版本=abcdef'}))
    };
    const snapshot={data:settings,revision:'fixture-revision',credentials_configured:true,state:'stopped',tasks:[],
      journal:'',journal_path:'D:/资料/待同步/每日更新记录.txt',collection:{revision:'c1',updated_at:'2026-11-04T19:00:00+08:00',counts:{copied:43,error:22}},
      plan:{layout:'relative-tree-v1',files:planFiles.slice(0,20),total_files:625,counts:{ready:250,ready_update:125,unchanged:125,conflict:125},revision:'p1',updated_at:'2026-11-04T19:00:00+08:00',knowledge_base_id:'fake-kb',folder_id:'',source:settings.source}};
    let delayNextRecords=0,delayedResponseFinished=false;
    await page.route('**/api/**',async route=>{
      const url=new URL(route.request().url()),body=route.request().postDataJSON?.();
      if(url.pathname==='/api/overview')return route.fulfill({json:{time:'2026-11-04T19:00:00+08:00',tools:[],tasks:[]}});
      if(url.pathname==='/api/ima')return route.fulfill({json:snapshot});
      if(url.pathname==='/api/ima/records'){
        const kind=url.searchParams.get('kind'),needle=(url.searchParams.get('search')||'').toLowerCase(),state=url.searchParams.get('status');
        recordRequests.push(Object.fromEntries(url.searchParams));
        const items=(datasets[kind]||[]).filter(item=>(!state||(item.state||item.status)===state)&&(!needle||JSON.stringify(item).toLowerCase().includes(needle)));
        const limit=Number(url.searchParams.get('limit')||20),offset=Math.min(Number(url.searchParams.get('offset')||0),Math.max(0,Math.floor((items.length-1)/limit)*limit));
        const delay=delayNextRecords;delayNextRecords=0;
        if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
        await route.fulfill({json:{items:items.slice(offset,offset+limit),total:items.length,offset,limit}});
        if(delay)delayedResponseFinished=true;
        return;
      }
      if(url.pathname==='/api/action'){
        actions.push(body);
        const item={id:'submitted-'+actions.length,action:body.action,label:'预览上传计划',state:'running',started:'2026-11-04T19:50:00+08:00',exit_code:null,log:'latest-plan.log'};
        snapshot.tasks=[item];return route.fulfill({json:{id:item.id,state:'running'}});
      }
      if(url.pathname==='/api/config'){Object.assign(settings,body.data);return route.fulfill({json:{data:settings,revision:'saved-revision'}})}
      if(url.pathname==='/api/logs/clear'){clears.push(body);return route.fulfill({json:{deleted:2,freed_bytes:2048,kept:1,skipped:1,errors:[]}})}
      if(url.pathname==='/api/logs')return route.fulfill({json:{items:[{id:'log',level:'ERROR',message:'ima HTTP 403（接口 check_repeated_names）；ima 200001：请求频率超限'}],total:1}});
      if(url.pathname==='/api/ima/query'){
        operations.push(body.operation);
        const items=body.operation==='bases'?[{id:'fake-kb',name:'阿联酋项目'}]:body.operation==='scan'?
          Array.from({length:65},(_,i)=>({path:'D:/资料/待同步/local-'+i+'.pdf',relative:'local-'+i+'.pdf',name:'local-'+i+'.pdf',status:'ready'})):
          body.operation==='list'?[{media_id:'folder-a',title:'项目管理规定',media_type:99},...Array.from({length:66},(_,i)=>({media_id:'cloud-'+i,title:'云端文件-'+i+'.pdf',media_type:1}))]:[];
        return route.fulfill({json:{items}});
      }
      throw Error('Unexpected API: '+url.pathname);
    });
    const nav=page.getByRole('navigation',{name:'ima 功能分区'});
    const result=page.locator('.ima-results');
    const pager=()=>result.getByRole('navigation');
    const filter=()=>page.getByRole('textbox',{name:'搜索文件或记录',exact:true});
    await page.goto('http://127.0.0.1:'+server.address().port+'/#ima');
    await expect(page).toHaveTitle('阿联酋项目 · 工具管理中心');
    await expect(page.getByRole('heading',{name:'最近上传计划',exact:true})).toBeVisible();
    await expect(result.locator('tbody tr')).toHaveCount(20);
    await expect(result.getByRole('columnheader',{name:'云端目标路径（相对所选目录）',exact:true})).toBeVisible();
    await expect(result.locator('tbody tr').first().locator('td').nth(1)).toHaveText('项目管理规定/02_质量管理/PP-0000.pdf');
    await expect(result.locator('tbody tr').nth(3)).toContainText('云端已存在');
    await expect(result).toContainText('按本地相对目录逐层匹配云端');
    await expect(pager()).toContainText('共 625 项');
    expect(operations).toEqual([]); // Passive rendering/pagination must not contact ima.
    await pager().getByRole('button',{name:'最近上传计划第32页',exact:true}).click();
    await expect(result.locator('tbody tr')).toHaveCount(5);
    await expect(result).toContainText('PP-0624.pdf'); // Beyond old first-500 truncation.
    await filter().fill('PP-0624');
    await expect(result.locator('tbody tr')).toHaveCount(1);
    await expect(pager()).toContainText('共 1 项');
    await filter().fill('');
    await expect(result.locator('tbody tr')).toHaveCount(20);
    await page.getByLabel('最近上传计划每页条数').selectOption('50');
    await expect(result.locator('tbody tr')).toHaveCount(50);
    await page.getByLabel('最近上传计划每页条数').selectOption('20');
    await expect(result.locator('tbody tr')).toHaveCount(20);
    await page.locator('.ima-filters .v-select .v-field').click();
    await page.getByRole('option',{name:'冲突',exact:true}).click();
    await expect(pager()).toContainText('共 125 项');
    await expect(result.locator('tbody tr').first()).toContainText('冲突');
    // Capture canonical work screen before changing task state.
    await page.locator('.ima-filters .v-select .v-field').click();
    await page.getByRole('option',{name:'全部状态',exact:true}).click();
    await expect(pager()).toContainText('共 625 项');
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-work-desktop.png'),fullPage:true});
    await nav.getByRole('button',{name:'同步配置',exact:true}).click();
    await expect(page.getByRole('heading',{name:'同步配置',exact:true})).toBeVisible();
    await expect(page.getByRole('textbox',{name:'下载数据库路径',exact:true})).toHaveValue('D:/同步工具/runtime/ledger.sqlite3');
    await expect(page.getByRole('textbox',{name:'文件台账路径',exact:true})).toHaveCount(0);
    const automatic=page.getByLabel('允许自动同步',{exact:true});
    await automatic.uncheck();
    await nav.getByRole('button',{name:'同步工作区',exact:true}).click();
    await expect(page.getByText('配置尚未保存。',{exact:false})).toBeVisible();
    await expect(page.getByRole('button',{name:'预览上传计划',exact:true})).toBeDisabled();
    await nav.getByRole('button',{name:'同步配置',exact:true}).click();
    await page.getByRole('button',{name:'保存 ima 配置',exact:true}).click();
    await page.reload();
    await nav.getByRole('button',{name:'同步配置',exact:true}).click();
    await expect(automatic).not.toBeChecked();
    await expect(page.getByLabel('每日同步时间（北京时间）',{exact:true})).toBeDisabled();
    await page.getByRole('button',{name:'读取知识库',exact:true}).click();
    expect(operations).toEqual(['bases']);
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-settings-desktop.png'),fullPage:true});
    await nav.getByRole('button',{name:'同步工作区',exact:true}).click();
    await page.getByRole('tab',{name:'本地文件',exact:true}).click();
    await page.getByRole('button',{name:'扫描本地文件',exact:true}).click();
    await expect(pager()).toContainText('共 65 项');
    await pager().getByRole('button',{name:'下一页',exact:true}).click();
    await expect(result.locator('tbody tr').first()).toContainText('local-20.pdf');
    await page.getByRole('tab',{name:'提取结果',exact:true}).click();
    await expect(result).toContainText('不代表已上传 ima');
    await pager().getByRole('button',{name:'下一页',exact:true}).click();
    await expect(result.locator('tbody tr').first()).toContainText('提取-20.pdf');
    await expect(result.locator('tbody tr').first()).toContainText('内容ID file-20');
    await expect(result.locator('tbody tr').first()).not.toContainText('台账第');
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-database-collection.png'),fullPage:true});
    await nav.getByRole('button',{name:'云端内容',exact:true}).click();
    await page.getByRole('button',{name:'根目录',exact:true}).click();
    await expect(pager()).toContainText('共 67 项');
    await pager().getByRole('button',{name:'下一页',exact:true}).click();
    await expect(result.locator('tbody tr').first()).toContainText('云端文件-19.pdf');
    await page.getByRole('textbox',{name:'筛选已读取的文件',exact:true}).fill('云端文件-65');
    await expect(result.locator('tbody tr')).toHaveCount(1);
    await page.getByRole('button',{name:'设为同步目标',exact:true}).click();
    await expect(page.getByRole('heading',{name:'同步配置',exact:true})).toBeVisible();
    await nav.getByRole('button',{name:'任务与记录',exact:true}).click();
    await expect(pager()).toContainText('共 65 项');
    await pager().getByRole('button',{name:'后台任务第4页',exact:true}).click();
    await expect(result.locator('tbody tr')).toHaveCount(5);
    await result.getByRole('button',{name:'查看日志',exact:true}).first().click();
    await expect(page.getByRole('dialog')).toContainText('请求频率超限');
    const taskDialog=page.getByRole('dialog');
    await taskDialog.getByRole('button',{name:'清除日志',exact:true}).click();
    let clearDialog=page.getByRole('dialog').filter({hasText:'清理工具：ima 知识库'});
    await expect(clearDialog).toContainText('运行中工具的日志会跳过');
    await expect(clearDialog).toContainText('仅清除今天之前的日志');
    await clearDialog.getByRole('button',{name:'取消',exact:true}).click();
    expect(clears).toEqual([]);
    await taskDialog.getByRole('button',{name:'清除日志',exact:true}).click();
    clearDialog=page.getByRole('dialog').filter({hasText:'清理工具：ima 知识库'});
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-clear-logs-desktop.png'),fullPage:false});
    await clearDialog.getByRole('button',{name:'确认清除',exact:true}).click();
    await expect.poll(()=>clears.length).toBe(1);
    expect(clears[0]).toEqual({tool:'ima',scope:'history'});
    await expect(page.getByRole('status').filter({hasText:'已清除 2 个日志文件'})).toBeVisible();
    await page.setViewportSize({width:390,height:844});
    await taskDialog.getByRole('button',{name:'清除日志',exact:true}).click();
    clearDialog=page.getByRole('dialog').filter({hasText:'清理工具：ima 知识库'});
    await clearDialog.locator('.v-select .v-field').click();
    await page.getByRole('option',{name:'清除全部日志，包含今天',exact:true}).click();
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-clear-logs-mobile.png'),fullPage:false});
    await clearDialog.getByRole('button',{name:'确认清除',exact:true}).click();
    await expect.poll(()=>clears.length).toBe(2);
    expect(clears[1]).toEqual({tool:'ima',scope:'all'});
    await page.setViewportSize({width:1440,height:1080});

    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.getByRole('tab',{name:'上传记录',exact:true}).click();
    await pager().getByRole('button',{name:'上传记录第14页',exact:true}).click();
    await expect(result).toContainText('历史文件-260.pdf');
    await page.getByRole('tab',{name:'每日记录',exact:true}).click();
    await expect(result).toContainText('不代表云端上传成功');
    await pager().getByRole('button',{name:'每日文件更新记录第5页',exact:true}).click();
    await expect(result).toContainText('每日文件-84.pdf');
    await nav.getByRole('button',{name:'同步工作区',exact:true}).click();
    await page.getByRole('button',{name:'预览上传计划',exact:true}).click();
    await expect(page.getByRole('status').filter({hasText:'后台正在处理'})).toBeVisible();
    snapshot.tasks[0].state='failed';snapshot.tasks[0].exit_code=1;
    await result.getByRole('button',{name:'刷新',exact:true}).click();
    await expect(page.getByText('最近一次预览失败，本次未生成成功的计划。',{exact:false})).toBeVisible();
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-preview-failed.png'),fullPage:true});
    await page.getByRole('button',{name:'查看任务日志',exact:true}).click();
    await expect(page.getByRole('dialog')).toContainText('请求频率超限');
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    snapshot.tasks[0].state='succeeded';snapshot.tasks[0].exit_code=0;snapshot.plan.revision='p2';
    await result.getByRole('button',{name:'刷新',exact:true}).click();
    await expect(page.getByText('任务已完成，请在下方查看对应结果。',{exact:true})).toBeVisible();
    await page.getByRole('button',{name:'提取本地资料',exact:true}).click();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    snapshot.tasks[0].state='succeeded';snapshot.tasks[0].exit_code=0;
    snapshot.collection.updated_at='2026-11-04T20:00:00+08:00';snapshot.collection.revision='c2';
    await result.getByRole('button',{name:'刷新',exact:true}).click();
    await expect(page.getByText('已完成，有提取问题',{exact:false})).toBeVisible();
    await expect(page.getByRole('button',{name:'手动同步',exact:true})).toBeEnabled();
    await expect(page.getByRole('button',{name:'预览上传计划',exact:true})).toBeEnabled();
    await page.getByRole('button',{name:'查看提取问题',exact:true}).click();
    await expect(result).toHaveAttribute('data-kind','collection');
    await page.getByRole('button',{name:'手动同步',exact:true}).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    expect(actions.some(value=>value.action==='sync-once')).toBeFalsy();
    await page.getByRole('button',{name:'取消',exact:true}).click();
    await page.getByRole('button',{name:'手动同步',exact:true}).click();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    expect(actions.some(value=>value.action==='sync-once')).toBeTruthy();
    snapshot.tasks=[];snapshot.state='stopped';snapshot.collection.counts={copied:65};
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1080});
      await page.reload();
      await expect(page.getByRole('heading',{name:'最近上传计划',exact:true})).toBeVisible();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
      await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-work-'+width+'.png'),fullPage:true});
      await nav.getByRole('button',{name:'同步配置',exact:true}).click();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
      await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({animations:'disabled',path:path.join(screenshots,'ima-settings-'+width+'.png'),fullPage:true});
    }
    // Stale slow response must not overwrite a newly selected result section.
    await nav.getByRole('button',{name:'同步工作区',exact:true}).click();
    delayNextRecords=350;
    await result.getByRole('button',{name:'刷新',exact:true}).click();
    await page.getByRole('tab',{name:'提取结果',exact:true}).click();
    await expect(result).toHaveAttribute('data-kind','collection');
    await expect(result.locator('tbody tr').first()).toContainText('提取-0.pdf');
    await expect.poll(()=>delayedResponseFinished).toBeTruthy();
    await expect(result.locator('tbody tr').first()).toContainText('提取-0.pdf');
    expect(recordRequests.some(value=>Number(value.offset)>=600)).toBeTruthy();
    expect(errors).toEqual([]);
    console.log('PASS: ima workflow/config; seven paginated lists; beyond-500 plan/beyond-200 receipts; filters/page sizes; unsaved-config guard; async task success/failure and logs; shared ima log cleanup/cancel/history/all at desktop/mobile; write confirmation; stale-response guard; 1440/768/390px. Screenshots: '+screenshots);
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(error=>{console.error(error);server.close();process.exitCode=1});
