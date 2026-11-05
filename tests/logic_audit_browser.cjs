'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.argv[2]);
(async()=>{
 const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:960}}),base=process.argv[4],studio=process.argv[5];
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/agent');await page.waitForSelector('.task-choice');
  const task=id=>page.locator('.task-choice').filter({hasText:id});
  await task('T2').click();await task('T2').focus();
  const original=await task('T2').elementHandle();
  await page.evaluate(()=>refresh());
  assert.equal(await original.evaluate(el=>el===document.activeElement),true,'focus and row identity survive refresh');
  await page.route('**/api/agent/tasks',async route=>{
   const response=await route.fetch(),data=await response.json();data.tasks.find(t=>t.id==='T2').state='completed';
   await route.fulfill({response,json:data});
  });
  await page.locator('#refresh').click();await page.waitForFunction(()=>selectedTask.state==='completed');
  assert.equal(await page.locator('#selected-state').textContent(),'완료');
  assert.equal(await page.locator('#task-operation').inputValue(),'task.show');
  assert.equal(await task('T2').locator('.task-status').textContent(),'완료');
  await page.unroute('**/api/agent/tasks');
  await page.evaluate(()=>selectTask(taskRows.find(t=>t.id==='T')));await page.locator('#task-operation').selectOption('task.show');
  let release,seen;
  const gate=new Promise(r=>release=r),started=new Promise(r=>seen=r);
  const delay=async route=>{const response=await route.fetch();seen();await gate;await route.fulfill({response});};
  await page.route('**/api/agent/preview',delay);
  await page.locator('#preview-button').click();await started;await task('T2').click();release();
  await page.waitForFunction(()=>!document.getElementById('preview-button').disabled);
  assert.equal(await page.evaluate(()=>checked),null,'old task preview is discarded');
  assert.equal(await page.locator('#preview').isHidden(),true);
  assert.equal(await page.locator('#execute').isDisabled(),true);
  await page.unroute('**/api/agent/preview',delay);
  // Same input value after an edit is still a newer input generation.
  await page.evaluate(()=>choose(catalog.find(o=>o.id==='preset.show')));
  await page.locator('#field-preset').fill('python');
  let releaseEdit,seenEdit;
  const editedGate=new Promise(r=>releaseEdit=r),editedStart=new Promise(r=>seenEdit=r);
  const editDelay=async route=>{const response=await route.fetch();seenEdit();await editedGate;await route.fulfill({response});};
  await page.route('**/api/agent/preview',editDelay);
  await page.locator('#preview-button').click();await editedStart;
  await page.locator('#field-preset').fill('other');await page.locator('#field-preset').fill('python');releaseEdit();
  await page.waitForFunction(()=>!document.getElementById('preview-button').disabled);
  assert.equal(await page.evaluate(()=>checked),null,'edited and restored input invalidates old preview');
  await page.unroute('**/api/agent/preview',editDelay);
  await page.locator('#preview-button').click();await page.waitForFunction(()=>checked!==null);
  let submits=0;
  await page.route('**/api/agent/submit',route=>{submits++;return route.fulfill({json:{id:'test-submit-only'}});});
  await page.evaluate(()=>{document.getElementById('field-preset').value='changed-without-event';document.getElementById('execute').click();});
  assert.equal(await page.evaluate(()=>checked),null);assert.equal(submits,0,'submit guard catches programmatic changes');
  await page.locator('#field-preset').fill('python');await page.locator('#preview-button').click();await page.waitForFunction(()=>checked!==null);
  await page.locator('#execute').click();await page.waitForFunction(()=>document.getElementById('result').textContent.includes('test-submit-only'));
  assert.equal(submits,1,'fresh preview permits submission (intercepted only)');
  await page.goto(base+'/studio?project_id='+studio);await page.waitForFunction(id=>document.getElementById('projects').value===id,studio);
  assert.equal(await page.locator('#title').inputValue(),'Audit Studio');
  await page.goto(base+'/review?task=T');await page.waitForSelector('#review-layout:not([hidden])');
  await page.waitForFunction(()=>document.getElementById('decision-reason').textContent.includes('검증 통과 기록'));
  assert.equal(await page.locator('input[name=decision][value=approved]').isDisabled(),true);
  assert.equal(await page.locator('#decision-form button[type=submit]').isDisabled(),true);
  await page.locator('input[name=decision][value=rejected]').check();
  assert.equal(await page.locator('#decision-form button[type=submit]').isEnabled(),true);
  await page.route('**/api/review/evidence?*',async route=>{const response=await route.fetch(),data=await response.json();data.verification={current:true,reason:'fixture',gates:[]};await route.fulfill({response,json:data});});
  await page.locator('.review-task-switch summary').click();await page.locator('#refresh').click();await page.waitForFunction(()=>document.getElementById('decision-reason').textContent==='');
  assert.equal(await page.locator('input[name=decision][value=approved]').isEnabled(),true);
  await page.goto(base+'/references');await page.waitForFunction(()=>document.getElementById('project').options.length>0);
  assert.equal(await page.locator('#attach').isDisabled(),true);assert.equal(await page.locator('#apply-profile').isDisabled(),true);
  // Pure response fixture, no external reference is read or stored.
  await page.route('**/api/design-references/bootstrap',async route=>{const response=await route.fetch(),data=await response.json();data.connections=[{id:'test-connection',name:'Test connection',state:'ready',project_id:'EXAMPLE'}];data.references=[{id:'test-reference',locator:{provider:'figma',file_key:'fixture'},fetched_at:0,source_digest:'fixture',content:{text:'local response fixture'}}];await route.fulfill({response,json:data});});
  await page.locator('#refresh').click();await page.waitForSelector('input[name=reference]');
  assert.equal(await page.locator('#attach').isDisabled(),true);
  await page.locator('input[name=reference]').check();assert.equal(await page.locator('#attach').isEnabled(),true);assert.equal(await page.locator('#apply-profile').isEnabled(),true);
  await page.locator('input[name=reference]').uncheck();assert.equal(await page.locator('#attach').isDisabled(),true);
  for(const width of [1440,390]){
   await page.setViewportSize({width,height:960});
   for(const path of ['/agent','/studio?project_id='+studio,'/review?task=T','/references']){
    await page.goto(base+path);await page.waitForLoadState('networkidle');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${path} fits ${width}px`);
   }
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({focus:true,state:true,previewRace:true,inputGeneration:true,submitGuard:true,studioSelection:true,approvalCTA:true,referenceCTA:true,viewports:[1440,390],pageerrors:errors,realSubmit:false}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
