'use strict';
const $ = id => document.getElementById(id);
let taskRows=[], selectedTask=null;
let token='', catalog=[], selected=null, checked=null, submitting=false, pollBusy=false, context={}, inputGeneration=0, previewRequest=0;
const states={queued:'대기',running:'실행 중',completed:'완료',failed:'실패',cancelled:'취소됨',interrupted:'중단됨'};
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function error(e){$('error').hidden=false;$('error').textContent=e.message||String(e);}
async function api(action,payload){const response=await fetch('/api/agent/'+action,payload===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(payload)});const value=await response.json();if(!response.ok||value.error)throw new Error(value.error||'요청을 처리하지 못했습니다.');return value;}
function invalidate() {inputGeneration++;checked=null;$('preview').hidden=true;$('execute').disabled=true;}
function payload(){const fields={},options={};selected.fields.forEach(f=>fields[f.name]=$('field-'+f.name).value);selected.options.forEach(name=>{const el=$('option-'+name);if(el.type==='checkbox')options[name]=el.checked;else if(el.value)options[name]=el.type==='number'?Number(el.value):el.value;});return {operation:selected.id,fields,options};}
const preparationReason='준비 모드에서는 AI 실행을 할 수 없습니다. 프로젝트 설정의 AI 도구에서 기본 AI를 활성화하고 인증을 확인해주세요.';
const aiBlocked=()=>!!context.preparation_mode&&['task.run','task.resume'].includes(selected?.id);
function choose(op,task){if(!op)return;selected=op;invalidate();$('title').textContent=op.label;$('category').textContent=op.group;$('description').textContent=op.mutates?'선택한 대상과 입력 내용을 확인한 뒤 실행을 요청합니다.':'대상과 입력 내용을 확인하여 정보를 조회합니다.';$('fields').replaceChildren();$('option-fields').replaceChildren();$('result').textContent='입력·범위를 확인하면 실행할 수 있습니다.';
for(const field of op.fields){const label=node('label',['backup.inspect','backup.restore'].includes(op.id)&&field.name==='source'?'백업 파일 이름':op.id==='backup.restore'&&field.name==='destination'?'새 검사 폴더 이름':field.type==='project-file'?'커밋된 Markdown 문서 경로':field.label);label.htmlFor='field-'+field.name;$('fields').append(label);let input=node(field.type==='document'?'textarea':'input');input.id='field-'+field.name;input.required=true;if(input.tagName==='INPUT')input.type='text';input.maxLength=field.type==='document'?500000:1000;input.addEventListener('input',invalidate);if(field.name==='task_id'&&task){input.value=task;input.readOnly=true;}if(field.name==='version')input.value='1';if(field.name==='profile')input.value=JSON.stringify(context.profile||{},null,2);if(field.type==='document'&&field.name==='source'){const samples={'design.refine':'## Scope\n구현하려는 기능\n\n## Acceptance\n사용자가 확인할 기대 동작\n\n## Constraints\n변경 범위와 제약','plan.create':JSON.stringify([{id:'TASK-001',title:'구현할 작업',system_name:'프로젝트',depends_on:[]}],null,2),'feedback.add':'확인한 화면/기능:\n\n문제:\n\n기대 동작:\n','view.set-settings':'{"theme": "dark"}','tool.register':JSON.stringify({connection_id:'tool-1',project_id:context.projects?.[0]?.id||'PROJECT',capabilities:['code.read'],enabled:false,authenticated:false,healthy:false,allowlist:[]},null,2)};input.value=samples[op.id]||'';}if(field.name==='project_id'&&context.projects?.length===1)input.value=context.projects[0].id;$('fields').append(input);if(field.type==='document'){const file=node('input');file.type='file';file.accept='.md,.json,text/markdown,application/json';file.setAttribute('aria-label',field.label+' 파일 불러오기');file.addEventListener('change',async()=>{try{if(!file.files[0])return;if(file.files[0].size>500000)throw new Error('문서는 500KB 이하로 줄여주세요.');input.value=await file.files[0].text();invalidate();}catch(e){error(e);}});$('fields').append(file,node('p','문서 내용만 프로젝트 내부에 저장합니다. 민감한 값은 프로젝트 설정 화면에서 관리해주세요.','hint'));}if(field.type==='artifact')$('fields').append(node('p','이 화면에서 만든 파일 이름을 입력해주세요.','hint'));if(field.name==='profile')$('fields').append(node('p','실행기·도구·검증 프로그램은 프로젝트 설정 화면의 검증된 설정을 사용합니다.','hint'));}
for(const name of op.options){const label=node('label');const input=node('input');input.id='option-'+name;input.type=['apply','dry_run','recover','check_auth'].includes(name)?'checkbox':['limit','page'].includes(name)?'number':'text';const names={apply:'변경 적용',dry_run:'미리 검사만 하기',recover:'중단된 업그레이드 복구',check_auth:'인증 상태 확인',limit:'조회 개수',page:'페이지',search:'검색어',state:'상태',project:'프로젝트 ID'};if(input.type==='checkbox'){if(name==='dry_run')input.checked=true;label.append(input,document.createTextNode(names[name]||name));$('option-fields').append(label);}else{label.textContent=names[name]||name;label.htmlFor=input.id;$('option-fields').append(label,input);}input.addEventListener('input',invalidate);}
$('task-operation').value=op.id;if(!op.id.startsWith('task.')&&$('operation-editor'))$('operation-editor').hidden=false;$('options').hidden=!op.options.length;$('preview-button').disabled=aiBlocked();$('preview-button').title=aiBlocked()?preparationReason:'';if(aiBlocked())$('description').textContent=preparationReason;renderCatalog();}
function renderCatalog(){
 const needle=$('filter').value.toLowerCase(),container=$('catalog');container.replaceChildren();
 const groups=new Map();for(const op of catalog){if(!op.fields||![op.group,op.label,op.id].join(' ').toLowerCase().includes(needle))continue;if(!groups.has(op.group))groups.set(op.group,[]);groups.get(op.group).push(op);}
 for(const [name,operations] of groups){const group=node('details');group.open=!!needle||operations.some(op=>op.id===selected?.id);group.append(node('summary',name+' · '+operations.length+'개'));
 for(const op of operations){const button=node('button',op.label);button.type='button';button.title=op.id;button.setAttribute('aria-pressed',String(selected?.id===op.id));if(selected?.id===op.id)button.className='selected';button.addEventListener('click',()=>{closeResult();choose(op,op.id.startsWith('task.')?selectedTask?.id:null);showEditor(true);$('all-functions').open=false;$('operation-editor').focus();});group.append(button);}container.append(group);}
 if(!groups.size)container.append(node('p','일치하는 기능이 없습니다. 검색어를 바꿔주세요.'));
}

