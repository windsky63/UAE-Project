const {chromium}=require('playwright');
const {expect}=require('@playwright/test');
const fs=require('fs'),path=require('path'),os=require('os');
(async()=>{
  const session=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../runtime/file-fixture-session.json'),'utf8'));
  const output=path.join(os.tmpdir(),'project-tools-pagination-qa');fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});
    await page.goto(session.url);await page.goto(new URL('/#files',session.url).href);
    const status=page.locator('.file-pagination>span');
    await expect(status).toContainText('152 / 152');
    await expect(page.locator('.file-page-number')).toHaveCount(5);
    expect(await page.locator('.file-page-number').allTextContents()).toEqual(['1','2','3','4','5']);
    await page.getByLabel('跳到第 5 页',{exact:true}).click();
    await expect(status).toContainText('第 5 / 8 页');
    expect(await page.locator('.file-page-number').allTextContents()).toEqual(['3','4','5','6','7']);
    await page.getByLabel('跳转到页码',{exact:true}).fill('8');
    await page.getByRole('button',{name:'跳转',exact:true}).click();
    await expect(status).toContainText('第 8 / 8 页');
    await expect(page.locator('.file-grid tbody tr')).toHaveCount(12);
    expect(await page.locator('.file-page-number').allTextContents()).toEqual(['4','5','6','7','8']);
    await page.getByLabel('跳转到页码',{exact:true}).fill('9');
    // Enter exercises application validation independently of HTML number limits.
    await page.getByLabel('跳转到页码',{exact:true}).press('Enter');
    await expect(status).toContainText('第 8 / 8 页');
    await page.getByLabel('跳转到页码',{exact:true}).fill('2');
    await page.getByRole('button',{name:'跳转',exact:true}).click();
    await expect(status).toContainText('第 2 / 8 页');
    await page.screenshot({path:path.join(output,'pagination-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    await page.locator('.file-pagination').scrollIntoViewIfNeeded();
    await expect(page.locator('.file-page-number')).toHaveCount(5);
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    expect(overflow).toBe(false);
    await page.screenshot({path:path.join(output,'pagination-mobile.png')});
    if(errors.length)throw Error(errors.join('\n'));
    console.log(JSON.stringify({passed:true,fixture:'152 real ledger rows, eight pages',checks:['five moving numeric pages','jump to last page','jump back','out-of-range stays put','mobile layout','no console errors'],screenshots:output}));
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
