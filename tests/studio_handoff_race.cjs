'use strict';
// Installed Playwright is supplied by the harness; no test/runtime dependency added.
const {chromium}=require(process.env.TASK3_PLAYWRIGHT);
const assert=require('node:assert/strict'), fs=require('node:fs');
const base=process.argv[2], output=process.argv[3];
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',args:['--no-first-run','--no-default-browser-check']});
 const result={errors:[],checks:[]};
 try {
  const page=await browser.newPage({viewport:{width:390,height:900}});page.on('pageerror',e=>result.errors.push(e.message));
  const selected=()=>page.locator('#handoff-json').textContent().then(JSON.parse);
  const settle=()=>page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  async function hold(path,match,error=false){const reached=deferred(),release=deferred(),done=deferred();const handler=async route=>{if(!match(route.request()))return route.continue();const response=await route.fetch();reached.resolve();await release.promise;await route.fulfill(error?{status:409,contentType:'application/json',body:JSON.stringify({error:'OLD_STALE_ERROR'})}:{response});done.resolve();};await page.route('**'+path,handler);return {reached:reached.promise,async finish(){release.resolve();await done.promise;await settle();await page.unroute('**'+path,handler);}};}
  const waitSelection=id=>page.waitForFunction(id=>document.getElementById('handoff-json').textContent.trim()&&JSON.parse(document.getElementById('handoff-json').textContent).project_id===id&&!document.getElementById('handoff-register').disabled,id);
  await page.goto(base+'/studio?project_id=STUDIO-001');await page.waitForFunction(()=>!document.getElementById('prepare-development').disabled);await page.click('[data-step=handoff]');await page.click('#prepare-development');await waitSelection('P2');
  const race=await hold('/api/handoff/preview',r=>r.postDataJSON().project_id==='P1');
  await page.selectOption('#handoff-project','P1');await race.reached;await page.selectOption('#handoff-project','P2');await waitSelection('P2');await race.finish();
  result.targetRace={dropdown:await page.locator('#handoff-project').inputValue(),selection:(await selected()).project_id};
  fs.writeFileSync(output,JSON.stringify(result,null,2));assert.equal(result.targetRace.selection,'P2');result.checks.push('늦은 P1 preview가 현재 P2를 덮어쓰지 않음');
  const preparing=await hold('/api/home/bootstrap',()=>true);await page.click('#prepare-development');await preparing.reached;
  const sourceLoaded=page.waitForResponse(r=>r.url().includes('/api/studio/project?id=STUDIO-002'));await page.selectOption('#projects','STUDIO-002');await sourceLoaded;await settle();await preparing.finish();assert(await page.locator('#journey-editor').isHidden());
  const loading=await hold('/api/studio/project?id=STUDIO-001',()=>true);await page.selectOption('#projects','STUDIO-001');await loading.reached;
  const newerSource=page.waitForResponse(r=>r.url().includes('/api/studio/project?id=STUDIO-002'));await page.selectOption('#projects','STUDIO-002');await newerSource;await settle();await loading.finish();
  await page.click('#prepare-development');await waitSelection('P2');assert.equal((await selected()).source_studio_id,'STUDIO-002');
  result.checks.push('늦은 준비 bootstrap·Studio load가 새로운 source 선택을 덮어쓰지 않음');
  for(const error of [false,true]){
   const pending=await hold('/api/handoff/preview',()=>true,error);await page.click('#handoff-preview');await pending.reached;
   const title=page.locator('[data-field=title]').first();await title.fill(error?'오류 응답 중 새 입력':'미리보기 중 새 입력');const message=await page.locator('#journey-status').textContent();await pending.finish();
   assert.equal(await title.inputValue(),error?'오류 응답 중 새 입력':'미리보기 중 새 입력');assert(await page.locator('#handoff-register').isDisabled());assert.equal(await page.locator('#journey-status').textContent(),message);
  }
  result.checks.push('preview 중 mapping 입력·늦은 409 응답 입력/상태 보존');
  const pendingSource=await hold('/api/handoff/preview',()=>true);await page.click('#handoff-preview');await pendingSource.reached;await page.selectOption('#projects','STUDIO-001');await page.waitForFunction(()=>document.getElementById('projects').value==='STUDIO-001'&&document.getElementById('journey-editor').hidden);await pendingSource.finish();assert(await page.locator('#handoff-register').isDisabled());assert(await page.locator('#journey-editor').isHidden());
  result.checks.push('Studio source 전환 뒤 늦은 preview 무시');
  await page.click('[data-step=handoff]');await page.click('#prepare-development');await waitSelection('P2');
  const commit=await hold('/api/handoff/commit',()=>true);const sent=page.waitForRequest(r=>r.url().endsWith('/api/handoff/commit'));await page.click('#handoff-register');const payload=(await sent).postDataJSON();await commit.reached;
  await page.selectOption('#handoff-project','P1');await waitSelection('P1');await commit.finish();assert.equal((await selected()).project_id,'P1');assert.equal(await page.locator('#journey-result').textContent(),'');assert(!(await page.locator('#journey-status').textContent()).includes('등록되었습니다'));
  const p1=await (await page.request.get(base+'/api/home/bootstrap?project_id=P1')).json(),p2=await (await page.request.get(base+'/api/home/bootstrap?project_id=P2')).json();assert.equal(payload.project_id,'P2');assert.equal(payload.source_studio_id,'STUDIO-001');assert.equal(p1.imports.length,0);assert.equal(p2.imports.length,1);
  result.commit={payloadProject:payload.project_id,source:payload.source_studio_id,P1Imports:p1.imports.length,P2Imports:p2.imports.length,currentDropdown:'P1',lateSuccessHidden:true};result.checks.push('제출 P2 문맥 고정·실제 P2만 기록·전환 후 늦은 성공 링크 숨김');
  await page.selectOption('#handoff-project','P2');await waitSelection('P2');
  const normalResponse=page.waitForResponse(r=>r.url().endsWith('/api/handoff/commit'));await page.click('#handoff-register');assert.equal((await normalResponse).status(),200);await page.locator('#journey-result a[href^="/traceability"]').waitFor();assert((await page.locator('#journey-result a[href^="/traceability"]').getAttribute('href')).includes('project_id=P2'));
  result.checks.push('비알파벳순 등록 gates도 새 preview 직후 정상 등록·현재 P2 성공 링크 표시');
  result.views=[];for(const width of [390,1280]){await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth),0);await page.locator('#app-menu-toggle').focus();await page.keyboard.press('Enter');await page.keyboard.press('Escape');assert(await page.locator('#app-menu-toggle').evaluate(e=>e===document.activeElement));result.views.push({width,overflow:0,escapeFocus:true});}
  assert.deepEqual(result.errors,[]);fs.writeFileSync(output,JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