function selectTask(task,focus=true){selectedTask=task;invalidate();$('selected-id').textContent=task.id;$('selected-title').textContent=task.title;$('selected-state').textContent=stateLabels[task.state]||task.state;choose(catalog.find(o=>o.id===defaultOperation(task.state)),task.id);closeResult();showEditor(false);renderActions(task);renderTasks();loadOverview(task.id);watchExecution(task.id).catch(error);if(focus)($('primary-action').hidden?$('more-actions'):$('primary-action')).focus();}
// Merged development status: state groups for the list and a read-only overview for the selection.
const STATE_GROUPS=[['','전체'],['attention','조치 필요',['blocked','interrupted','failed']],['running','진행 중',['running','verifying','waiting_for_capacity','queued']],['approval','승인·검토 대기',['approval_pending','user_review','pr_ready']],['ready','실행 준비',['ready']],['done','완료',['completed','cancelled']]];
let stateGroup='';
const inGroup=(task,key)=>!key||(STATE_GROUPS.find(g=>g[0]===key)?.[2]||[]).includes(task.state);
function renderStateFilter(){
 const box=$('task-state-filter');if(!box)return;
 box.replaceChildren(...STATE_GROUPS.map(([key,label])=>{const count=taskRows.filter(t=>inGroup(t,key)).length;const b=node('button',undefined,'chip-btn');b.type='button';b.setAttribute('aria-pressed',String(stateGroup===key));b.append(node('span',label),node('span',String(count),'count muted'));b.addEventListener('click',()=>{stateGroup=key;renderTasks();box.querySelector('[aria-pressed=true]')?.focus();});return b;}).filter((b,i)=>i===0||STATE_GROUPS[i][0]===stateGroup||taskRows.some(t=>inGroup(t,STATE_GROUPS[i][0]))));
}
let overviewGeneration=0;
async function loadOverview(id){
 const box=$('task-overview');if(!box)return;const generation=++overviewGeneration;
 box.hidden=false;box.replaceChildren(node('p','작업 현황을 불러오는 중입니다.','hint'));
 try{
  const response=await fetch('/api/task?id='+encodeURIComponent(id),{cache:'no-store'});const data=await response.json();
  if(generation!==overviewGeneration)return;  // a newer selection wins
  if(!response.ok)throw new Error(data.error||'작업 현황을 불러오지 못했습니다.');
  renderOverview(data);
 }catch(e){if(generation===overviewGeneration)box.replaceChildren(node('p',globalThis.DevRosterUI?DevRosterUI.errorMessage(e):e.message,'hint'));}
}
function renderOverview(data){
 const box=$('task-overview'),def=data.task?.definition||{},approvals=data.approvals||{};
 const section=(title,...children)=>{const s=node('section','', 'overview-block');s.append(node('h3',title),...children);return s;};
 const chips=node('div',undefined,'chips');
 for(const [key,label] of [['design','설계'],['plan','실행 계획'],['snapshot','작업 준비']]){chips.append(node('span',label+(approvals[key]?' 확인됨':' 승인 대기'),'badge '+(approvals[key]?'success':'warning')));}
 const goal=node('div',undefined,'overview-goal');goal.append(node('p',def.title||data.task?.task_id||''));
 const reqs=node('div',undefined,'chips');(def.requirement_ids||[]).forEach(r=>reqs.append(node('span',r,'badge accent')));if(reqs.children.length)goal.append(reqs);
 if((def.depends_on||[]).length)goal.append(node('p','선행 작업: '+def.depends_on.join(', '),'hint'));
 const gates=data.gates||[];const checks=node('div',undefined,'overview-checks');
 if(!gates.length)checks.append(node('p','아직 검증 결과가 없습니다. 개발이 끝나면 등록한 검증 명령의 결과가 여기에 표시됩니다.','hint'));
 for(const g of gates){const row=node('div',undefined,'overview-row');row.append(node('span',g.id||g.gate_id||g.name||'검증'),node('span',g.passed===false||g.state==='failed'?'실패':g.passed||g.state==='passed'?'통과':String(g.state||'대기'),'badge '+(g.passed===false||g.state==='failed'?'danger':g.passed||g.state==='passed'?'success':'')));checks.append(row);}
 const timeline=node('ol',undefined,'overview-timeline');
 for(const ev of (data.timeline||[]).slice(-6).reverse()){const li=node('li');li.append(node('span',ev.kind||'기록'),node('time',ev.created_at?new Date(ev.created_at*1000).toLocaleString('ko-KR'):''));timeline.append(li);}
 if(!timeline.children.length)timeline.append(node('li','변경 이력이 없습니다.'));
 const blocks=[node('h2','작업 현황'),section('확인·승인',chips),section('이번 작업의 목표',goal),section('테스트와 검증',checks),section('최근 변경 이력',timeline)];
 // Code review link carried over from the former development status page (approved-run tasks only).
 if(data.source!=='legacy'&&data.task?.task_id){const review=node('a','변경 검토 열기','text-link');review.href='/review?'+new URLSearchParams({task:data.task.task_id});blocks.push(section('코드 변경 검토',node('p','등록된 작업 저장소의 변경 파일·검증 근거·코멘트를 확인합니다.','hint'),review));}
 box.replaceChildren(...blocks);
}
// State-based primary action and the ⋯ menu. The editor below stays for inputs (게시·완료),
// deep links and the project-wide catalogue; every change still goes through the confirmation panel.
const DEFAULT_OPERATION={ready:'run',blocked:'explain',waiting_for_capacity:'resume',verifying:'verify',pr_ready:'review',user_review:'review',completed:'show',cancelled:'show'};
const defaultOperation=state=>'task.'+(DEFAULT_OPERATION[state]||'show');
const PRIMARY={ready:['task.run','실행'],blocked:['task.explain','차단 원인 보기'],waiting_for_capacity:['task.resume','이어서 실행'],verifying:['task.verify','검증 실행'],pr_ready:['task.review','검토 상태 확인'],user_review:['review-page','변경 검토 열기'],planned:['task.explain','대기 이유 보기'],approval_pending:['task.explain','대기 이유 보기'],feedback_required:['task.explain','대기 이유 보기'],completed:['task.show','상세 보기'],cancelled:['task.show','상세 보기']};
const TASK_MENU=[['task.explain','상태·차단 원인','info'],['task.usage','사용량','clock'],['task.show','상세','file-text'],'-',['task.run','실행','arrow-right'],['task.resume','이어서 실행','refresh-cw'],['task.verify','검증','check-square'],['task.review','검토 상태 확인','eye'],['task.publish','게시','upload'],['task.complete','완료','check-circle'],['task.checkpoint','체크포인트 저장','archive'],'-',['task.cancel','작업 취소','x']];
function unavailable(id,task){
 const op=catalog.find(o=>o.id===id);if(!op||!op.fields)return '이 작업 공간에서 사용할 수 없는 기능입니다.';
 if(!op.mutates)return '';
 if(['completed','cancelled'].includes(task.state))return '끝난 작업입니다.';
 if(context.preparation_mode&&['task.run','task.resume'].includes(id))return preparationReason;
 if(id==='task.run'&&task.state!=='ready')return '실행 준비 상태에서만 사용할 수 있습니다.';
 if(id==='task.complete'&&task.state!=='user_review')return '사용자 검토 단계에서만 사용할 수 있습니다.';
 return '';
}
function showEditor(on){$('operation-editor').hidden=!on;}
function renderActions(task){
 const box=$('task-actions'),primary=$('primary-action'),hint=$('primary-hint');
 box.hidden=!task;hint.hidden=true;hint.textContent='';if(!task)return;
 const entry=PRIMARY[task.state];primary.hidden=!entry;
 if(!entry){hint.textContent=task.state==='running'?'실행 중입니다. 아래 실행 현황이 자동으로 갱신됩니다.':'';hint.hidden=!hint.textContent;return;}
 const [id,label]=entry,reason=id==='review-page'?'':unavailable(id,task);
 primary.textContent=label;primary.dataset.action=id;primary.disabled=!!reason;primary.title=reason;
 if(reason){hint.textContent=reason;hint.hidden=false;}
}
function openTaskMenu(anchor){
 const task=selectedTask,UI=globalThis.DevRosterUI;if(!task||!UI)return;
 const items=TASK_MENU.map(entry=>{if(entry==='-')return '-';const [id,label,icon]=entry,reason=unavailable(id,task);return {label,icon,disabled:!!reason,reason,danger:id==='task.cancel',run:()=>startAction(id)};});
 items.push('-',{label:'프로젝트 전체 기능',icon:'list',run:()=>{const all=$('all-functions');all.open=true;all.scrollIntoView({block:'start'});$('filter').focus();}});
 UI.menu(anchor,items);
}
async function startAction(id){
 const task=selectedTask;if(!task||!id)return;
 if(id==='review-page'){location.href='/review?'+new URLSearchParams({task:task.id});return;}
 const op=catalog.find(o=>o.id===id),reason=unavailable(id,task);
 if(!op||reason){globalThis.DevRosterUI?.toast(reason||'사용할 수 없는 기능입니다.',{type:'info'});return;}
 closeResult();choose(op,task.id);
 if(!op.mutates)return showReadOnly(op,task);
 const needsInput=op.fields.some(f=>f.name!=='task_id')||op.options.length>0;
 showEditor(needsInput);
 if(needsInput){$('operation-editor').focus();return;}
 $('form').requestSubmit();  // Goes straight to the confirmation panel; nothing runs until 실행 is pressed.
}
// Read-only views open at once: nothing changes, so there is no confirmation step.
let resultGeneration=0;
function closeResult(){resultGeneration++;const box=$('action-result');if(box)box.hidden=true;}
function facts(rows){const dl=node('dl',undefined,'facts');for(const [key,value] of rows){dl.append(node('dt',key),node('dd',value==null||value===''?'—':String(value)));}return dl;}
const metricLabels={input_tokens:'입력 토큰',output_tokens:'출력 토큰',cached_input_tokens:'캐시 입력 토큰',reasoning_tokens:'추론 토큰',runner_seconds:'AI 실행 시간(초)',cost:'비용'};
function explainView(data){
 const rows=data.next_actions||[];
 if(!rows.length)return [node('p',data.terminal?'끝난 작업입니다.':'막힌 이유가 없습니다. 다음 행동을 진행할 수 있습니다.','hint')];
 const list=node('ul',undefined,'reason-list');
 for(const row of rows){const li=node('li');li.append(node('strong',row.message),node('p',row.action));list.append(li);}
 return [list];
}
function usageView(result){
 const usage=result.usage||{},cost=result.cost||{},limits=result.limits||{};
 const rows=[['실행 시도',(usage.attempts??0)+'회']];
 for(const m of usage.measurements||[])rows.push([metricLabels[m.metric]||m.metric,m.value==null?'기록 없음':Number(m.value).toLocaleString('ko-KR')]);
 rows.push(['예상 비용',cost.configured?`${cost.currency||''} ${cost.known_cost??0}`.trim():'가격표 미설정']);
 if(limits.max_attempts)rows.push(['최대 시도',limits.max_attempts+'회']);
 if(limits.max_runner_seconds)rows.push(['AI 실행 한도',duration(limits.max_runner_seconds)]);
 return [facts(rows)];
}
function showView(result){
 const def=result.definition||{};
 return [facts([['작업 ID',result.task_id],['상태',stateLabels[result.state]||result.state],['계획',result.plan_id],['실행 조건',result.snapshot_id||'아직 없음'],['요구사항',(def.requirement_ids||[]).join(', ')],['선행 작업',(def.depends_on||[]).join(', ')||'없음'],['우선순위',def.priority]])];
}
async function waitForJob(id){
 for(let i=0;i<80;i++){const {jobs}=await api('history');const job=jobs.find(j=>j.id===id);if(job&&!['queued','running'].includes(job.state))return job;await new Promise(r=>setTimeout(r,250));}
 throw new Error('조회가 오래 걸리고 있습니다. 실행 이력에서 결과를 확인해주세요.');
}
async function showReadOnly(op,task){
 const generation=++resultGeneration,UI=globalThis.DevRosterUI,box=$('action-result');
 $('action-result-title').textContent=op.id==='task.explain'?(task.state==='blocked'?'차단 원인':'상태와 대기 이유'):op.id==='task.usage'?'사용량':'작업 상세';
 $('action-result-body').replaceChildren(node('p','불러오는 중입니다.','hint'));box.hidden=false;box.focus();
 const load=async()=>{
  if(op.id==='task.explain')return explainView(await api('execution?'+new URLSearchParams({task:task.id,cursor:'0'})));
  const input={operation:op.id,fields:{task_id:task.id},options:{}};
  const preview=await api('preview',input);
  const job=await api('submit',{...input,preview_digest:preview.preview_digest,request_id:crypto.randomUUID()});
  const done=await waitForJob(job.id);
  if(done.state!=='completed')throw new Error(done.error||'조회하지 못했습니다.');
  return op.id==='task.usage'?usageView(done.result||{}):showView(done.result||{});
 };
 const button=$('primary-action').dataset.action===op.id&&!$('primary-action').hidden?$('primary-action'):null;
 try{const view=button&&UI?await UI.busy(button,load):await load();if(generation===resultGeneration&&selectedTask?.id===task.id)$('action-result-body').replaceChildren(...view);}
 catch(e){if(generation===resultGeneration)$('action-result-body').replaceChildren(node('p',UI?UI.errorMessage(e):e.message,'hint error-text'));}
}
function renderTasks(){
 renderStateFilter();
 const container=$('tasks'),needle=$('task-filter').value.trim().toLowerCase(),focused=document.activeElement;
 const focusId=focused?.classList.contains('task-choice')?focused.dataset.taskId:null;
 const existing=new Map([...container.querySelectorAll('.task-choice')].map(b=>[b.dataset.taskId,b]));
 const rows=taskRows.filter(t=>inGroup(t,stateGroup)&&[t.id,t.title].join(' ').toLowerCase().includes(needle));
 const wanted=[];
 for(const task of rows){
  let button=existing.get(task.id);
  if(!button){button=node('button',undefined,'task-choice');button.type='button';button.dataset.taskId=task.id;button.addEventListener('click',()=>{const current=taskRows.find(t=>t.id===button.dataset.taskId);if(current)selectTask(current);});}
  button.setAttribute('aria-pressed',String(selectedTask?.id===task.id));
  const signature=JSON.stringify([task.title,task.state]);
  if(button.dataset.signature!==signature){button.replaceChildren(node('strong',task.title),node('small',task.id),node('span',stateLabels[task.state]||task.state,'task-status badge '+task.state));button.dataset.signature=signature;}
  wanted.push(button);
 }
 for(const child of [...container.children])if(!wanted.includes(child))child.remove();
 wanted.forEach((button,i)=>{if(container.children[i]!==button)container.insertBefore(button,container.children[i]||null);});
 if(!rows.length)container.append(node('p',taskRows.length?'검색 결과가 없습니다. 검색어를 바꿔주세요.':'등록된 작업이 없습니다. 기획·화면 검토에서 새 작업을 준비해주세요.'));
 if(focusId&&document.activeElement!==focused){const replacement=wanted.find(b=>b.dataset.taskId===focusId);(replacement||$('task-filter')).focus();}
}
function taskTable(value){
 taskRows=value.tasks;
 if(selectedTask){
  const current=taskRows.find(t=>t.id===selectedTask.id);
  if(current){
   const changed=current.state!==selectedTask.state;selectedTask=current;
   $('selected-title').textContent=current.title;$('selected-state').textContent=stateLabels[current.state]||current.state;
   if(changed){
    invalidate();
    if(selected?.id.startsWith('task.'))choose(catalog.find(o=>o.id===defaultOperation(current.state)),current.id);
    renderActions(current);
    watchExecution(current.id).catch(error);
   }
  }else{
   selectedTask=null;invalidate();renderActions(null);closeResult();$('selected-id').textContent='';$('selected-title').textContent='목록에서 작업을 다시 선택해주세요.';$('selected-state').textContent='';
   if(selected?.id.startsWith('task.')&&$('field-task_id'))$('field-task_id').value='';
   clearTimeout(execTimer);execGen++;execTask='';$('task-detail').hidden=true;
  }
 }
 renderTasks();if(value.has_next)$('tasks').append(node('p','처음 100개 작업입니다. 검색이나 상태 필터로 범위를 좁혀주세요.'));
}
let execTask='',execCursor=0,execTimer=null,execGen=0;
const stateLabels={planned:'계획됨',approval_pending:'승인 대기',ready:'실행 준비',running:'실행 중',blocked:'차단됨',waiting_for_capacity:'AI 실행 대기',verifying:'검증 중',pr_ready:'게시 준비',user_review:'사용자 검토',feedback_required:'피드백 필요',completed:'완료',cancelled:'취소됨'};
const providerLabels={codex_cli:'Codex CLI',claude_cli:'Claude CLI',gemini_cli:'Gemini CLI',openai_api:'OpenAI API',anthropic_api:'Anthropic API',gemini_api:'Gemini API',xai_api:'Grok API',compatible_api:'호환 API'};
const duration=s=>s<60?Math.round(s)+'초':s<3600?Math.floor(s/60)+'분 '+Math.round(s%60)+'초':Math.floor(s/3600)+'시간 '+Math.floor(s%3600/60)+'분';
const fmtTime=s=>s?new Date(s*1000).toLocaleString('ko-KR'):'—';
function card(title,value,note){const c=node('div',undefined,'exec-card');c.append(node('span',title,'exec-label'),node('strong',value));if(note)c.append(node('small',note));return c;}
const sessionNames={analysis:'분석',design:'디자인',develop:'개발',verify:'검증',docs:'문서'},sessionStates={passed:'완료',running:'진행 중',failed:'실패',skipped:'건너뜀',not_started:'시작 전'};
function sessionCard(rows){const name=id=>{const [base,n]=id.split('-');return (sessionNames[base]||base)+(n?' '+n:'');};return card('세션',`${rows.filter(r=>r.state==='passed'||r.state==='skipped').length} / ${rows.length} 완료`,rows.map(r=>`${name(r.id)} ${sessionStates[r.state]||r.state}`).join(' · '));}
// Done steps (passed or skipped) out of all steps: the run's real progress. Stripes while it runs.
let execBar=null;
function executionProgress(data){
 const s=data.stages;if(!execBar||!execBar.element.isConnected){execBar=DevRosterProgress.create('작업 단계 진행');execBar.element.id='exec-progress';$('exec-state').after(execBar.element);}
 execBar.element.hidden=!s.total;if(!s.total)return;
 const done=s.passed+(s.skipped||0),running=['running','verifying'].includes(data.task.state);
 execBar.set(done/s.total,`단계 ${done} / ${s.total} 끝남${s.running?' · 진행 중 '+s.running:''}${s.awaiting_approval?' · 승인 대기':''}`,running?'active':'');
}
function renderExecution(data){$('execution-retry').hidden=true;$('exec-title').textContent=`${data.task.id} · ${data.task.title}`;$('exec-state').textContent=`${data.task.state === 'waiting_for_capacity' ? stateLabels.waiting_for_capacity : data.task.state_label}${data.started_at?' · 시작 '+fmtTime(data.started_at):''}${data.elapsed_seconds!=null&&['running','verifying'].includes(data.task.state)?' · 경과 '+duration(data.elapsed_seconds):''}`;
 executionProgress(data);
 const s=data.stages,c=data.current,cost=data.cost;$('exec-cards').hidden=!s.total&&!c.recorded;$('execution-empty').hidden=!!s.total||!!c.recorded;$('execution-empty').textContent='실행을 요청하면 이곳에서 진행과 결과를 확인할 수 있습니다.';$('exec-cards').replaceChildren(
  card('단계',`${s.passed} / ${s.total} 완료`,[s.running&&`진행 ${s.running}`,s.failed&&`실패 ${s.failed}`,s.skipped&&`건너뜀 ${s.skipped}`,s.awaiting_approval&&`승인 대기 ${s.awaiting_approval}`,s.not_started&&`시작 전 ${s.not_started}`].filter(Boolean).join(' · ')),
  card('현재 AI 선택',c.recorded?`${providerLabels[c.provider]||c.provider||'도구 미기록'} · ${c.model||'모델 미기록'}${c.effort?' · '+c.effort:''}`:'아직 기록 없음',c.reason?'선택 이유: '+c.reason:(c.phase?'진행 단계: '+c.phase:'')),
  ...(data.sessions&&data.sessions.length?[sessionCard(data.sessions)]:[]),
  card('대기',data.wait.capacity?'AI 사용량 대기':data.wait.lease_expires_at?'실행 소유권 유지 중':'대기 없음',data.wait.lease_expires_at?'만료 예정 '+fmtTime(data.wait.lease_expires_at)+'. '+data.wait.note:''),
  card('예상 비용',!cost||!cost.configured?'가격표 미설정':`${cost.currency} ${cost.known_cost}`,!cost?'승인 스냅샷 없음':!cost.configured?'프로젝트 설정에서 가격표를 입력하면 추정합니다.':`산정 ${cost.known_calls}회 · 미산정 ${cost.unknown_calls}회${cost.budget?' · 예산 대비 '+Math.round((cost.budget_ratio||0)*100)+'%':''}. ${cost.note}`));
 $('exec-actions').replaceChildren(...(data.next_actions.length?data.next_actions.map(r=>node('li',`${r.message} → ${r.action}`)):[node('li',data.terminal?'종료된 작업입니다.':'차단 사유가 없습니다.')]));
 for(const e of data.timeline){const li=node('li');const d=e.details||{};li.append(node('strong',e.label),node('span',' '+fmtTime(e.at)+' ',''),node('span',[d.phase_id,d.from&&d.to&&`${d.from}→${d.to}`,d.selection_model,d.reason,d.threshold&&`예산 ${Math.round(d.threshold*100)}% 도달(추정)`].filter(Boolean).join(' · '),'exec-meta'));$('exec-timeline').append(li);}
 execCursor=data.cursor;$('task-detail').hidden=data.task.state==='ready'&&!s.total&&!c.recorded&&!data.timeline.length&&!data.next_actions.length;}
