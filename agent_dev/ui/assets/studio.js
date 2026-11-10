(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const preparationReason='준비 모드에서는 AI 실행을 할 수 없습니다. 프로젝트 설정의 AI 도구에서 기본 AI를 활성화하고 인증을 확인해주세요.';
  const state = {preparationMode:false,data:null, token:'', template:'', documents:[], step:'brief', screen:'', element:'', document:'', specDraft:null, specDirty:false, briefDirty:false, commenting:true, preview:false, pending:false, history:false, timer:null};
  const make = (tag, text, cls) => {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
  const button = (title, action, cls='secondary') => {const b=make('button',title,cls);b.type='button';b.addEventListener('click',action);return b;};
  // Feedback rules: blocking errors stay in a dismissible banner until handled; successes are toasts.
  const UI = globalThis.DevRosterUI;
  const notice = (message, error=false) => {
    const box=$('notice');
    if(!message || !error){box.hidden=true;box.replaceChildren();box.classList.remove('error');if(message){if(UI)UI.toast(message);else{box.textContent=message;box.hidden=false;}}return;}
    const text=make('p',UI?UI.errorMessage(message):message),dismiss=make('button','닫기','ghost sm');dismiss.type='button';dismiss.addEventListener('click',()=>{box.hidden=true;box.replaceChildren();});
    box.replaceChildren(text,dismiss);box.classList.add('error');box.hidden=false;
  };
  const live = () => state.data?.jobs.find(j=>['queued','running'].includes(j.state));
  const current = () => !!state.data?.mock && !state.history && state.data.mock.revision===state.data.project.mock_revision;
  const approved = screen => state.data?.approvals.some(a=>a.screen_id===screen);
  const writable = () => current() && !approved(state.screen) && !live() && !state.pending;
  async function api(path, payload) {
    let response,value;
    try {response=await fetch('/api/studio/'+path,{method:payload ? 'POST':'GET',headers:payload ? {'Content-Type':'application/json','X-Studio-Token':state.token}:undefined,body:payload ? JSON.stringify(payload):undefined});value=await response.json();}
    catch(error){throw new Error(UI?UI.errorMessage(error):error.message);}
    if(!response.ok)throw new Error(value.error || '요청을 처리하지 못했습니다.');return value;
  }
  function base() {return {id:state.data.project.id,version:state.data.project.version,revision:state.data.mock?.revision};}
  function apply(data, {brief=false}={}) {
    const previous=state.data;
    if(previous?.project.id!==data.project.id){$('journey-editor').hidden=true;$('journey-result').replaceChildren();journeySelection=null;invalidateJourney();}
    else if(previous?.project.version!==data.project.version){invalidateJourney();journeyStatus('Studio 변경됨 · 입력을 확인하고 새 미리보기를 준비해주세요.');}
    state.data=data;state.history=!!data.readonly;$('projects').value=data.project.id;
    if(brief || !state.briefDirty) {$('title').value=data.project.title;$('brief').value=data.project.brief;state.briefDirty=false;}
    if(!state.specDirty || previous?.spec?.revision!==data.spec?.revision) {
      state.specDraft=data.spec ? structuredClone(data.spec.payload):null;state.specDirty=false;
    }
    render();poll();
  }
  function setStep(step) {
    state.step=step;
    const stepTitle = $('studio-step-title');
    if(stepTitle) stepTitle.textContent = {brief:'기획서 작성',spec:'명세서 검토',mock:'화면 검토',handoff:'개발 인계'}[step];
    const stepDescription = $('studio-step-description');
    if(stepDescription) stepDescription.textContent = {brief:'기획 내용을 작성하고 생성 조건을 확인하세요.',spec:'본문과 확인 질문을 검토해 현재 명세를 확정하세요.',mock:'요소별 의견을 확인하고 현재 화면을 확정하세요.',handoff:'확정 자료와 개발 대상 연결을 확인해 인계하세요.'}[step];
    ['brief','spec','mock','handoff'].forEach(s=>{$(s+'-panel').hidden=s!==step;});
    $('steps').querySelectorAll('button').forEach(b=>{if(b.dataset.step===step)b.setAttribute('aria-current','step');else b.removeAttribute('aria-current');});
    render();
  }
  function syncButtons() {
    const busy=!!live() || state.pending, spec=state.data?.spec;
    $('save-brief').disabled=busy;
    $('generate-spec').disabled=state.preparationMode || busy || !state.data || state.briefDirty;
    const studioContext = $('studio-context');
    if(studioContext){studioContext.textContent='기획 프로젝트 · '+(state.data?.project.title||'저장 전 초안')+(state.preparationMode?' · 준비 모드 · '+preparationReason+' ':'');if(state.preparationMode){const link=make('a','AI 설정');link.href='/setup#ai';studioContext.append(link);}}
    ['generate-spec','generate-mock','regenerate-mock'].forEach(id=>{$(id).title=state.preparationMode?preparationReason:'';});
    const briefSaveState = $('brief-save-state');
    if(briefSaveState) briefSaveState.textContent = busy ? '생성·저장 처리 중' : state.briefDirty ? '변경됨 · 저장 필요' : state.data ? '현재 기획서 저장됨' : '기획서를 저장하면 명세서를 생성할 수 있습니다.';
    $('save-spec').disabled=busy || !state.specDraft || state.history || !state.specDirty;
    $('approve-spec').disabled=busy || !spec || state.history || state.specDirty || !!spec.approved || state.specDraft?.questions.some(q=>!q.answer?.trim());
    $('generate-mock').disabled=state.preparationMode || busy || !spec?.approved || state.specDirty || state.history || !spec.payload.screens.length;
    $('regenerate-mock').disabled=state.preparationMode || busy || !spec?.approved || state.history || !state.data?.mock;
    $('new-review').disabled=busy || !current();
    $('carry-mock').disabled=busy || !spec?.approved || state.history || !!state.data?.mock;
    $('download-spec').disabled=busy || !spec || state.history || state.specDirty;
    $('approve-screen').disabled=!writable() || state.data?.comments.some(c=>c.state==='open' && (c.screen_id===state.screen || c.orphaned));
    const ready=current() && !!spec?.approved && state.data.mock.payload.screens.every(s=>approved(s.id)) && !state.data.comments.some(c=>c.state==='open');
    $('download-handoff').disabled=busy || !ready;
    $('prepare-development').disabled=busy || !ready;
    renderJob();
    renderSteps(busy);
  }
  // Step completion and gate reasons: say what is done and why a step's main action is unavailable.
  const STEP_ICON={done:'check',locked:'lock'};
  function stepStates() {
    const data=state.data, spec=data?.spec, mock=current() ? data.mock : null;
    const unanswered=(state.specDraft?.questions||[]).filter(q=>!q.answer?.trim()).length;
    const screens=mock?.payload.screens||[], done=screens.filter(s=>approved(s.id)).length;
    const open=(data?.comments||[]).filter(c=>c.state==='open').length;
    const ready=!!mock && !!spec?.approved && screens.length>0 && done===screens.length && !open;
    return {
      brief: state.briefDirty ? ['attention','변경됨 · 저장 필요'] : data ? ['done','저장됨'] : ['active','작성 중'],
      spec: !spec ? ['locked','기획서 저장 후 생성'] : state.specDirty ? ['attention','수정됨 · 저장 필요'] : spec.approved ? ['done','버전 '+spec.revision+' 확정'] : unanswered ? ['attention','확인 질문 '+unanswered+'개 남음'] : ['active','버전 '+spec.revision+' 검토 중'],
      mock: !spec?.approved ? ['locked','명세서 확정 후'] : !mock ? ['active','목업 생성 전'] : ready ? ['done',screens.length+'개 화면 확정'] : ['attention',done+'/'+screens.length+' 확정'+(open?' · 의견 '+open+'개':'')],
      handoff: ready ? ['active','인계 준비 완료'] : ['locked','모든 화면 확정 후']
    };
  }
  function renderSteps(busy) {
    const states=stepStates(), tones={done:'success',attention:'warning',active:'accent',locked:''};
    $('steps').querySelectorAll('button[data-step]').forEach((b,i)=>{
      const [st,text]=states[b.dataset.step];b.dataset.state=st;
      b.querySelector('.s-status').textContent=text;
      const ico=b.querySelector('.s-ico');
      if(STEP_ICON[st])ico.innerHTML='<svg class="i sm" viewBox="0 0 24 24" aria-hidden="true"><use href="/brand-mark.svg#nav-'+STEP_ICON[st]+'"/></svg>';else ico.textContent=String(i+1);
      b.setAttribute('aria-label',(i+1)+'단계 '+b.querySelector('.s-label').textContent+', '+text);
    });
    const [st,text]=states[state.step]||['',''];const badge=$('studio-step-badge');
    if(badge){badge.className=st&&tones[st]!==undefined?'badge '+tones[st]:'badge';badge.textContent=text;badge.hidden=!text;}
    const unanswered=(state.specDraft?.questions||[]).filter(q=>!q.answer?.trim()).length, spec=state.data?.spec;
    gate('brief-gate', $('generate-spec').disabled && !busy && (state.preparationMode ? '준비 모드에서는 생성할 수 없습니다.' : !state.data ? '기획서를 저장하면 생성할 수 있습니다.' : state.briefDirty ? '저장한 뒤 생성할 수 있습니다.' : ''));
    gate('spec-gate', spec && !spec.approved && $('approve-spec').disabled && !busy && !state.history && (state.specDirty ? '수정 내용을 저장한 뒤 확정할 수 있습니다.' : unanswered ? '확인 질문 '+unanswered+'개에 답변해야 확정할 수 있습니다.' : ''));
  }
  function gate(id,text){const el=$(id);if(!el)return;el.textContent=text||'';el.hidden=!text;}
  // AI tools report no progress: compare elapsed time with recent runs of the same kind.
  const JOB_DEFAULT_SECONDS={specs:210,mock:60};
  let jobTicker=null,jobBar=null;
  function clock(seconds){const s=Math.max(0,Math.round(seconds));return s<60?s+'초':Math.floor(s/60)+'분'+(s%60?' '+(s%60)+'초':'');}
  function renderJob(){
    const job=live(),box=$('job-status');box.hidden=!job;
    if(!job){clearInterval(jobTicker);jobTicker=null;jobBar=null;box.dataset.job='';box.replaceChildren();return;}
    const key=job.id+':'+job.state;
    if(box.dataset.job!==key){
      box.dataset.job=key;
      const what=job.kind==='specs'?'명세서를':job.kind==='spec_documents'?'선택한 문서를':'목업을',head=make('div',undefined,'job-head');
      head.append(make('strong',job.state==='queued'?'AI 생성 준비 중입니다':'AI가 '+what+' 만들고 있습니다'),button('생성 중단',()=>mutate('cancel',{job_id:job.id})));
      jobBar=DevRosterProgress.create('AI 생성 진행 상황 (예상)');
      const meta=make('p',undefined,'job-meta');meta.setAttribute('aria-hidden','true');  // ticks every second; the bar carries it
      box.replaceChildren(head,jobBar.element,meta,make('p','다른 단계는 완료 후 수정할 수 있습니다.','job-meta'));
      clearInterval(jobTicker);jobTicker=setInterval(tickJob,1000);
    }
    tickJob();
  }
  function tickJob(){
    const job=live(),box=$('job-status');if(!job||box.hidden)return;
    const meta=box.querySelector('.job-meta');if(!jobBar||!meta)return;
    const estimate=state.data?.job_estimates?.[job.kind]||JOB_DEFAULT_SECONDS[job.kind]||120;
    const elapsed=Date.now()/1000-job.created,over=job.state==='running'&&elapsed>estimate;
    const percent=job.state==='queued'?0:Math.min(95,Math.round(elapsed/estimate*100));
    const stage=job.state==='running'&&job.message?job.message+' · ':'';const text=job.state==='queued'?'AI 실행 순서를 기다리고 있습니다':stage+(over?'예상보다 오래 걸리고 있습니다 · 경과 '+clock(elapsed):'경과 '+clock(elapsed)+' · 보통 약 '+clock(estimate)+' 걸립니다');
    jobBar.set(percent/100,text,over?'over':'');meta.textContent=text;
  }
  function render() {
    syncButtons();
    renderSelection();
    if(state.step==='spec')renderSpecs();
    if(state.step==='mock')renderMock();
    if(state.step==='handoff')renderHandoff();
  }
  function markSpec() {state.specDirty=true;syncButtons();const b=$('regenerate-spec-answers');if(b)b.disabled=true;}
  function field(label,value,onchange,multiline=false) {
    const box=make('label',label), input=make(multiline?'textarea':'input');input.value=value || '';input.addEventListener('input',()=>onchange(input.value));box.append(input);return box;
  }
  function renderSpecs() {
    const spec=state.data?.spec, draft=state.specDraft;
    const selector=$('spec-revisions');selector.replaceChildren();
    if(!spec){const o=make('option','현재 기획서 · 명세 생성 전');o.value='';selector.append(o);}
    (state.data?.spec_versions || []).forEach(v=>{const o=make('option',`버전 ${v.revision} · ${v.approved?'확정':'초안'}`);o.value=v.revision;selector.append(o);});selector.value=spec?.revision || '';selector.disabled=!state.data?.spec_versions.length;
    if(!draft)$('spec-summary').textContent='현재 기획서의 명세서를 생성해주세요. 이전 명세서는 버전 목록에서 읽을 수 있습니다.';
    $('spec-empty').hidden=!!draft;$('spec-content').hidden=!draft;if(!draft)return;
    $('spec-summary').textContent=`버전 ${spec.revision} · 요구사항 ${draft.requirements.length}개 · 화면 ${draft.screens.length}개 · ${spec.approved?'확정됨':'검토 중'}${state.history?' · 과거 버전 (읽기 전용)':''}`;
    const questions=$('questions');questions.replaceChildren();
    if(draft.questions.length) {
      questions.append(make('h3','확정 전에 확인해주세요.'));
      draft.questions.forEach(q=>{const f=field(q.question,q.answer,v=>{q.answer=v;markSpec();},true);f.querySelector('textarea').disabled=state.history || !!live() || state.pending;questions.append(f);});
      const regenerate=button('저장한 답변으로 명세서 다시 생성',()=>generate('specs'));regenerate.id='regenerate-spec-answers';regenerate.disabled=state.preparationMode || state.specDirty || state.history || !!live() || state.pending;questions.append(regenerate);
      questions.append(make('p','답변을 저장해 확정 근거로 남깁니다. 답변에 따라 문서 내용이 달라진다면 1단계의 ‘명세서 생성’으로 답변을 반영해 다시 생성하고 검토해주세요.','hint'));
    }
    const requirements=$('requirements');requirements.replaceChildren();
    draft.requirements.forEach(r=>{
      const card=make('article',undefined,'requirement');card.append(make('strong',r.id+' · '+r.priority));
      card.append(field('요구사항 이름',r.title,v=>{r.title=v;markSpec();}),field('구체적인 동작',r.description,v=>{r.description=v;markSpec();},true));
      card.append(make('blockquote','기획서 근거: '+r.source_quote));
      card.append(field('수용 기준 (한 줄에 하나)',r.acceptance.join('\n'),v=>{r.acceptance=v.split('\n').filter(x=>x.trim());markSpec();},true));
      card.querySelectorAll('input,textarea').forEach(e=>e.disabled=state.history || !!live() || state.pending);requirements.append(card);
    });
    if(!draft.documents[state.document])state.document=state.documents[0];
    renderDocList();
    renderDocument();
  }
  // Mermaid source with a copy button. The screen's CSP (style-src 'self') blocks the inline
  // styles Mermaid draws with, so the picture is left to GitHub or any Mermaid viewer.
  function diagram(pre, code) {
    const box=make('figure',undefined,'mermaid-block'), head=make('figcaption');
    head.append(make('span','Mermaid 다이어그램 · GitHub나 Mermaid 편집기에 붙여 넣으면 그림으로 보입니다.'),button('코드 복사',async()=>{
      try{await navigator.clipboard.writeText(code.textContent);notice('다이어그램 코드를 복사했습니다.');}
      catch{notice('복사 권한이 없습니다. 코드를 선택해 직접 복사하세요.',true);}
    },'secondary sm'));
    box.append(head,pre);return box;
  }
  function markdown(container, text) {
    container.replaceChildren();
    const lines=text.split('\n'), cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(v=>v.trim());
    let code=null, list=null;
    for(let i=0;i<lines.length;i++) {
      const line=lines[i];
      if(line.trim().startsWith('```')) {if(code){code=null;}else{const pre=make('pre');code=make('code');pre.append(code);container.append(line.trim().slice(3).trim().toLowerCase()==='mermaid'?diagram(pre,code):pre);}list=null;continue;}
      if(code){code.append(document.createTextNode(line+'\n'));continue;}
      if(line.includes('|') && i+1<lines.length && /^\s*\|?[\s:|\-]+\|?\s*$/.test(lines[i+1]) && lines[i+1].includes('---')) {
        const table=make('table'), head=make('thead'), row=make('tr');cells(line).forEach(c=>row.append(make('th',c)));head.append(row);table.append(head);i++;
        const body=make('tbody');while(i+1<lines.length && lines[i+1].includes('|')) {i++;const tr=make('tr');cells(lines[i]).forEach(c=>tr.append(make('td',c)));body.append(tr);}table.append(body);
        const scroll=make('div',undefined,'markdown-table');scroll.append(table);container.append(scroll);list=null;continue;
      }
      const item=line.match(/^\s*[-*]\s+(.+)$/);
      if(item){if(!list){list=make('ul');container.append(list);}list.append(make('li',item[1]));continue;}
      list=null;
      const heading=line.match(/^(#{1,4})\s+(.+)$/);
      if(heading)container.append(make('h'+Math.min(heading[1].length+1,6),heading[2]));
      else if(line.trim())container.append(make('p',line));
    }
  }
  // 22 specification documents in reading order, grouped; unknown names fall into 기타.
  const DOC_GROUPS=[['기획·요구',['개발기획서','요구사항명세서','기능명세서','비기능요구사항정의서','프로세스정의서','용어사전']],['화면·인터페이스',['화면정의서','API명세서','프로그램목록']],['데이터·정책',['데이터정의서','테이블정의서','권한정의서','정책정의서','코드공통코드정의서']],['설계',['기술설계서','시스템다이어그램정의서','시퀀스다이어그램','클래스다이어그램']],['검증·추적',['테스트시나리오','요구사항추적성매트릭스','갭분석및보완사항','산출물목록']]];
  function renderDocList() {
    const draft=state.specDraft, nav=$('documents');if(!draft)return;nav.replaceChildren();
    const needle=($('document-filter')?.value||'').trim();
    const known=new Set(DOC_GROUPS.flatMap(g=>g[1]));
    const groups=[...DOC_GROUPS,['기타',state.documents.filter(name=>!known.has(name))]];
    let shown=0;
    for(const [label,names] of groups){
      const list=names.filter(name=>state.documents.includes(name)&&(!needle||name.includes(needle)));if(!list.length)continue;
      nav.append(make('p',label,'doc-group'));
      for(const name of list){const b=button(name,()=>{state.document=name;renderDocument();nav.querySelectorAll('button').forEach(e=>e.classList.toggle('active',e===b));});b.classList.toggle('active',name===state.document);b.dataset.available=String(typeof draft.documents[name]==='string');if((draft.failed_documents||[]).includes(name)){b.dataset.failed='true';b.title='생성 실패 · 다시 생성 필요';}if(name===state.document)b.setAttribute('aria-current','true');nav.append(b);shown++;}
    }
    if(!shown)nav.append(make('p','‘'+needle+'’와 일치하는 문서가 없습니다.','hint'));
  }
  function renderDocument() {
    if(!state.specDraft)return;
    $('document-title').textContent=state.document;$('document-body').value=state.specDraft.documents[state.document]||'';
    $('document-body').disabled=state.history || !!live() || state.pending;$('document-body').hidden=state.preview;$('document-preview').hidden=!state.preview;
    $('document-toggle').textContent=state.preview?'편집 내용으로 돌아가기':'미리보기';
    markdown($('document-preview'),state.specDraft.documents[state.document]||'이 버전에는 선택한 문서 본문이 없습니다.');
    const failed=(state.specDraft.failed_documents||[]).includes(state.document);$('document-failed').hidden=!failed;$('document-failed').textContent=failed?'이 문서는 AI 생성에 실패했습니다. 아래에서 다시 생성해주세요.':'';
    const blocked=state.preparationMode||state.history||!!live()||state.pending||state.specDirty||state.document==='개발기획서'||!state.data?.spec;
    $('regenerate-document').disabled=blocked;$('document-note').disabled=blocked;$('regenerate-document').title=state.document==='개발기획서'?'개발기획서는 입력한 기획서 원문입니다.':state.specDirty?'수정 내용을 저장한 뒤 다시 생성할 수 있습니다.':'';
  }
  function collect(items,result={}) {items.forEach(e=>{result[e.id]=e;collect(e.children || [],result);});return result;}
  function renderMock() {
    const mock=state.data?.mock;$('mock-empty').hidden=!!mock;$('mock-content').hidden=!mock;if(!mock)return;
    const screens=mock.payload.screens;
    if(!screens.some(s=>s.id===state.screen)){state.screen=screens[0].id;state.element='';}
    $('mock-badge').textContent=state.history?'과거 버전 · 읽기 전용':`명세 ${mock.spec_revision} · 목업 ${mock.revision}`;
    const revisions=$('revisions');revisions.replaceChildren();state.data.mock_versions.forEach(v=>{const option=make('option',`목업 ${v.revision} · 명세 ${v.spec_revision}`);option.value=v.revision;revisions.append(option);});revisions.value=mock.revision;
    const nav=$('screens');nav.replaceChildren();screens.forEach(s=>{const b=button((approved(s.id)?'✓ ':'')+s.title,()=>{state.screen=s.id;state.element='';renderMock();syncButtons();});b.classList.toggle('active',s.id===state.screen);nav.append(b);});
    drawCanvas();renderInspector();renderScreenComments();
    $('screen-status').textContent=state.history?'이전 검토 결과':approved(state.screen)?'✓ 이 화면은 확정되었습니다.':'이 화면은 검토 중입니다.';
    $('screen-help').textContent=state.history?'최신 버전에서 의견과 확정을 진행해주세요.':approved(state.screen)?'변경이 필요하면 새 검토 버전을 만드세요.':'미해결 의견을 처리한 뒤 개발할 화면을 확정해주세요.';
    const orphan=$('orphan-comments');orphan.replaceChildren();state.data.comments.filter(c=>c.orphaned && c.state==='open').forEach(c=>orphan.append(commentCard(c,true)));
    if(!orphan.children.length)orphan.append(make('p','삭제된 요소에 남은 미해결 의견이 없습니다.','hint'));
    syncButtons();
  }
  function renderScreenComments(){
    const area=$('screen-comments-list');area.replaceChildren();
    const rows=state.data.comments.filter(c=>c.screen_id===state.screen||c.orphaned);
    if(!rows.length){area.append(make('p','이 화면에 남긴 의견이 없습니다.','hint'));return;}
    const table=make('table'),head=make('tr');['상태','요소','의견'].forEach(t=>head.append(make('th',t)));const thead=make('thead');thead.append(head);table.append(thead);const body=make('tbody');
    for(const c of rows){const tr=make('tr');tr.append(make('td',c.state==='open'?'미해결':'처리 완료'),make('td',c.orphaned?'삭제된 요소 · '+c.element_id:c.element_id));const cell=make('td');cell.append(make('p',c.body));if(!c.orphaned)cell.append(button('요소·처리 근거 보기',()=>{state.element=c.element_id;drawCanvas();renderInspector();$('inspector').scrollIntoView({block:'nearest'});}));tr.append(cell);body.append(tr);}table.append(body);area.append(table);
  }
  function drawCanvas() {
    if(!state.data?.mock)return;
    window.StudioMock.render($('canvas'),state.data.mock.payload,state.screen,{mobile:$('viewport').value==='mobile',commenting:state.commenting,selected:state.element,comments:state.data.comments,
      onSelect:id=>{state.element=id;drawCanvas();renderInspector();const selected=$('canvas').querySelector('[data-element-id="'+id+'"]');selected?.focus({preventScroll:true});},
      onNavigate:id=>{state.screen=id;state.element='';renderMock();}});
  }
  function commentCard(comment, orphan=false) {
    const card=make('article',undefined,'comment-card '+(comment.state==='resolved'?'resolved':''));
    card.append(make('small',(orphan?comment.screen_id+' / '+comment.element_id+' · ':'')+(comment.state==='open'?'미해결':'처리 완료')));
    card.append(make('p',comment.body));if(comment.resolution)card.append(make('small','처리 메모: '+comment.resolution));
    const editable=current() && !approved(comment.screen_id) && !live() && !state.pending;
    if(comment.state==='open') {
      const input=make('input');input.placeholder='어떻게 처리했는지 작성해주세요.';input.maxLength=8000;input.setAttribute('aria-label','코멘트 처리 메모');input.disabled=!editable;
      const b=button('처리 완료',()=>mutate('comment-state',{comment_id:comment.id,state:'resolved',resolution:input.value}));b.disabled=!editable;card.append(input,b);
    } else {const b=button('다시 검토',()=>mutate('comment-state',{comment_id:comment.id,state:'open'}));b.disabled=!editable;card.append(b);}
    return card;
  }
  function renderInspector() {
    const box=$('inspector');box.replaceChildren();
    const screen=state.data.mock.payload.screens.find(s=>s.id===state.screen), item=collect(screen.elements)[state.element];
    if(!item) {box.append(make('h3','어느 부분을 바꾸고 싶으신가요?'),make('p','‘요소에 의견 남기기’ 모드에서 화면의 제목, 입력창, 버튼 등을 선택해주세요.'));return;}
    box.append(make('h3',item.label),make('p',item.id+' · '+item.kind,'element-meta'));
    item.requirement_ids.forEach(id=>{const req=state.data.spec.payload.requirements.find(r=>r.id===id);box.append(make('p',id+' · '+req.title,'hint'));});
    for(const id of item.reference_ids||[]){const reference=state.data.mock.payload.design_references?.find(row=>row.id===id);if(reference)box.append(make("p",reference.provider+" 참고 출처 · "+reference.source_digest.slice(0,12)+" (원본과 동일하다는 확인은 아님)","hint"));}
    const body=make('textarea');body.placeholder='예: 저장 버튼을 하단에 고정해주세요.';body.maxLength=8000;body.setAttribute('aria-label',item.label+'에 남길 코멘트');body.disabled=!writable();
    const add=button('코멘트 남기기',()=>mutate('comment',{screen:state.screen,element:item.id,body:body.value}),'');add.disabled=!writable();box.append(body,add);
    const edits=make('details');edits.append(make('summary','요소 문구 직접 수정'));
    const title=field('요소 이름',item.label,()=>{}), content=field('표시 내용',item.text || '',()=>{},true);
    const save=button('문구 수정해 새 버전 만들기',()=>{const payload=structuredClone(state.data.mock.payload);const change=collect(payload.screens.find(s=>s.id===state.screen).elements)[item.id];change.label=title.querySelector('input').value;change.text=content.querySelector('textarea').value;mutate('mock-save',{payload});});save.disabled=!current() || !!live() || state.pending;
    edits.append(title,content,save);box.append(edits);
    state.data.comments.filter(c=>c.screen_id===state.screen && c.element_id===item.id && !c.orphaned).forEach(c=>box.append(commentCard(c)));
  }
  function renderSelection() {
    const lastJob=(state.data?.events||[]).find(e=>e.kind==='job.queued');
    let selection=lastJob?.payload;if(typeof selection==='string'){try{selection=JSON.parse(selection);}catch{selection={};}}
    selection=selection?.selection;
    $('last-selection').textContent=selection?`최근 생성: ${selection.model||'도구 기본 모델'} · ${selection.effort||'도구 기본 강도'} · ${selection.reason}`:'';
  }
  function renderHandoff() {
    const box=$('handoff-status');box.replaceChildren();
    const spec=state.data?.spec, mock=state.data?.mock;
    box.append(make('div',(spec?.approved?'✓ ':'○ ')+'명세서 확정','check-row'+(spec?.approved?' done':'')));
    if(mock)mock.payload.screens.forEach(s=>box.append(make('div',(approved(s.id)?'✓ ':'○ ')+s.title+' 화면 확정','check-row'+(approved(s.id)?' done':''))));
    else box.append(make('div','○ 목업 생성 및 화면 검토','check-row'));
    const open=state.data?.comments.filter(c=>c.state==='open').length || 0;box.append(make('div',`미해결 의견 ${open}개`,'check-row'+(!open?' done':'')));
    const history=$('history');history.replaceChildren();const names={'project.created':'프로젝트 생성','brief.updated':'기획서 변경','spec.edited':'명세서 수정','spec.approved':'명세서 확정','mock.revised':'목업 새 버전','comment.created':'요소 의견 추가','comment.processed':'의견 처리','screen.approved':'화면 확정','job.queued':'AI 생성 시작','job.completed':'AI 생성 완료','job.failed':'AI 생성 실패','job.cancelled':'AI 생성 중단','job.interrupted':'세션 중단'};
    (state.data?.events || []).forEach(e=>{const row=make('div',new Date(e.created*1000).toLocaleString('ko-KR')+' · '+(names[e.kind] || e.kind),'history-row');let payload=e.payload;if(typeof payload==='string'){try{payload=JSON.parse(payload);}catch{payload={};}}const selected=payload?.selection;if(selected)row.append(make('p',`${state.providers?.[selected.provider]?.label||selected.provider} · ${selected.model||'도구 기본 모델'} · ${selected.effort||'도구 기본 강도'} · ${selected.reason}`));history.append(row);});
  }
  // The Journey and Studio boundaries deliberately retain separate CSRF tokens.
  let journeyToken='',journeyProfile=null,journeySelection=null,journeyPreview=null,journeyRequest='',journeyGeneration=0,journeyPreviewContext=null,studioLoadGeneration=0;
  const journeyStatus=(text,error=false)=>{$('journey-status').textContent=text;$('journey-status').classList.toggle('error-state',error);};
  function invalidateJourney(){journeyGeneration++;journeyPreview=null;journeyPreviewContext=null;$('handoff-register').disabled=true;}
  function journeyContext(){return {generation:journeyGeneration,project:$('handoff-project').value,source:JSON.stringify([state.data?.project.id,state.data?.project.version,state.data?.spec?.revision,state.data?.spec?.digest,state.data?.mock?.revision,state.data?.mock?.digest,state.history,$('projects').value,state.briefDirty,state.specDirty])};}
  function currentJourney(context){const now=journeyContext();return context.generation===now.generation&&context.project===now.project&&context.source===now.source;}
  function boundJourney(selection,context){return currentJourney(context)&&selection.project_id===context.project&&selection.source_studio_id===state.data?.project.id&&selection.expected_source_version===state.data?.project.version;}

  async function journeyApi(path,payload){
    const response=await fetch(path,payload?{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':journeyToken},body:JSON.stringify(payload)}:undefined);
    const data=await response.json();if(!response.ok)throw new Error(data.error||'개발 계획 요청을 처리하지 못했습니다.');return data;
  }
  function gatherJourney(){
    if(!journeySelection)return null;
    const tasks=[...$('handoff-mappings').querySelectorAll('.handoff-task')].map((box,index)=>{
      const previous=journeySelection.tasks[index];const mappings=[];
      box.querySelectorAll('[data-criterion]').forEach(select=>{if(!select.value)return;const [gate,target]=JSON.parse(select.value);mappings.push({requirement_id:select.dataset.requirement,criteria_ids:[select.dataset.criterion],gate,target});});
      return {...previous,id:box.querySelector('[data-field=id]').value,title:box.querySelector('[data-field=title]').value,module_id:box.querySelector('[data-field=module]').value,requirement_ids:[...box.querySelector('[data-field=requirements]').selectedOptions].map(o=>o.value),depends_on:[...box.querySelector('[data-field=depends]').selectedOptions].map(o=>o.value),verification_gates:[...box.querySelectorAll('[data-gate]:checked')].map(e=>e.value),verification_mapping:mappings,skip_reviews:[...box.querySelectorAll('[data-review]:not(:checked)')].map(e=>e.value)};
    });const ids=new Set(tasks.map(t=>t.id));tasks.forEach(t=>{t.depends_on=t.depends_on.filter(d=>ids.has(d)&&d!==t.id);});return {...journeySelection,tasks};
  }
  function journeyInput(selection){
    if(!selection)return '';
    // Only set-valued UI fields and equivalent per-criterion mappings are unordered.
    return JSON.stringify({...selection,tasks:selection.tasks.map(task=>({...task,
      requirement_ids:[...new Set(task.requirement_ids)].sort(),depends_on:[...new Set(task.depends_on||[])].sort(),verification_gates:[...new Set(task.verification_gates)].sort(),
      verification_mapping:(task.verification_mapping||[]).flatMap(m=>m.criteria_ids.map(id=>JSON.stringify([m.requirement_id,id,m.gate,m.target]))).sort()
    }))});
  }
  function renderJourney(prepared,context=journeyContext()){
    if(!boundJourney(prepared.selection,context))return;
    journeySelection=prepared.selection;journeyPreview=prepared;journeyPreviewContext=context;journeyRequest=crypto.randomUUID();
    const container=$('handoff-mappings');container.replaceChildren();
    $('handoff-profile').textContent=`프로파일 버전 ${journeySelection.profile_version} · 명세 ${prepared.spec_revision} · 목업 ${prepared.mock_revision} · 요구사항 ${prepared.requirement_count}개 · 화면 ${prepared.screen_count}개 · 설계·계획 각각 명시적 승인 필요`;
    journeySelection.tasks.forEach((task,index)=>{
      const box=make('fieldset',undefined,'handoff-task');box.append(make('legend',task.id));
      const input=(label,value,key)=>{const field=make('label',label),control=make('input');control.value=value;control.dataset.field=key;field.append(control);box.append(field);return control;};
      input('제안 작업 ID',task.id,'id');input('작업 이름',task.title,'title');
      const moduleLabel=make('label','등록 모듈'),module=make('select');module.dataset.field='module';journeyProfile.modules.forEach(m=>{const option=make('option',m.id+' · '+m.allowed_paths.join(', '));option.value=m.id;module.append(option);});module.value=task.module_id;moduleLabel.append(module);box.append(moduleLabel);
      const reqLabel=make('label','연결할 요구사항 (복수 선택)'),req=make('select');req.multiple=true;req.dataset.field='requirements';prepared.mappings.forEach(r=>{const option=make('option',r.requirement_id+' · '+r.title+(r.backend_only?' · 백엔드 요구사항':''));option.value=r.requirement_id;option.selected=task.requirement_ids.includes(r.requirement_id);req.append(option);});reqLabel.append(req);box.append(reqLabel);
      // Order between tasks, e.g. the API task before the screen task.
      const depLabel=make('label','먼저 끝나야 할 작업 (복수 선택, 없으면 함께 실행)'),dep=make('select');dep.multiple=true;dep.dataset.field='depends';journeySelection.tasks.forEach(other=>{if(other.id===task.id)return;const option=make('option',other.id+' · '+other.title);option.value=other.id;option.selected=(task.depends_on||[]).includes(other.id);dep.append(option);});dep.disabled=journeySelection.tasks.length<2;depLabel.append(dep);box.append(depLabel);dep.addEventListener('change',()=>invalidateJourney());
      const selectedModule=journeyProfile.modules.find(m=>m.id===task.module_id),gates=make('fieldset');gates.append(make('legend','등록된 검증 범위'));for(const gate of selectedModule.gates){const label=make('label'),check=make('input');check.type='checkbox';check.dataset.gate=gate.name;check.value=gate.name;check.checked=task.verification_gates.includes(gate.name);label.append(check,make('span',gate.name));gates.append(label);}box.append(gates);
      // Infra and DB reviews come from repository markers alone; drop them when the task does not need them.
      const optional=[['infra','인프라 검토 (Dockerfile 등 감지)'],['db','DB 검토 (PostgreSQL 등 감지)']].filter(([key])=>(task.workstreams||[]).includes(key)||(task.risk||[]).includes(key)||(task.skip_reviews||[]).includes(key));
      if(optional.length){const reviews=make('fieldset');reviews.append(make('legend','저장소 표식으로 추가된 검토 (기능과 무관하면 해제)'));for(const [key,text] of optional){const label=make('label'),check=make('input');check.type='checkbox';check.dataset.review=key;check.value=key;check.checked=!(task.skip_reviews||[]).includes(key);check.addEventListener('change',()=>invalidateJourney());label.append(check,make('span',text));reviews.append(label);}box.append(reviews);}
      const criteria=make('details');criteria.append(make('summary','수용 기준별 검증 대상 연결 (선택)'));for(const mapping of prepared.mappings.filter(r=>task.requirement_ids.includes(r.requirement_id))){for(const id of mapping.criteria_ids){const label=make('label',id),select=make('select');select.dataset.requirement=mapping.requirement_id;select.dataset.criterion=id;const unknown=make('option','미연결 · 수동/일반 검증은 근거로 확대하지 않음');unknown.value='';select.append(unknown);for(const gate of selectedModule.gates){for(const target of gate.targets){const option=make('option',gate.name+' · '+target);option.value=JSON.stringify([gate.name,target]);select.append(option);}}const existing=task.verification_mapping?.find(m=>m.requirement_id===mapping.requirement_id&&m.criteria_ids.includes(id));if(existing)select.value=JSON.stringify([existing.gate,existing.target]);label.append(select);criteria.append(label);}}box.append(criteria);
      module.addEventListener('change',()=>{const edited=gatherJourney();const selected=journeyProfile.modules.find(m=>m.id===module.value);edited.tasks[index].allowed_paths=selected.allowed_paths;edited.tasks[index].verification_gates=selected.gates.map(g=>g.name);edited.tasks[index].verification_mapping=[];renderJourney({...prepared,selection:edited});invalidateJourney();journeyStatus('모듈 변경을 반영해 새 미리보기를 확인해주세요.');});
      req.addEventListener('change',()=>{const edited=gatherJourney();edited.tasks[index].verification_mapping=[];renderJourney({...prepared,selection:edited});invalidateJourney();});
      // Tasks can be merged or split: each task runs the whole roster.
      const remove=make('button','이 작업 삭제','secondary');remove.type='button';remove.disabled=journeySelection.tasks.length<2;remove.addEventListener('click',()=>{const edited=gatherJourney();edited.tasks.splice(index,1);renderJourney({...prepared,selection:edited});invalidateJourney();journeyStatus('작업을 삭제했습니다. 남은 작업에 모든 요구사항이 연결됐는지 확인하고 새 미리보기를 받아주세요.');});box.append(remove);
      container.append(box);
    });
    const add=make('button','작업 추가','secondary');add.type='button';add.addEventListener('click',()=>{const edited=gatherJourney(),first=edited.tasks[0],used=new Set(edited.tasks.map(t=>t.id));let n=edited.tasks.length+1,id=first.id.replace(/-\d+$/,'')+'-'+String(n).padStart(2,'0');while(used.has(id))id=first.id.replace(/-\d+$/,'')+'-'+String(++n).padStart(2,'0');edited.tasks.push({...first,id,title:'새 작업',requirement_ids:[],verification_mapping:[]});renderJourney({...prepared,selection:edited});invalidateJourney();journeyStatus('작업을 추가했습니다. 연결할 요구사항을 고르고 새 미리보기를 받아주세요.');});container.append(add);
    if(prepared.runs_per_task)container.append(make('p',`예상 AI 실행: 작업 ${journeySelection.tasks.length}개 × 작업당 최대 ${prepared.runs_per_task}회 = 최대 ${journeySelection.tasks.length*prepared.runs_per_task}회. 작업이 많을수록 구독 사용량이 늘어납니다.`,'hint'));
    $('handoff-json').textContent=JSON.stringify(journeySelection,null,2);$('handoff-register').disabled=false;
  }
  async function selectJourneyProject(){
    invalidateJourney();journeySelection=null;journeyProfile=null;$('handoff-mappings').replaceChildren();$('handoff-json').textContent='';$('journey-result').replaceChildren();
    const context=journeyContext(),project=context.project,source=state.data?.project.id,version=state.data?.project.version;
    if(!project){journeyStatus('프로젝트 설정에서 개발 프로젝트를 먼저 등록해주세요.');return;}
    try {
      const bootstrap=await journeyApi('/api/home/bootstrap?project_id='+encodeURIComponent(project));if(!currentJourney(context))return;
      journeyToken=bootstrap.token;journeyProfile=bootstrap.profile;
      const prepared=await journeyApi('/api/handoff/preview',{project_id:project,source_studio_id:source,expected_source_version:version,profile_version:journeyProfile?.version});if(!currentJourney(context))return;
      renderJourney(prepared,context);journeyStatus('확정 버전과 모든 요구사항·모듈·검증 범위를 확인한 뒤 초안으로 등록하세요.');
    }catch(error){if(currentJourney(context))journeyStatus(error.message,true);}
  }
  $('prepare-development').addEventListener('click',async()=>{
    invalidateJourney();const context=journeyContext();
    try {
      $('prepare-development').disabled=true;journeyStatus('등록된 프로젝트와 확정 자료를 불러오는 중…');const bootstrap=await journeyApi('/api/home/bootstrap');if(!currentJourney(context))return;
      journeyToken=bootstrap.token;$('journey-editor').hidden=false;$('handoff-project').replaceChildren();for(const p of bootstrap.projects){const option=make('option',p.name+' · '+p.id);option.value=p.id;$('handoff-project').append(option);}await selectJourneyProject();
    }catch(error){if(currentJourney(context))journeyStatus(error.message,true);}finally{syncButtons();}
  });
  $('handoff-project').addEventListener('change',selectJourneyProject);
  $('handoff-mappings').addEventListener('input',()=>{invalidateJourney();journeyStatus('입력 변경됨 · 새 미리보기를 확인해주세요.');});
  $('handoff-preview').addEventListener('click',async()=>{
    const selection=gatherJourney();if(!selection)return;invalidateJourney();const context=journeyContext();if(!boundJourney(selection,context))return;
    try {const prepared=await journeyApi('/api/handoff/preview',selection);if(!currentJourney(context))return;renderJourney(prepared,context);journeyStatus('미리보기가 갱신되었습니다. 확정 버전과 범위를 확인해주세요.');}
    catch(error){if(currentJourney(context))journeyStatus(error.message+' 입력은 유지됩니다.',true);}
  });
  $('handoff-register').addEventListener('click',async()=>{
    const context=journeyPreviewContext;
    if(!journeyPreview||!context||!boundJourney(journeyPreview.selection,context)||journeyInput(gatherJourney())!==journeyInput(journeyPreview.selection)){invalidateJourney();journeyStatus('현재 선택과 입력으로 새 미리보기를 확인해주세요.',true);return;}
    const payload=structuredClone({...journeyPreview.selection,preview_digest:journeyPreview.preview_digest,request_id:journeyRequest});
    $('handoff-register').disabled=true;
    try {
      const result=await journeyApi('/api/handoff/commit',payload);if(!currentJourney(context))return;
      invalidateJourney();journeyStatus('초안 설계·계획이 등록되었습니다. 설계 승인 후 계획을 직접 승인해주세요.');const box=$('journey-result');box.replaceChildren();
      for(const [label,href] of [['1. 설계 명시적 승인','/agent?operation=design.approve&design_id='+encodeURIComponent(result.design_id)+'&version=1'],['2. 계획 명시적 승인','/agent?operation=plan.approve&plan_id='+encodeURIComponent(result.plan_id)],['요구사항 근거 확인','/traceability?project_id='+encodeURIComponent(payload.project_id)+'&import_id='+encodeURIComponent(result.import_id)]]){const p=make('p'),a=make('a',label);a.href=href;p.append(a);box.append(p);}box.append(make('p','승인 후 devroster task run TASK-ID, 완료 후 devroster task verify TASK-ID로 실행·검증합니다.'));
    }catch(error){if(currentJourney(context)){journeyStatus(error.message+' 입력은 유지됩니다. 변경 내용을 확인하고 미리보기를 갱신해주세요.',true);$('handoff-register').disabled=false;}}
  });
  async function mutate(action, payload={}, options={}) {
    if(state.pending)return false;state.pending=true;syncButtons();notice('');
    try {
      const data=await api(action,{...((action==='create')?{}:base()),...payload});
      state.pending=false;apply(data,options);notice(options.message || '저장했습니다.');
      await refreshProjects(data.project.id);return true;
    } catch(error) {state.pending=false;notice(error.message,true);syncButtons();return false;}
  }
  async function refreshProjects(selected) {
    const bootstrap=await api('bootstrap');state.preparationMode=!!bootstrap.preparation_mode;state.token=bootstrap.token;state.documents=bootstrap.documents;state.template=bootstrap.template;
    if(!state.aiInitialized&&bootstrap.ai){state.providers=bootstrap.providers;$('provider').replaceChildren();Object.entries(state.providers).forEach(([p,info])=>{const note=bootstrap.ai_notes?.notes?.[p]??(bootstrap.ai.runners[p].enabled?'':'설정 필요'),option=make('option',info.label+(note?' · '+note:''));option.value=p;$('provider').append(option);});$('provider').value=bootstrap.ai.provider;state.ai=bootstrap.ai;state.aiShared=!!bootstrap.ai_notes?.shared;state.aiInitialized=true;syncRouting();}
    const select=$('projects');select.replaceChildren(make('option','새 프로젝트'));select.firstChild.value='';
    bootstrap.projects.forEach(p=>{const o=make('option',p.title);o.value=p.id;select.append(o);});select.value=selected || '';syncButtons();
    return bootstrap.projects;
  }
  // Reopen the project the user last worked on; browser storage is a convenience only.
  const LAST_PROJECT='studio:last-project';
  function rememberProject(id){try{localStorage.setItem(LAST_PROJECT,id);}catch(e){}}
  function lastProject(projects){let id='';try{id=localStorage.getItem(LAST_PROJECT)||'';}catch(e){}if(projects.some(p=>p.id===id))return id;return [...projects].sort((a,b)=>(b.created||0)-(a.created||0))[0]?.id||'';}
  function poll() {
    clearTimeout(state.timer);const job=live();if(!job)return;
    state.timer=setTimeout(async()=>{
      try {const data=await api('project?id='+encodeURIComponent(state.data.project.id));const completed=data.jobs.find(j=>j.id===job.id);apply(data);
        if(completed && !['queued','running'].includes(completed.state)) {notice(completed.message,completed.state==='failed');if(completed.state==='done')setStep(job.kind==='specs'?'spec':'mock');}
      } catch(error) {notice(error.message+' 잠시 후 다시 확인합니다.',true);poll();}
    },1800);
  }
  function unsaved() {return state.briefDirty || state.specDirty;}
  async function confirmDiscard() {
    if(!unsaved())return true;
    const what=[state.briefDirty&&'기획서',state.specDirty&&'명세서'].filter(Boolean).join('와 ');
    return !!(await ask('저장하지 않은 변경이 있습니다',what+' 수정 내용이 아직 저장되지 않았습니다. 이동하면 변경 내용이 사라집니다.','변경 버리고 이동','계속 편집'));
  }
  // Shared confirmation dialog; falls back to the browser dialog if the shared module is missing.
  function ask(title, body, confirm, cancel='취소') {return UI?UI.confirmAction(title,body,confirm,{cancel}):Promise.resolve(window.confirm(title+'\n'+body));}
  function reset() {studioLoadGeneration++;invalidateJourney();$('journey-editor').hidden=true;$('journey-result').replaceChildren();journeySelection=null;clearTimeout(state.timer);state.data=null;state.specDraft=null;state.specDirty=false;state.briefDirty=false;state.history=false;state.screen='';state.element='';$('title').value='';$('brief').value='';$('projects').value='';notice('');setStep('brief');}
  async function load(id, revision, specRevision) {
    const generation=++studioLoadGeneration;invalidateJourney();$('journey-editor').hidden=true;$('journey-result').replaceChildren();journeySelection=null;
    try {state.specDirty=false;state.briefDirty=false;const data=await api('project?id='+encodeURIComponent(id)+(revision?'&revision='+revision:'')+(specRevision?'&spec_revision='+specRevision:''));if(generation!==studioLoadGeneration)return;apply(data,{brief:true});rememberProject(id);}
    catch(e){if(generation===studioLoadGeneration)notice(e.message,true);}
  }
  async function download(final) {
    try {const response=await fetch('/api/studio/'+(final?'handoff':'specs')+'.zip?id='+encodeURIComponent(state.data.project.id));if(!response.ok){const v=await response.json();throw new Error(v.error);}saveFile(await response.blob(),final?'development-handoff.zip':'specifications.zip');}
    catch(e){notice(e.message,true);}
  }
  function saveFile(blob,name){const url=URL.createObjectURL(blob), a=make('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  $('steps').addEventListener('click',e=>{const b=e.target.closest('button[data-step]');if(b)setStep(b.dataset.step);});
  $('new-project').addEventListener('click',async()=>{if(await confirmDiscard()){reset();$('title').focus();}});
  $('projects').addEventListener('change',async()=>{const id=$('projects').value;if(!(await confirmDiscard())){$('projects').value=state.data?.project.id || '';return;}if(id)await load(id);else reset();});
  ['title','brief'].forEach(id=>$(id).addEventListener('input',()=>{state.briefDirty=true;syncButtons();}));
  $('template').addEventListener('click',()=>saveFile(new Blob([state.template],{type:'text/markdown;charset=utf-8'}),'프로젝트_기획서_템플릿.md'));
  $('use-template').addEventListener('click',async()=>{if($('brief').value.trim() && !(await ask('작성 중인 내용을 템플릿으로 바꿀까요?','현재 기획서 내용이 템플릿으로 대체됩니다. 저장하기 전까지는 마지막 저장본이 그대로 남아 있습니다.','템플릿으로 바꾸기')))return;$('brief').value=state.template;state.briefDirty=true;syncButtons();});
  $('upload').addEventListener('change',async()=>{
    const file=$('upload').files[0];if(!file)return;
    try {if(!/\.(md|markdown|txt)$/i.test(file.name) || file.size>500000)throw new Error('500KB 이하의 Markdown·텍스트 파일을 선택해주세요.');
      const value=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());if(value.length>120000)throw new Error('기획서 내용을 120,000자 이하로 줄여주세요.');
      if($('brief').value.trim() && !(await ask('작성 중인 내용을 파일 내용으로 바꿀까요?','‘'+file.name+'’의 내용이 현재 기획서를 대체합니다.','파일 내용으로 바꾸기')))return;
      $('brief').value=value;if(!$('title').value)$('title').value=file.name.replace(/\.[^.]+$/,'');state.briefDirty=true;syncButtons();notice('파일을 가져왔습니다. 기획서를 저장한 뒤 명세서를 생성해주세요.');
    }catch(e){notice(e.message,true);}finally{$('upload').value='';}
  });
  $('save-brief').addEventListener('click',()=>mutate(state.data?'brief':'create',{title:$('title').value,brief:$('brief').value},{brief:true,message:'기획서를 저장했습니다. ‘명세서 생성’으로 다음 단계를 시작하세요.'}));
  function syncRouting() {
    const provider=$('provider').value,selected=state.ai?.runners[provider];
    const levels=provider==='gemini_cli'||selected?.reasoning==='none'?[]:provider==='gemini_api'||provider==='xai_api'?['low','medium','high']:provider==='exaone_api'?['low','high','max']:provider==='anthropic_api'?['low','medium','high','max']:['low','medium','high','xhigh','max'];
    $('effort').replaceChildren();['',...levels].forEach(level=>{const option=make('option',level||'도구 기본값');option.value=level;$('effort').append(option);});
    $('model').value=selected?.model||'';$('effort').value=selected?.effort||'';
    const automatic=$('auto-routing').checked;
    $('model').disabled=automatic;$('effort').disabled=automatic;
    const info=state.providers?.[$('provider').value];$('provider-help').textContent=info?.kind==='api'?'등록한 API 또는 로컬 서버로 기획서를 전달합니다. 모델·인증 키는 '+(state.aiShared?'AI 도구 메뉴':'AI·환경 설정')+'에서 준비해주세요.':'로컬에 설치하고 로그인한 CLI를 사용합니다. 생성 시 선택한 AI 서비스로 기획서를 전달합니다.';
    $('routing-hint').textContent=automatic?(selected?.routing?.mode==='auto'?'균형형 자동 선택: 명세·위험 검토 high, 일반 작업 medium, 단순 작업 low. 등록한 지원 강도와 상한 안에서 선택합니다.':'프로젝트에 저장한 고정 모델·추론 강도를 사용합니다.'):'이번 생성에만 모델·추론 강도를 직접 지정합니다.';
  }
  const generate=(kind,extra={})=>{if(state.preparationMode){notice(preparationReason,true);return Promise.resolve(false);}return mutate('generate',{kind,...extra,provider:$('provider').value,...($('auto-routing').checked?{}:{model:$('model').value,effort:$('effort').value})},{message:kind==='spec_documents'?'선택한 문서를 다시 생성합니다. 완료되면 새 버전으로 열립니다.':'AI 생성을 시작했습니다. 완료되면 검토 화면으로 이동합니다.'});};
  $('regenerate-document').addEventListener('click',async()=>{const name=state.document;if(!name||$('regenerate-document').disabled)return;if(!await ask('이 문서 다시 생성',name+' 문서만 AI로 다시 씁니다. 다른 문서와 요구사항은 그대로이고, 결과는 새 명세 버전으로 저장됩니다.','다시 생성'))return;if(await generate('spec_documents',{documents:[name],note:$('document-note').value.trim()}))$('document-note').value='';});
  $('provider').addEventListener('change',syncRouting);
  $('auto-routing').addEventListener('change',syncRouting);
  $('generate-spec').addEventListener('click',()=>{if(state.specDirty){notice('명세서 수정 내용을 먼저 저장해주세요.',true);return;}generate('specs');});
  $('save-spec').addEventListener('click',()=>mutate('spec-save',{payload:state.specDraft},{message:'새 명세서 버전으로 저장했습니다. 내용을 확인한 뒤 확정해주세요.'}));
  $('approve-spec').addEventListener('click',()=>mutate('spec-approve',{}, {message:'명세서를 확정했습니다. 이제 목업을 생성할 수 있습니다.'}));
  $('generate-mock').addEventListener('click',()=>generate('mock'));
  $('regenerate-mock').addEventListener('click',()=>generate('mock'));
  $('carry-mock').addEventListener('click',()=>mutate('mock-carry',{}, {message:'이전 목업을 현재 명세서에 이어 붙였습니다. 화면을 확인하고 확정해주세요.'}));
  $('new-review').addEventListener('click',()=>mutate('mock-save',{}, {message:'새 검토 버전을 만들었습니다. 미해결 의견을 이어서 검토해주세요.'}));
  $('document-body').addEventListener('input',()=>{state.specDraft.documents[state.document]=$('document-body').value;markSpec();});
  $('document-filter')?.addEventListener('input',renderDocList);
  $('document-toggle').addEventListener('click',()=>{state.preview=!state.preview;renderDocument();});
  $('download-spec').addEventListener('click',()=>download(false));$('download-handoff').addEventListener('click',()=>download(true));
  $('comment-mode').addEventListener('click',()=>{state.commenting=true;$('comment-mode').setAttribute('aria-pressed','true');$('preview-mode').setAttribute('aria-pressed','false');drawCanvas();});
  $('preview-mode').addEventListener('click',()=>{state.commenting=false;$('comment-mode').setAttribute('aria-pressed','false');$('preview-mode').setAttribute('aria-pressed','true');drawCanvas();});
  $('viewport').addEventListener('change',drawCanvas);
  $('spec-revisions').addEventListener('change',async()=>{if(await confirmDiscard())load(state.data.project.id,null,$('spec-revisions').value);else $('spec-revisions').value=state.data.spec.revision;});
  $('revisions').addEventListener('change',async()=>{if(await confirmDiscard())load(state.data.project.id,$('revisions').value);else $('revisions').value=state.data.mock.revision;});
  $('approve-screen').addEventListener('click',()=>mutate('screen-approve',{screen:state.screen,note:$('approval-note').value},{message:'이 화면을 확정했습니다. 모든 화면이 확정되면 개발 인계를 받을 수 있습니다.'}));
  window.addEventListener('beforeunload',e=>{if(unsaved()){e.preventDefault();e.returnValue='';}});
  refreshProjects().then(async projects=>{const id=new URLSearchParams(location.search).get('project_id')||lastProject(projects||[]);if(id)await load(id);syncButtons();}).catch(e=>notice('Studio를 불러오지 못했습니다. '+e.message,true));
})();
