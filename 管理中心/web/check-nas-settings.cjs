// Isolated built-UI checks; no NAS, SSH process or real project API is used.
// No Browser plugin is installed; use the repository's Playwright and Edge.
const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const http=require('http'),fs=require('fs'),path=require('path');
const dist=path.resolve(__dirname,'dist');
const shots=path.resolve(__dirname,'../../runtime/test-reports/nas-settings-ui');
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
    fs.mkdirSync(shots,{recursive:true});
    const page=await browser.newPage({viewport:{width:1440,height:1100}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let settings={source_root:'../FusionLive/downloads',download_ledger:'../FusionLive/runtime/ledger.sqlite3',
      target_root:'\\\\nas\\share',retry_attempts:2,chunk_bytes:1048576,preserve_previous_versions:true,
      start_hour_beijing:12,stop_hour_beijing:0,verification_method:'ssh',remote_hash:{enabled:true,host:'192.168.32.177',port:22,
        username:'windsky',root_path:'',identity_file:'runtime/ssh/nas_hash_ed25519',
        known_hosts_file:'runtime/ssh/known_hosts',python_command:'python3',timeout_seconds:3600,fallback_to_smb:false}};
    let verified=false,fail=false;const actions=[];
    await page.route('**/api/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/overview')return route.fulfill({json:{tools:[{id:'nas',name:'绿联云上传工具',state:'stopped',installed:true,actions:[]}],tasks:[]}});
      if(url.pathname==='/api/config'){
        if(route.request().method()==='POST')settings=route.request().postDataJSON().data;
        return route.fulfill({json:{data:settings,revision:'fixture'}});
      }
      if(url.pathname==='/api/nas/hash-status')return route.fulfill({json:{state:verified?'ready':settings.remote_hash.enabled?'unverified':'disabled',message:verified?'SSH认证与目录映射检查通过':'当前使用SMB校验',last_run:{started:'2026-11-09T12:00:00+08:00',status:'complete',remote_hash_files:24,remote_hash_bytes:1000000000,nas_read_bytes:0,hash_cache_hits:12,remote_hash_fell_back:false}}});
      if(url.pathname==='/api/nas/setup'){actions.push('setup');settings.remote_hash.root_path='/volume1/share';return route.fulfill({json:{message:'首次配置向导已在服务器桌面打开'}})}
      if(url.pathname==='/api/nas/hash-check'){
        actions.push('hash-check');
        if(fail)return route.fulfill({status:400,json:{detail:'请先安全停止上传'}});
        verified=true;return route.fulfill({json:{id:'check1',state:'running'}});
      }
      if(url.pathname==='/api/action')throw Error('Unexpected upload start');
      return route.fulfill({json:{items:[]}});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#nas`);
    await page.getByRole('button',{name:'配置设置',exact:true}).click();
    const region=page.getByRole('region',{name:'NAS本机校验设置'});
    await expect(region.getByRole('textbox',{name:'SSH 地址',exact:true})).toHaveValue('192.168.32.177');
    await expect(region.getByRole('textbox',{name:'SSH 用户名',exact:true})).toHaveValue('windsky');
    await expect(region.getByRole('button',{name:'保存并检查连接',exact:true})).toBeDisabled();
    expect(await page.locator('input[type=password]').count()).toBe(0);
    await region.getByRole('button',{name:'首次配置向导',exact:true}).click();
    await expect.poll(()=>actions.filter(x=>x==='setup').length).toBe(1);
    await region.getByRole('button',{name:'刷新配置与状态',exact:true}).click();
    await expect(region.getByText('NAS 目录已配置：/volume1/share',{exact:true})).toBeVisible();
    await expect(region.getByRole('textbox',{name:'NAS 内部目录（自动识别）',exact:true})).toHaveCount(0);
    const method=region.getByRole('combobox',{name:'文件校验方式',exact:true});
    await method.locator('..').click();await page.getByRole('option',{name:'SMB 完整回读校验',exact:true}).click();
    await expect(region.getByRole('textbox',{name:'SSH 地址',exact:true})).toHaveCount(0);
    await method.locator('..').click();await page.getByRole('option',{name:'SSH 本机校验（默认）',exact:true}).click();
    await region.getByRole('button',{name:'保存并检查连接',exact:true}).click();
    await expect(region.getByText('已保存配置：SSH认证与目录映射检查通过')).toBeVisible();
    expect(settings.verification_method).toBe('ssh');expect(settings.remote_hash.fallback_to_smb).toBe(false);
    expect(settings.remote_hash.enabled).toBe(true);expect(settings.remote_hash.root_path).toBe('/volume1/share');
    await page.reload();await page.getByRole('button',{name:'配置设置',exact:true}).click();
    await region.getByRole('button',{name:'密钥与连接选项',exact:true}).click();
    await expect(region.getByRole('textbox',{name:'NAS 内部目录（自动识别）',exact:true})).toHaveValue('/volume1/share');
    fail=true;await region.getByRole('button',{name:'保存并检查连接',exact:true}).click();
    await expect(region.getByText('请先安全停止上传', {exact:true})).toBeVisible();
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1100});
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
      await region.scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(shots,`nas-settings-${width}.png`),fullPage:true});
    }
    expect(errors).toEqual([]);console.log('PASS NAS settings save/reload, setup, explicit check, errors, metrics and responsive layout');
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