async function pollExecution(gen){if(gen!==execGen)return;try{const data=await api('execution?'+new URLSearchParams({task:execTask,cursor:String(execCursor)}));if(gen!==execGen)return;renderExecution(data);if(data.terminal)return;execTimer=setTimeout(()=>pollExecution(gen),document.hidden?data.poll.hidden_ms:data.poll.active_ms);}catch(e){if(gen===execGen){$('exec-state').textContent=e.message||'실행 현황을 불러오지 못했습니다.';$('exec-cards').replaceChildren();$('exec-actions').replaceChildren(node('li','프로젝트 설정의 준비 상태와 선택한 작업을 확인해주세요.'));$('execution-retry').hidden=false;error(e);}}}
async function watchExecution(id){clearTimeout(execTimer);execGen+=1;execTask=id;execCursor=0;$('exec-timeline').replaceChildren();$('execution-empty').hidden=true;$('task-detail').hidden=false;$('exec-title').textContent=id+' · 실행 현황';$('exec-cards').replaceChildren();$('exec-actions').replaceChildren();$('execution-retry').hidden=true;$('exec-state').textContent='실행 현황을 불러오는 중…';$('exec-raw').textContent='불러오는 중…';const gen=execGen;api('task?id='+encodeURIComponent(id)).then(raw=>{if(gen===execGen)$('exec-raw').textContent=JSON.stringify(raw,null,2);}).catch(()=>{});await pollExecution(execGen);}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&execTask&&execTimer){clearTimeout(execTimer);pollExecution(execGen);}});
function history(jobs){$('history').replaceChildren();if(!jobs.length){$('history').append(node('p','실행 이력이 없습니다.'));return;}for(const job of jobs){const article=node('article',undefined,'job'),line=node('div',undefined,'job-line');line.append(node('strong',job.operation),node('span',states[job.state]||job.state,'state'),node('span',job.task_id||''),node('small',new Date(job.created_at*1000).toLocaleString()));if(['running','queued'].includes(job.state)){const button=node('button','실행 요청 취소');button.addEventListener('click',async()=>{button.disabled=true;try{await api('cancel',{id:job.id});await refresh();}catch(e){error(e);button.disabled=false;}});line.append(button);}article.append(line);const details=node('details'),summary=node('summary','결과 · '+job.id),pre=node('pre',JSON.stringify(job.result||{error:job.error||'진행 중',code:job.code},null,2));details.append(summary,pre);article.append(details);if(job.state==='completed'&&['backup.inspect','backup.restore'].includes(job.operation)&&job.result?.manifest){const download=node('button','검증 manifest 내려받기');download.type='button';download.addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(job.result.manifest,null,2)],{type:'application/json'})),link=node('a');link.href=url;link.download=job.id+'-manifest.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});article.append(download);}$('history').append(article);}}
async function refresh(){if(pollBusy)return;pollBusy=true;try{const [jobs,tasks]=await Promise.all([api('history'),api('tasks')]);history(jobs.jobs);taskTable(tasks);}finally{pollBusy=false;}}
$('task-filter').addEventListener('input',renderTasks);$('task-operation').addEventListener('change',()=>choose(catalog.find(o=>o.id===$('task-operation').value),selectedTask?.id));$('execution-retry').addEventListener('click',()=>watchExecution(execTask).catch(error));$('filter').addEventListener('input',renderCatalog);$('refresh').addEventListener('click',()=>refresh().catch(error));
$('primary-action').addEventListener('click',()=>startAction($('primary-action').dataset.action));$('more-actions').addEventListener('click',event=>openTaskMenu(event.currentTarget));$('action-result-close').addEventListener('click',()=>{closeResult();$('more-actions').focus();});
$('form').addEventListener('submit',async e=>{
 e.preventDefault();if(!selected||submitting)return;if(aiBlocked()){error(new Error(preparationReason));return;}
 invalidate();const generation=inputGeneration,request=++previewRequest,input=payload();
 $('preview-button').disabled=true;$('error').hidden=true;
 try{
  const value=await api('preview',input);
  if(generation!==inputGeneration||request!==previewRequest||JSON.stringify(input)!==JSON.stringify(payload()))return;
  checked={...input,preview_digest:value.preview_digest,request_id:crypto.randomUUID()};
  $('preview-summary').replaceChildren(node('p',value.label),node('p','프로젝트: '+value.project),...value.notes.map(t=>node('p',t)));
  $('preview-detail').textContent=JSON.stringify(value,null,2);$('preview').hidden=false;$('preview').tabIndex=-1;$('preview').focus();$('execute').disabled=false;
 }catch(e){if(generation===inputGeneration&&request===previewRequest)error(e);}
 finally{if(request===previewRequest)$('preview-button').disabled=aiBlocked();}
});
$('preview-cancel').addEventListener('click',()=>{invalidate();($('primary-action').hidden||$('task-actions').hidden?$('operation-editor'):$('primary-action')).focus();});
$('execute').addEventListener('click',async()=>{
 if(!checked||submitting)return;if(aiBlocked()){invalidate();error(new Error(preparationReason));return;}
 const current=payload();
 if(JSON.stringify(current)!==JSON.stringify({operation:checked.operation,fields:checked.fields,options:checked.options})){invalidate();error(new Error('입력 내용이 바뀌었습니다. 입력·범위를 다시 확인해주세요.'));return;}
 const confirmed=checked;submitting=true;$('execute').disabled=true;
 try{const job=await api('submit',confirmed);$('result').textContent='실행 요청이 등록되었습니다: '+job.id+'\n실행 이력에서 진행과 결과를 확인할 수 있습니다.';globalThis.DevRosterUI?.toast('실행 요청을 등록했습니다. 진행은 실행 현황에서 확인할 수 있습니다.',{type:'success'});$('result').closest('details').open=true;invalidate();await refresh();}
 catch(e){error(e);}
 finally{submitting=false;$('execute').disabled=!checked;}
});
$('doctor-run')?.addEventListener('click',async event=>{const UI=globalThis.DevRosterUI,button=event.currentTarget;
 const run=async()=>{const response=await fetch('/api/doctor',{cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.error||'환경 진단에 실패했습니다.');return data;};
 try{const data=UI?await UI.busy(button,run):await run();const failed=Number(data.failed||0),warnings=Number(data.warnings||0);
  UI?.toast(failed||warnings?'실패 '+failed+'개 · 주의 '+warnings+'개 — 프로젝트 설정의 준비 상태에서 항목별로 확인하세요.':'Git·AI 실행 도구·작업 공간에 문제가 없습니다.',{type:failed?'error':warnings?'info':'success',title:'환경 진단을 마쳤습니다',action:failed||warnings?{label:'준비 상태 열기',run:()=>{location.href='/setup';}}:null});}
 catch(e){UI?.toast(UI.errorMessage(e),{type:'error',title:'환경 진단을 하지 못했습니다'});}
});
(async()=>{try{const data=await api('bootstrap');token=data.token;catalog=data.catalog.operations;for(const op of catalog.filter(o=>o.fields&&o.id.startsWith('task.'))){const option=node('option',op.label);option.value=op.id;$('task-operation').append(option);}context=data.context||{};$('workspace').textContent=data.workspace+(context.preparation_mode?' · 준비 모드':'');for(const link of data.catalog.links.filter(link=>['studio','setup'].includes(link.label))){const a=node('a',({studio:'기획·화면 검토',setup:'프로젝트 설정',workflow:'워크플로',integration:'연결',source:'소스 탐색',figma:'Figma 참조',stitch:'Stitch 참조',dashboard:'작업 현황',init:'프로젝트 준비'})[link.label]||link.label,'link');a.href=context.preparation_mode&&link.label==='setup'?'/setup#ai':link.route;$('links').append(a);}renderCatalog();taskTable(data.tasks);history(data.jobs);const params=new URLSearchParams(location.search);choose(catalog.find(o=>o.id===params.get('operation'))||catalog.find(o=>o.id==='task.run'));const linked=taskRows.find(t=>t.id===params.get('task'));if(linked)selectTask(linked,false);else if(!params.has('operation')&&taskRows.length)selectTask(taskRows[0],false);for(const field of selected.fields){if(params.has(field.name))$('field-'+field.name).value=params.get(field.name);}if(params.has('operation'))showEditor(true);setInterval(()=>refresh().catch(error),3000);}catch(e){error(e);}})();
