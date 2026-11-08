/* Canonical project facts only; checks run through explicit settings actions. */
(() => {
  'use strict';
// Readable failure text: network/parse errors become a Korean hint, server messages pass through.
  const errText = error => globalThis.DevRosterUI ? globalThis.DevRosterUI.errorMessage(error) : (error?.message || String(error));
  const taskStates={planned:'계획됨',approval_pending:'승인 대기',ready:'실행 준비',running:'진행 중',blocked:'조치 필요',waiting_for_capacity:'실행 자리 대기',verifying:'검증 중',pr_ready:'변경 검토 준비',user_review:'사용자 검토',feedback_required:'피드백 반영 필요',completed:'완료',cancelled:'취소'};
  const $=id=>document.getElementById(id);
  const make=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  let generation=0;
  const remembered='devroster.home.project';
  const tone=state=>['running','verifying'].includes(state)?'active':['pr_ready','user_review','feedback_required','blocked','approval_pending'].includes(state)?'attention':'idle';
  const readinessLabels={ready:'준비됨',unknown:'검사 전',failed:'확인 필요',missing:'준비 필요'};
  const readinessTone={ready:'ok',unknown:'idle',failed:'attention',missing:'attention'};
  const remember=id=>{try{if(id)localStorage.setItem(remembered,id);}catch{}};
  const recall=()=>{try{return localStorage.getItem(remembered)||'';}catch{return '';}};
  function dot(kind,label){const span=make('span');span.className='status-dot '+kind;span.append(make('i'),make('span',label));return span;}
  function readinessRow(item){
    // One clickable row per check: state at a glance, reason inline, the check opens on click.
    const row=make('a');row.className='readiness-row';row.href=item.action;
    const name=make('span',item.item);name.className='readiness-name';
    const reason=make('span',item.reason);reason.className='readiness-reason';
    if(item.checked_at)reason.title='마지막 확인: '+new Date(item.checked_at*1000).toLocaleString();
    const arrow=make('span','›');arrow.className='readiness-arrow';arrow.setAttribute('aria-hidden','true');
    row.append(name,dot(readinessTone[item.status]||'idle',readinessLabels[item.status]||item.status),reason,arrow);
    row.setAttribute('aria-label',item.item+' · '+(readinessLabels[item.status]||item.status)+' · '+item.reason);
    return row;
  }
  function taskTarget(task){return ['pr_ready','user_review'].includes(task.state)?'/review?task='+encodeURIComponent(task.id):'/agent?operation=task.run&task_id='+encodeURIComponent(task.id);}
  async function fetchBootstrap(projectId){const response=await fetch('/api/home/bootstrap'+(projectId?'?project_id='+encodeURIComponent(projectId):''));const data=await response.json();if(!response.ok)throw new Error(data.error||'프로젝트를 불러오지 못했습니다.');return data;}
  async function load(projectId){
    const current=++generation;$('home-loading').hidden=false;$('home-error').hidden=true;$('home-retry').hidden=true;
    try{
      const data=await fetchBootstrap(projectId);if(current!==generation)return;
      $('project-section').hidden=false;$('project-select').replaceChildren();
      if(!data.projects.length){$('home-empty').hidden=false;$('project-select').append(make('option','등록된 프로젝트 없음'));}else{$('home-empty').hidden=true;for(const project of data.projects){const option=make('option',project.name+' · '+project.id);option.value=project.id;$('project-select').append(option);}$('project-select').value=data.selected?.id||'';}
      if(data.selected){remember(data.selected.id);$('home-context').textContent='개발 프로젝트 · '+data.selected.name;}
      $('readiness-cards').replaceChildren();
      for(const item of data.readiness)$('readiness-cards').append(readinessRow(item));
      $('next-action-card').hidden=false;$('next-action-content').replaceChildren();const action=make('a',data.primary_action.label);action.className='button primary';action.href=data.primary_action.href;$('next-action-content').append(action);
      $('current-task').textContent=data.current_task?'현재 작업: '+data.current_task.id+' · '+data.current_task.title+' · '+(taskStates[data.current_task.state]||data.current_task.state):'현재 진행 중인 작업이 없습니다.';
      $('tasks-list').replaceChildren();
      for(const task of data.tasks_in_progress){const row=make('tr');if(data.current_task&&task.id===data.current_task.id)row.className='current';const cell=make('td');const link=make('a');link.href=taskTarget(task);link.append(make('strong',task.id),make('span',task.title));cell.append(link);const state=make('td');state.append(dot(tone(task.state),taskStates[task.state]||task.state));const next=make('td');const go=make('a',['pr_ready','user_review'].includes(task.state)?'변경 검토':'작업실');go.href=taskTarget(task);go.className='row-link';next.append(go);row.append(cell,state,next);$('tasks-list').append(row);}
      $('tasks-empty').hidden=!!data.tasks_in_progress.length;$('tasks-table').hidden=!data.tasks_in_progress.length;$('tasks-section').hidden=false;
      const trace=$('home-trace-link');trace.href='/traceability'+(data.selected?'?project_id='+encodeURIComponent(data.selected.id):'');
    }catch(error){if(current===generation){$('home-error').hidden=false;$('home-retry').hidden=false;$('home-error').textContent=errText(error);}}finally{if(current===generation)$('home-loading').hidden=true;}
  }
  $('project-select').addEventListener('change',()=>{const id=$('project-select').value;const url=new URL(location.href);url.searchParams.set('project_id',id);history.replaceState(null,'',url);load(id);});
  $('home-retry').addEventListener('click',()=>load($('project-select').value));
  load(new URLSearchParams(location.search).get('project_id')||recall());
})();
