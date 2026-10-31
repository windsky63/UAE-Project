// Isolated built-UI fixture: no real project API, platform or WeChat access.
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
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const items=[
      {id:'parent',name:'package.zip',kind:'原始下载文件',state:'ready'},
      {id:'child',name:'1-43-52-1167-FLC-AAE_0.pdf',kind:'解压文件',state:'ready',
       business_type:'地上新建管道单线图',parent_archive:'package.zip',parent_file_id:'archive-1',
       member_path:'nested.zip!/ISO/1-43-52-1167-FLC-AAE_0.pdf',extraction_status:'已解压',
       path:'D:/fixture/runtime/file_analysis/jobs/job/run/extracted/ISO/iso.pdf'},
      {id:'office',name:'材料说明.docx',kind:'解压文件',state:'ready',parent_archive:'package.zip'}
    ];
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      let body={tools:[],tasks:[]};
      if(url.pathname==='/api/files'){
        const query=url.searchParams.get('search')||'';
        const selected=items.filter(r=>JSON.stringify(r).includes(query));
        body={items:selected,total:selected.length,unfiltered_total:items.length};
      }
      return route.fulfill({json:body});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#files`);
    await expect(page.locator('.file-grid tbody tr')).toHaveCount(3);
    await expect(page.locator('.file-grid')).toContainText('解压文件');
    await page.getByLabel('搜索文件名 / 项目',{exact:true}).fill('1167-FLC-AAE');
    await expect(page.locator('.file-grid tbody tr')).toHaveCount(1);
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:width===390?844:1000});
      await page.locator('.file-name-link').click();
      await expect(page.locator('.file-details-grid')).toContainText('所属压缩包');
      await expect(page.locator('.file-details-grid')).toContainText('package.zip');
      await expect(page.locator('.file-details-grid')).toContainText('nested.zip!/ISO/');
      await expect(page.locator('.file-details-grid')).toContainText('已解压');
      await page.getByLabel('关闭文件详情',{exact:true}).click();
    }
    expect(errors).toEqual([]);
    console.log('PASS: extracted records, direct filename search and archive lineage details at desktop/mobile sizes');
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(error=>{console.error(error);server.close();process.exitCode=1});
