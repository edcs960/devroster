'use strict';
const assert=require('node:assert/strict');
const {chromium}=require(process.argv[2]);
(async()=>{
 const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
 const base=process.argv[4],studio=process.argv[5],out=process.argv[6];
 try{
  for(const width of [1440,390]){
   const page=await browser.newPage({viewport:{width,height:960}}),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.goto(base+'/setup');await page.waitForFunction(()=>document.getElementById('revision').textContent.includes('준비 모드'));
   await page.locator('#setup-steps a[href="#ai"]').click();
   assert.match(await page.locator('#setup-context-help').textContent(),/기본 AI를 활성화/);
   await page.screenshot({path:out+'/preparation-setup-'+width+'.png'});
   await page.goto(base+'/agent?operation=task.run&task_id=T');await page.waitForFunction(()=>document.getElementById('workspace').textContent.includes('준비 모드'));
   assert.equal(await page.locator('#preview-button').isDisabled(),true);
   assert.equal(await page.locator('#execute').isDisabled(),true);
   assert.match(await page.locator('#description').textContent(),/기본 AI를 활성화/);
   assert.equal(await page.locator('#links a[href="/setup#ai"]').count(),1);
   await page.screenshot({path:out+'/preparation-agent-'+width+'.png'});
   await page.locator('#task-operation').selectOption('task.show');
   assert.equal(await page.locator('#preview-button').isDisabled(),false);
   await page.goto(base+'/studio');await page.waitForFunction(()=>document.getElementById('studio-context').textContent.includes('준비 모드'));
   await page.locator('#projects').selectOption(studio);await page.waitForFunction(()=>document.getElementById('title').value==='Preparation browser');
   assert.equal(await page.locator('#generate-spec').isDisabled(),true);
   assert.match(await page.locator('#generate-spec').getAttribute('title'),/기본 AI를 활성화/);
   assert.equal(await page.locator('#save-brief').isDisabled(),false);
   await page.locator('#brief').fill('Editable preparation brief <img src=x onerror="window.bad=true">');
   await page.locator('#save-brief').click();await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('기획서를 저장했습니다'));
   assert.equal(await page.evaluate(()=>window.bad),undefined);
   assert.equal(await page.locator('#studio-context a[href="/setup#ai"]').count(),1);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'mobile horizontal overflow');
   await page.screenshot({path:out+'/preparation-studio-'+width+'.png'});
   assert.deepEqual(errors,[]);await page.close();
   console.log(JSON.stringify({width,pages:['setup','agent','studio'],preparation:true,editSaved:true,errors}));
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
