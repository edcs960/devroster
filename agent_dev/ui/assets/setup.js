(() => {
  'use strict';
  // Readable failure text: network/parse errors become a Korean hint, server messages pass through.
  const errText = error => globalThis.DevRosterUI ? globalThis.DevRosterUI.errorMessage(error) : (error?.message || String(error));
  // The login check follows the visible option only; it is hidden while shared AI tools run.
  const authCheck = () => !$('check-auth-label').hidden && $('check-auth').checked;
  // Shared confirmation dialog (ui-feedback.js); falls back to the browser dialog if it is missing.
  const ask = (title, body, confirm, opts = {}) => globalThis.DevRosterUI ? globalThis.DevRosterUI.confirmAction(title, body, confirm, opts) : Promise.resolve(window.confirm(title + '\n' + body));
  const $ = id => document.getElementById(id);
  const state = {token:'',settings:null,presets:{},catalog:{},providers:{},roleCatalog:[],skillCatalog:{bundles:{}},skills:null,sharedAi:null,aiTools:null,roles:null,dirty:false,pending:false,folderTarget:null,selectedRole:''};
  const make = (tag,text,cls) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
  const notice = (text,error=false) => {$('notice').classList.toggle('instruction',text==='설정을 확인하고 저장해주세요.');$('notice').textContent=text;$('notice').classList.toggle('error',error);};
  const stepIds=['project','ai','roles','development','environment','diagnosis'];
  function syncContext(id=location.hash.slice(1)||'project') {
    const names={project:'프로젝트',ai:'AI 도구',roles:'개발 역할',development:'개발·검증',environment:'환경변수·연동',diagnosis:'준비 상태'};
    const help={project:'프로젝트 폴더와 모듈의 수정 허용 경로를 확인하세요.',ai:'도구·모델 선택은 다음 생성/개발 작업에 사용합니다. 유료 실행은 별도입니다.',roles:'역할 순서 변경은 저장 후 프로젝트 Workflow에 반영됩니다.',development:'검증 명령과 미리보기 서비스는 승인 작업의 기준입니다.',environment:'비밀값은 다시 표시하지 않습니다. 유지·교체·삭제를 구분하세요.',diagnosis:'저장한 설정을 검사합니다. 진단 성공이 실제 AI 실행 성공을 뜻하지 않습니다.'};
    if(!$('setup-context-step'))return;
    $('setup-context-step').textContent=names[id]||names.project;
    $('setup-context-help').textContent=state.settings?.preparation_mode&&id==='ai'?'준비 모드 · 기본 AI를 활성화하고 인증을 확인하면 AI 작업을 실행할 수 있습니다.':help[id]||help.project;
    $('setup-context-name').textContent=$('name').value||'프로젝트 이름 미입력';
    $('setup-context-revision').textContent=state.dirty?'변경됨 · 저장 필요':$('revision').textContent;
    const heading=document.querySelector('.intro h1');if(heading)heading.textContent='설정 / '+(names[id]||names.project);
  }
  function activateStep(id,focus=false,syncUrl=true) {
    if(!stepIds.includes(id))id='project';
    for(const key of stepIds)$(key).hidden=key!==id;
    for(const link of document.querySelectorAll('#setup-steps a')) {
      if(link.hash==='#'+id)link.setAttribute('aria-current','step');else link.removeAttribute('aria-current');
    }
    if(syncUrl&&location.hash!=='#'+id)history.replaceState(null,'','#'+id);
    syncContext(id);
    if(focus){const heading=$(id).querySelector('h2');heading.tabIndex=-1;heading.focus();}
  }
  for(const link of document.querySelectorAll('#setup-steps a'))link.addEventListener('click',event=>{
    event.preventDefault();activateStep(link.hash.slice(1),true);
  });
  window.addEventListener('hashchange',()=>activateStep(location.hash.slice(1),true));
  activateStep(location.hash.slice(1),false,false);
  let invalidTarget=null;
  $('settings-form').addEventListener('click',()=>queueMicrotask(()=>syncContext()));
  $('settings-form').addEventListener('input',()=>{state.dirty=true;syncContext();});
  $('settings-form').addEventListener('change',()=>{state.dirty=true;syncContext();});
  $('settings-form').addEventListener('invalid',event=>{
    event.preventDefault();if(invalidTarget)return;invalidTarget=event.target;
    const panel=invalidTarget.closest('[data-setup-step]');if(panel)activateStep(panel.id);
    let ancestor=invalidTarget.parentElement;while(ancestor){if(ancestor.tagName==='DETAILS')ancestor.open=true;ancestor=ancestor.parentElement;}
    invalidTarget.setAttribute('aria-invalid','true');notice('입력 확인: '+invalidTarget.validationMessage,true);
    requestAnimationFrame(()=>{invalidTarget.focus();invalidTarget=null;});
  },true);
  $('settings-form').addEventListener('input',event=>event.target.removeAttribute('aria-invalid'));
  function field(parent,label,value,type='text',name='',options=null) {
    const wrap=make('div'),id='field-'+crypto.randomUUID(),lab=make('label',label);
    lab.htmlFor=id;const input=make(options?'select':'input');input.id=id;input.dataset.field=name;if(!options)input.type=type;
    if(options)options.forEach(([val,text])=>{const option=make('option',text);option.value=val;input.append(option);});
    input.value=value??'';wrap.append(lab,input);parent.append(wrap);return input;
  }
  function removeButton(card) {const b=make('button','삭제','danger');b.type='button';b.addEventListener('click',()=>{card.remove();state.dirty=true;refreshModuleChoices();});return b;}
  async function api(path,payload) {
    const response=await fetch('/api/setup/'+path,{method:payload?'POST':'GET',headers:payload?{'Content-Type':'application/json','X-Setup-Token':state.token}:undefined,body:payload?JSON.stringify(payload):undefined});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'요청을 처리하지 못했습니다.');return data;
  }
  function sourceRoot() {return state.settings&&$('root').value===state.settings.root?state.settings.configuration_root:$('root').value;}
  function moduleCard(row) {
    const card=make('div',undefined,'card module-row');card.dataset.kind='module';const header=make('div',undefined,'card-title');header.append(make('h3','소스 모듈'),removeButton(card));card.append(header);
    const cols=make('div',undefined,'columns');const name=field(cols,'모듈 이름 (source/frontend/backend)',row.id,'text','id');
    const preset=field(cols,'기술 프리셋',row.preset,'text','preset',Object.keys(state.presets).map(p=>[p,({generic:'직접 설정',react:'React / npm',python:'Python', 'spring-boot':'Spring Boot / Maven','spring-boot-gradle':'Spring Boot / Gradle'})[p]]));card.append(cols);
    const pathRow=make('div',undefined,'path-row');const path=field(pathRow,'폴더 경로 (상대 경로 또는 절대 경로)',row.path,'text','path');path.parentElement.className='path-field';const browse=make('button','폴더 선택','secondary');browse.type='button';browse.addEventListener('click',()=>openFolders(path));pathRow.append(browse);card.append(pathRow);
    const advanced=make('details',undefined,'module-advanced');advanced.append(make('summary','수정 허용 경로·Git 기준'));card.append(advanced);const allowed=field(advanced,'수정 허용 경로 (모듈 폴더 기준, 쉼표 구분)',row.allowed_paths.join(', '),'text','allowed_paths');
    field(advanced,'Git 기준 ref',row.base_ref||'HEAD','text','base_ref');advanced.append(make('p','예: src, tests, package.json. 전체 폴더(.)는 허용하지 않습니다.','hint'));
    preset.addEventListener('change',()=>{allowed.value=state.presets[preset.value].paths.join(', ');});name.addEventListener('change',refreshModuleChoices);
    $('modules').append(card);refreshModuleChoices();
  }
  function refreshModuleChoices() {
    const names=[...$('modules').children].map(card=>card.querySelector('[data-field=id]').value);
    [...document.querySelectorAll('#gates [data-field=module], #preview-services [data-field=module]')].forEach(select=>{const value=select.value;select.replaceChildren();names.forEach(key=>{const opt=make('option',key);opt.value=key;select.append(opt);});select.value=names.includes(value)?value:(names[0]||'');});
    if(state.skills){state.skills={...state.skills,...collectSkills()};renderSkills();}
  }
  function argvText(argv) {return argv.map(x=>/\s|["']/.test(x)?JSON.stringify(x):x).join(' ');}
  function parseArgs(value) {
    const args=[];let word='',quote='',started=false;
    for(let i=0;i<value.length;i++) {const c=value[i];if(c==='\\'&&i+1<value.length&&value[i+1]===quote&&quote){word+=value[++i];started=true;}else if(quote){if(c===quote)quote='';else word+=c;}else if(c==='"'||c==="'"){quote=c;started=true;}else if(/\s/.test(c)){if(started){args.push(word);word='';started=false;}}else{word+=c;started=true;}}
    if(quote)throw new Error('검증 명령의 따옴표를 닫아주세요.');if(started)args.push(word);return args;
  }
  function gateCard(row) {
    const card=make('div',undefined,'card');card.dataset.kind='gate';const header=make('div',undefined,'card-title');header.append(make('h3','완료 확인 명령'),removeButton(card));card.append(header);
    const cols=make('div',undefined,'columns');field(cols,'검증 이름',row.name,'text','name');field(cols,'실행할 모듈',row.module,'text','module',[...$('modules').children].map(c=>{const key=c.querySelector('[data-field=id]').value;return [key,key];}));card.append(cols);
    field(card,'명령 (실행파일 + 인수)',argvText(row.command||[]),'text','command');card.append(make('p','예: npm run build / python -m pytest -q. &&, 파이프 등 shell 문법은 사용하지 않습니다.','hint'));
    const advanced=make('details',undefined,'gate-advanced');advanced.append(make('summary','실행 폴더·시간·필수 검증'));card.append(advanced);const second=make('div',undefined,'columns');field(second,'실행 폴더 (모듈 기준)',row.cwd||'.','text','cwd');field(second,'제한시간 (초)',row.timeout_seconds||900,'number','timeout_seconds');advanced.append(second);
    const label=make('label',undefined,'check'),check=make('input');check.type='checkbox';check.checked=row.required!==false;check.dataset.field='required';label.append(check,document.createTextNode('필수 검증: 실패하면 개발 완료를 차단'));advanced.append(label);$('gates').append(card);
  }
  function previewCard(row={}) {
    const card=make('div',undefined,'card');card.dataset.kind='preview';const header=make('div',undefined,'card-title');header.append(make('h3','미리보기 서비스'),removeButton(card));card.append(header);
    const cols=make('div',undefined,'columns');field(cols,'서비스 ID (영문 소문자)',row.id||'web','text','id');field(cols,'모듈',row.module||'','text','module',[...$('modules').children].map(c=>{const key=c.querySelector('[data-field=id]').value;return [key,key];}));card.append(cols);
    field(card,'실행 명령 (실행파일 + 인수)',argvText(row.argv||[]),'text','argv');card.append(make('p','예: npm run dev -- --port 5173. 비밀 값은 넣지 말고 환경변수 참조를 사용합니다.','hint'));
    const advanced=make('details',undefined,'gate-advanced');advanced.append(make('summary','실행 폴더·시간·필수 검증'));card.append(advanced);const second=make('div',undefined,'columns');field(second,'실행 폴더 (모듈 기준)',row.cwd||'.','text','cwd');field(second,'포트',row.port||5173,'number','port');field(second,'준비 확인 경로',row.readiness_path||'/','text','readiness_path');field(second,'시작 대기 (초)',row.startup_timeout||30,'number','startup_timeout');card.append(second);
    field(card,'전달할 환경변수 이름 (쉼표 구분)',(row.env_refs||[]).join(', '),'text','env_refs');field(card,'추가 허용 origin (쉼표 구분, 예: http://127.0.0.1:8000)',(row.allowed_origins||[]).join(', '),'text','allowed_origins');
    $('preview-services').append(card);
  }
  const rateText=v=>v===null||v===undefined?'':String(v);
  function priceCard(row={}) {
    const card=make('div',undefined,'card');card.dataset.kind='price';const header=make('div',undefined,'card-title');header.append(make('h3','모델 가격 (100만 토큰당)'),removeButton(card));card.append(header);
    const cols=make('div',undefined,'columns');field(cols,'AI 도구',row.provider||'codex_cli','text','provider',Object.entries(state.providers).map(([p,info])=>[p,info.label]));field(cols,'모델 이름',row.model||'','text','model');field(cols,'적용 시작일 (YYYY-MM-DD)',row.valid_from&&row.valid_from!=='1970-01-01'?row.valid_from:'','text','valid_from');card.append(cols);
    const rates=make('div',undefined,'columns');field(rates,'입력 단가',rateText(row.input_per_million),'number','input_per_million');field(rates,'캐시 입력 단가 (선택)',rateText(row.cached_input_per_million),'number','cached_input_per_million');field(rates,'출력 단가',rateText(row.output_per_million),'number','output_per_million');rates.querySelectorAll('input').forEach(i=>{i.step='any';i.min='0';});card.append(rates);
    $('price-book').append(card);
  }
  function renderPolicy(policy) {
    $('cost-currency').value=policy?.currency||'USD';$('cost-budget').value=rateText(policy?.estimated_budget);$('cost-thresholds').value=(policy?.alert_thresholds||[]).map(v=>Math.round(v*10000)/100).join(', ');
    $('price-book').replaceChildren();(policy?.price_book||[]).forEach(priceCard);
  }
  function collectPolicy() {
    const num=v=>{if(v.trim()==='')return null;const n=Number(v);if(!Number.isFinite(n))throw new Error('비용 설정에는 숫자를 입력해주세요.');return n;};
    const book=[...$('price-book').children].map(card=>{const get=k=>card.querySelector(`[data-field=${k}]`).value;const row={provider:get('provider'),model:get('model').trim(),input_per_million:num(get('input_per_million')),cached_input_per_million:num(get('cached_input_per_million')),output_per_million:num(get('output_per_million'))};if(get('valid_from').trim())row.valid_from=get('valid_from').trim();return row;});
    const thresholds=$('cost-thresholds').value.split(',').map(v=>v.trim()).filter(Boolean).map(v=>num(v)/100),budget=num($('cost-budget').value);
    if(!book.length&&!thresholds.length&&budget===null)return null;
    return {currency:$('cost-currency').value,alert_thresholds:thresholds,estimated_budget:budget,price_book:book};
  }
  function envRow(row={key:'',value:''}) {
    const card=make('div',undefined,'card env-row'),cols=make('div',undefined,'columns');field(cols,'변수 이름',row.key,'text','key');const input=field(cols,'값 (저장 후 숨김)',row.value,'password','value');input.autocomplete='new-password';card.append(cols);
    const original=row.value||'',existing=!!original,group=make('fieldset'),groupName='env-mode-'+crypto.randomUUID();group.append(make('legend','변경 방식'));
    const summary=make('p',undefined,'hint'),radios=[];
    function select(mode){card.dataset.remove=String(mode==='delete');input.readOnly=mode!=='replace';if(mode==='keep')input.value=original;summary.textContent=mode==='delete'?'설정 저장 시 삭제합니다. 저장 전 유지/교체로 되돌릴 수 있습니다.':mode==='keep'?'기존 비밀값을 유지합니다. 원본 값을 다시 표시하지 않습니다.':'새 값을 입력하세요. 설정 저장 후 교체합니다.';}
    for(const [mode,label] of [['keep','유지'],['replace','새 값으로 교체'],['delete','삭제']]){const wrap=make('label',undefined,'check'),radio=make('input');radio.type='radio';radio.name=groupName;radio.value=mode;radio.checked=mode===(existing?'keep':'replace');radio.addEventListener('change',()=>select(mode));wrap.append(radio,document.createTextNode(label));group.append(wrap);radios.push(radio);}
    card.append(group,summary);select(existing?'keep':'replace');$('env-rows').append(card);
  }
  function runnerCard(provider,row) {
    const card=make('details',undefined,'card');card.dataset.provider=provider;card.open=provider===$('provider').value;const info=state.providers[provider],isApi=info.kind==='api';card.append(make('summary',info.label),make('p',isApi?'API 생성 · 기획·명세·목업과 텍스트 단계':'개발 CLI · 파일 읽기·수정과 기획 생성','hint'));
    const label=make('label',undefined,'check'),check=make('input');check.type='checkbox';check.checked=row.enabled;check.dataset.field='enabled';label.append(check,document.createTextNode('이 도구 사용'));card.append(label);
    if(isApi){field(card,'API 주소',row.base_url,'url','base_url');field(card,'인증 환경변수 이름 (값은 아래 환경변수에 등록)',row.credential_ref,'text','credential_ref');field(card,'출력 토큰 상한',row.max_output_tokens,'number','max_output_tokens');field(card,'추론 옵션',row.reasoning,'text','reasoning',[['native','모델이 지원하는 추론 옵션 전달'],['none','추론 옵션 전달하지 않음']]);const add=make('button','인증 키 입력란 추가','secondary');add.type='button';add.addEventListener('click',()=>{const key=card.querySelector('[data-field=credential_ref]').value;if(!key){notice('인증 없는 로컬 서버는 키 입력이 필요하지 않습니다.');return;}if(!cards('env-rows').some(r=>r.key===key))envRow({key,value:''});activateStep('environment');const target=[...$('env-rows').children].find(r=>r.querySelector('[data-field=key]').value===key);target?.querySelector('[data-field=value]').focus();state.dirty=true;});card.append(add,make('p','API 모델과 키는 해당 서비스에서 준비합니다. 로컬 호환 서버는 HTTP loopback 주소와 인증 없는 연결을 사용할 수 있습니다.','hint'));}else{field(card,'실행파일 (이름 또는 절대 경로)',row.command,'text','command');if(['codex_cli','claude_cli'].includes(provider))card.append(cliLoginBox(provider));else card.append(make('p','로그인: '+info.login,'hint'));}field(card,isApi?'고정 모델 (자동 선택이 아니면 필수)':'고정 모델 (선택)',row.model,'text','model');
    field(card,'고정 추론 강도',row.effort||'','text','effort',[['','도구 기본값'],...(provider==='gemini_cli'?[]:provider==='gemini_api'||provider==='xai_api'?['low','medium','high']:provider==='anthropic_api'?['low','medium','high','max']:['low','medium','high','xhigh','max']).map(v=>[v,v])]);
    const policy=row.routing||{mode:'fixed',max_effort:'high',profiles:{}};
    const routeField=(parent,label,value,name,options)=>{const input=field(parent,label,value,'text','',options);delete input.dataset.field;input.dataset.routeField=name;return input;};
    const mode=routeField(card,'선택 방식',policy.mode,'mode',[['fixed','모델·강도 고정'],['auto','작업에 따라 자동 선택 (균형형)']]);
    const body=make('div',undefined,'routing-profiles');
    routeField(body,'자동 선택의 최대 추론 강도',policy.max_effort,'max_effort',['low','medium','high'].map(v=>[v,v]));
    ['simple','standard','complex'].forEach((tier,index)=>{const row=policy.profiles[tier]||{model:'',supported_efforts:[]},group=make('div',undefined,'routing-tier');group.dataset.tier=tier;group.append(make('h4',(['단순 작업 · 목표 low','일반 개발 · 목표 medium','설계·보안·DB · 목표 high'])[index]));routeField(group,'사용할 모델',row.model,'model');routeField(group,'모델 지원 강도 (쉼표 구분)',row.supported_efforts.join(', '),'supported_efforts');body.append(group);});
    body.append(make('p','지원 강도는 모델마다 다릅니다. 예: low, medium, high. 빈칸이면 도구 기본 강도를 사용합니다. Gemini CLI는 강도 목록을 비워주세요. 자동 선택은 모델을 등록한 뒤 적용됩니다.','hint'));
    const fill=make('button','로컬 CLI 모델 후보로 채우기','secondary');fill.type='button';fill.disabled=!(state.catalog[provider]?.length);fill.addEventListener('click',()=>{const models=state.catalog[provider]||[],tags=['luna','sol','astra'];[...body.querySelectorAll('[data-tier]')].forEach((group,index)=>{const row=models.find(m=>m.model.includes(tags[index]))||models[0];group.querySelector('[data-route-field=model]').value=row.model;group.querySelector('[data-route-field=supported_efforts]').value=row.supported_efforts.join(', ');});state.dirty=true;});fill.dataset.catalogUnavailable=state.catalog[provider]?.length?'':'true';body.append(fill,make('p',provider==='codex_cli'?'로컬 Codex 공개 목록을 참고합니다. 목록·이름은 실제 계정 권한이나 가격 순위를 보장하지 않습니다.':'모델·지원 강도는 제공자에서 확인해 직접 등록해주세요. 계정 접근 권한은 별도로 필요합니다.','hint'));
    const sync=()=>body.hidden=mode.value!=='auto';mode.addEventListener('change',sync);sync();card.append(body);const fields=make('div',undefined,'runner-fields');for(const child of [...card.children].slice(1))fields.append(child);card.append(fields);$('runners').append(card);
  }
  const loginLabel=value=>value===true?'앱 전용 로그인: 로그인됨':value===false?'앱 전용 로그인: 로그인 필요':'앱 전용 로그인 상태 확인 중…';
  function cliLoginBox(provider) {
    const box=make('details',undefined,'cli-login'),status=make('p',loginLabel(),'hint'),start=make('button','앱 전용 로그인','secondary'),cancel=make('button','로그인 취소','secondary'),out=make('pre');
    box.append(make('summary','앱 전용 로그인·인증'));status.dataset.cliStatus=provider;status.setAttribute('role','status');start.type=cancel.type='button';cancel.hidden=out.hidden=true;
    box.append(make('p','DevRoster 전용 로그인입니다. 개인 터미널의 Codex·Claude 로그인과 분리되며, 저장된 실행파일로 실행합니다.','hint'),status,start,cancel,out);
    let job='';
    const show=data=>{out.hidden=!data.output;out.textContent=data.output||'';cancel.hidden=data.done;start.disabled=!data.done;
      if(data.done){status.textContent=loginLabel(data.logged_in);notice(data.logged_in?'앱 전용 로그인을 완료했습니다.':'로그인이 완료되지 않았습니다. 출력 내용을 확인해주세요.',!data.logged_in);}};
    const poll=async()=>{try{const data=await api('cli-login-status',{root:sourceRoot(),job_id:job});show(data);if(!data.done)setTimeout(poll,2000);}catch(e){notice(errText(e),true);start.disabled=false;}};
    start.addEventListener('click',async()=>{if(state.dirty){notice('실행파일 변경을 먼저 저장한 뒤 로그인해주세요.',true);return;}try{start.disabled=true;status.textContent='로그인 진행 중: 아래 안내에 따라 브라우저에서 로그인하세요.';const data=await api('cli-login',{root:sourceRoot(),provider});job=data.job_id;show(data);if(!data.done)setTimeout(poll,1000);}catch(e){notice(errText(e),true);start.disabled=false;}});
    cancel.addEventListener('click',async()=>{try{show(await api('cli-login-cancel',{root:sourceRoot(),job_id:job}));}catch(e){notice(errText(e),true);}});
    return box;
  }
  async function refreshCliAuth() {try{const data=await api('cli-auth',{root:sourceRoot()});for(const el of document.querySelectorAll('[data-cli-status]'))el.textContent=loginLabel(data[el.dataset.cliStatus]);}catch(e){for(const el of document.querySelectorAll('[data-cli-status]'))el.textContent='앱 전용 로그인 상태를 확인하지 못했습니다.';}}
  function render(settings) {
    state.settings=settings;state.dirty=false;$('context-token').value=settings.context_policy?.token_mode||'balanced';$('context-ponytail').value=settings.context_policy?.ponytail||'off';$('root').value=settings.root;$('name').value=settings.name;$('project-id').value=settings.project_id;$('project-id').disabled=settings.profile_version>0;
    $('modules').replaceChildren();$('gates').replaceChildren();$('preview-services').replaceChildren();settings.modules.forEach(moduleCard);settings.verification.forEach(gateCard);(settings.preview_services||[]).forEach(previewCard);renderPolicy(settings.usage_policy);
    $('provider').replaceChildren();Object.entries(state.providers).forEach(([p,info])=>{const option=make('option',info.label);option.value=p;$('provider').append(option);});$('provider').value=settings.ai.provider;$('runners').replaceChildren();Object.keys(state.providers).forEach(p=>runnerCard(p,settings.ai.runners[p]));
    $('limits').replaceChildren();const labels={max_attempts:'최대 AI 실행 횟수',max_runner_seconds:'총 AI 실행 시간 (초)',phase_timeout_seconds:'단계별 제한시간 (초)',max_reentries:'재진입 횟수',session_timeout_seconds:'세션 제한시간 (초, 0: 역할별 합)'};Object.entries(settings.limits).forEach(([k,v])=>field($('limits'),labels[k],v,'number',k));
    $('ports').replaceChildren();Object.entries(settings.ports).forEach(([k,v])=>field($('ports'),({setup:'설정 화면',studio:'기획·화면 검토',dashboard:'개발 현황'})[k]+' 포트',v,'number',k));
    state.roles=structuredClone(settings.roles);$('role-domain').value=state.roles.domain;renderRoles();
    state.skills={bundles:[],repositories:{},roles:{},exclude:[],...(settings.skills||{})};renderSkills();$('skill-status').replaceChildren();loadRules();
    state.aiTools=settings.ai_tools?{selected:[...settings.ai_tools.selected]}:null;renderSharedAi();
    $('verification-scope').value=settings.verification_scope||'all';$('completion-mode').value=settings.completion_mode||'local';$('test-paths').value=(settings.test_paths||[]).join(', ');$('document-paths').value=(settings.document_paths||[]).join(', ');$('wiki-dir').value=settings.wiki_dir||'';renderStructuredWarning();
    $('env-rows').replaceChildren();settings.environment.forEach(envRow);$('revision').textContent=settings.revision?`설정 ${settings.revision} · 프로필 ${settings.profile_version}`:'아직 저장하지 않음';if(settings.preparation_mode)$('revision').textContent+=' · 준비 모드';$('checks').replaceChildren(make('p','저장된 설정으로 준비 상태를 확인해주세요.','hint'));$('next-actions').hidden=!settings.revision;syncContext();
    const quoted="'"+settings.root.replace(/'/g,/^[A-Za-z]:/.test(settings.root)?"''":"'\\''")+"'";$('commands').textContent=`devroster studio --root ${quoted}\ndevroster dashboard --root ${quoted}\ndevroster setup --check --check-auth --root ${quoted}`;
  }
  const catalogIds=()=>state.roleCatalog.map(r=>r.id);
  // Shared AI tools: the project only picks tools and their order; /ai manages the rest.
  async function loadSharedAi() {try{const r=await fetch('/api/settings/ai');state.sharedAi=r.ok?await r.json():null;}catch(e){state.sharedAi=null;}}
  function aiOrder() {const shared=state.sharedAi?.values?.order||Object.keys(state.providers),selected=state.aiTools?.selected||[];return [...selected,...shared.filter(p=>!selected.includes(p))];}
  function aiStatus(provider) {const tool=state.sharedAi?.values?.tools?.[provider],info=state.providers[provider];if(!tool?.enabled)return 'AI 도구 메뉴에서 꺼짐';if(info.kind==='api'){const key=tool.credential_ref;return !key?'인증 없음':state.sharedAi.keys?.[key]?'API 키 저장됨':'API 키 필요';}const auth=state.sharedAi.auth?.[provider];return !auth?'로그인 상태 미확인':auth.authenticated?'로그인됨':'로그인 필요';}
  function renderSharedAi() {
    const shared=!!state.aiTools;$('shared-ai').hidden=!shared;$('legacy-ai').hidden=shared;$('legacy-ai-banner').hidden=shared;$('legacy-ai-info').hidden=shared;$('check-auth-label').hidden=shared;if(!shared)return;
    const list=$('shared-ai-list'),order=aiOrder();list.replaceChildren();
    order.forEach((provider,index)=>{const li=make('li'),tool=state.sharedAi?.values?.tools?.[provider],label=make('label',undefined,'check'),box=make('input');li.dataset.provider=provider;box.type='checkbox';box.checked=state.aiTools.selected.includes(provider);box.disabled=!tool?.enabled&&!box.checked;box.addEventListener('change',()=>{state.aiTools.selected=[...list.querySelectorAll('li')].filter(r=>r.querySelector('input').checked).map(r=>r.dataset.provider);state.dirty=true;renderSharedAi();});
      label.append(box,document.createTextNode(` ${state.providers[provider].label} · ${aiStatus(provider)}`));const actions=make('span',undefined,'role-actions');
      for(const [text,delta] of [['위로',-1],['아래로',1]]){const b=make('button',text,'secondary');b.type='button';b.disabled=!box.checked||index+delta<0||index+delta>=order.length||!state.aiTools.selected.includes(order[index+delta]);b.setAttribute('aria-label',`${state.providers[provider].label} ${text}`);b.addEventListener('click',()=>{const s=[...state.aiTools.selected],i=s.indexOf(provider);[s[i],s[i+delta]]=[s[i+delta],s[i]];state.aiTools.selected=s;state.dirty=true;renderSharedAi();});actions.append(b);}
      actions.hidden=!box.checked;li.append(label,actions);list.append(li);});
    const empty=$('shared-ai-empty');empty.hidden=!!(state.sharedAi?.revision&&state.aiTools.selected.length);empty.textContent=!state.sharedAi?.revision?'공통 AI 도구가 아직 없습니다. AI 도구 메뉴에서 먼저 설정해주세요.':'사용할 AI 도구를 하나 이상 선택해주세요. 선택하지 않으면 준비 모드로 저장되어 AI 실행을 할 수 없습니다.';
  }
  function collectAiTools() {return state.aiTools?{selected:[...state.aiTools.selected]}:null;}
  function skillCell(bundle,module,checked) {const td=make('td'),box=make('input');box.type='checkbox';box.checked=checked;box.dataset.bundle=bundle;box.dataset.module=module;box.setAttribute('aria-label',`${bundle} · ${module||'프로젝트 전체'}`);box.addEventListener('change',()=>{state.dirty=true;});td.append(box);return td;}
  function renderSkills() {const s=state.skills,mods=[...new Set(cards('modules').map(m=>m.id).filter(Boolean))],table=make('table'),thead=make('thead'),body=make('tbody'),head=make('tr');head.append(make('th','스킬 묶음'),make('th','프로젝트 전체'),...mods.map(m=>make('th',m)));thead.append(head);
    for(const [name,b] of Object.entries(state.skillCatalog.bundles||{})){const tr=make('tr'),th=make('th');th.scope='row';th.append(make('strong',name),make('small',`${b.skills.length}개 · ${b.description}`));tr.append(th,skillCell(name,'',s.bundles.includes(name)));for(const m of mods)tr.append(skillCell(name,m,(s.repositories[m]?.bundles||[]).includes(name)));body.append(tr);}
    table.append(thead,body);$('skill-bundles').replaceChildren(table);}
  function collectSkills() {const out={bundles:[],repositories:{},roles:state.skills?.roles||{},exclude:state.skills?.exclude||[]},modules=new Set(cards('modules').map(r=>r.id));for(const box of $('skill-bundles').querySelectorAll('input[data-bundle]')){if(!box.checked)continue;if(!box.dataset.module)out.bundles.push(box.dataset.bundle);else if(modules.has(box.dataset.module))(out.repositories[box.dataset.module]??={bundles:[]}).bundles.push(box.dataset.bundle);}return out;}
  const SKILL_STATES={installed:'설치됨',missing:'미설치',outdated:'새 버전 있음',modified:'직접 수정됨',conflict:'같은 이름의 다른 스킬',claude_link_missing:'Claude 연결 없음'};
  // Skills install per git repository; modules sharing one repository share one line.
  const repoLabel=(m,row)=>row?.path?`${row.path.split(/[\\/]/).filter(Boolean).pop()} 저장소`:m;
  function renderSkillStatus(data) {const box=$('skill-status');box.replaceChildren();for(const [m,row] of Object.entries(data.suggestions||{})){box.append(make('p',`${m}: 감지 ${row.markers.join(', ')||'없음'} → 제안 ${row.suggested.join(', ')}`,'hint'));}
    for(const [m,row] of Object.entries(data.status?.repositories||{})){const counts={};Object.values(row.skills).forEach(v=>counts[v]=(counts[v]||0)+1);box.append(make('p',`${repoLabel(m,row)}: `+(Object.entries(counts).map(([k,n])=>`${SKILL_STATES[k]||k} ${n}`).join(' · ')||'선택한 스킬 없음')));}
    for(const [m,row] of Object.entries(data.repositories||{})){const failed=Object.keys(row.failed||{});box.append(make('p',`${repoLabel(m,row)}: 설치 ${row.installed.length} · 유지 ${row.unchanged.length} · 제거 ${row.removed.length}`+(row.conflicts.length?` · 건너뜀(같은 이름) ${row.conflicts.join(', ')}`:'')+(failed.length?` · 실패 ${failed.join(', ')}`:''),failed.length||row.conflicts.length?'warning':''));}}
  function roleInfo(id) {const base=state.roleCatalog.find(r=>r.id===id);if(base)return base;const row=state.roles.custom.find(r=>r.id===id);return {id,label:row?row.name:id,kind:'custom',locked:false};}
  function insertCatalogRole(id) {const order=state.roles.order,ids=catalogIds(),next=ids.slice(ids.indexOf(id)+1).find(r=>order.includes(r));order.splice(next?order.indexOf(next):order.length,0,id);}
  function removeRole(id) {state.roles.order=state.roles.order.filter(r=>r!==id);state.roles.custom=state.roles.custom.filter(r=>r.id!==id);}
  const splitPaths=value=>value.split(',').map(v=>v.trim()).filter(Boolean);
  // A structured role in the roster with no scope cannot apply any proposal.
  function renderStructuredWarning() {const order=state.roles?state.roles.order:[],missing=[];if(order.includes('test-automation')&&!splitPaths($('test-paths').value).length)missing.push('Test Automation → 테스트 경로');if(order.includes('tech-writer')&&!splitPaths($('document-paths').value).length)missing.push('Tech Writer → 문서 경로');const box=$('structured-paths-warning');box.hidden=!missing.length;box.textContent=missing.length?'비어 있으면 해당 역할의 수정안이 적용되지 않습니다: '+missing.join(', '):'';}
  function changed() {state.dirty=true;renderRoles();renderStructuredWarning();}
  ['test-paths','document-paths'].forEach(id=>$(id).addEventListener('input',()=>{state.dirty=true;renderStructuredWarning();}));
  const SESSION_LABELS={analysis:'분석',design:'디자인',develop:'개발',verify:'검증',docs:'문서'};
  // Mirrors project_roles.sessions(): custom code roles develop, others join the previous role;
  // a session that returns after another one gets a numbered id.
  function roleSessions() {const out={},used={};let base='',current='';for(const id of state.roles.order){const custom=state.roles.custom.find(r=>r.id===id),name=roleInfo(id).session||(custom&&custom.mode==='code'?'develop':base);if(name&&name!==base){used[name]=(used[name]||0)+1;current=used[name]===1?name:`${name}-${used[name]}`;}if(name)out[id]=current;base=name;}return out;}
  function sessionLabel(id) {const [name,n]=id.split('-');return (SESSION_LABELS[name]||name)+(n?' '+n:'');}
  function renderSessionControls() {const mode=!!state.roles.session_mode,gates=state.roles.gates||{analysis:true};$('session-mode').checked=mode;$('session-gates').hidden=!mode;for(const k of ['analysis','design']){const box=$('gate-'+k);box.checked=!!gates[k];box.disabled=!mode;}}
  function renderRoles() {
    const list=$('role-order'),order=state.roles.order;if(!order.includes(state.selectedRole))state.selectedRole=order[0]||'';list.replaceChildren();$('role-detail').replaceChildren();
    renderSessionControls();const sessions=state.roles.session_mode?roleSessions():{};
    order.forEach((id,index)=>{
      const info=roleInfo(id),item=make('li'),name=make('button',undefined,'role-name role-select'),actions=make('div',undefined,'role-actions');
      name.type='button';name.setAttribute('aria-pressed',String(id===state.selectedRole));name.addEventListener('click',()=>{state.selectedRole=id;renderRoles();[...$('role-order').querySelectorAll('.role-select')].find(b=>b.getAttribute('aria-pressed')==='true')?.focus({preventScroll:true});});name.append(make('strong',`${index+1}. ${info.label}`),make('span',({base:'기본',optional:'선택',custom:'추가'})[info.kind],'badge'));
      const custom=state.roles.custom.find(r=>r.id===id);
      if(custom)name.append(make('span',({readonly:'읽기 전용',documents:'문서 작성',code:'코드 수정'})[custom.mode],'hint'));
      if(sessions[id])name.append(make('span','세션 · '+sessionLabel(sessions[id]),'hint'));
      const move=(label,delta)=>{const b=make('button',label,'secondary');b.type='button';b.setAttribute('aria-label',`${info.label} ${delta<0?'위로':'아래로'} 이동`);b.disabled=index+delta<0||index+delta>=order.length;b.addEventListener('click',()=>{order.splice(index,1);order.splice(index+delta,0,id);changed();list.children[index+delta]?.querySelector(`[aria-label="${b.getAttribute('aria-label')}"]`)?.focus();});return b;};
      actions.append(move('↑',-1),move('↓',1));
      if(!info.locked){const del=make('button','삭제','danger');del.type='button';del.setAttribute('aria-label',`${info.label} 삭제`);del.addEventListener('click',async()=>{if(custom&&!(await ask(`'${info.label}' 역할을 삭제할까요?`,'역할과 지시문이 함께 삭제됩니다. 설정을 저장하기 전까지는 되돌릴 수 있습니다.','역할 삭제')))return;removeRole(id);changed();});actions.append(del);}
      item.append(name,actions);
      if(custom){const box=make('details');box.append(make('summary','이름·지시문 수정'));const n=field(box,'역할 이름',custom.name,'text');n.maxLength=60;delete n.dataset.field;n.addEventListener('change',()=>{custom.name=n.value;changed();});const wrap=make('div'),lab=make('label','역할 지시문'),area=make('textarea');area.id='role-instruction-'+id;lab.htmlFor=area.id;area.rows=5;area.maxLength=8000;area.value=custom.instruction;area.addEventListener('change',()=>{custom.instruction=area.value;state.dirty=true;});wrap.append(lab,area);box.append(wrap);item.append(box);}
      if(id===state.selectedRole){const detail=$('role-detail');detail.append(make('h3',info.label),make('p','역할 ID · '+id,'hint'),make('p',info.locked?'기본 역할 · 순서 변경 가능, 삭제 불가':'선택 역할 · 추가·삭제 가능','hint'));if(custom){const box=item.querySelector('details');if(box){box.open=true;detail.append(box);}}else detail.append(make('p','내장 역할 지시는 읽기 전용입니다. 실행 순서는 목록의 이동 버튼으로 변경하세요.','info'));}
      list.append(item);
    });
    const select=$('role-add-select');select.replaceChildren();
    state.roleCatalog.filter(r=>r.kind==='optional'&&!order.includes(r.id)).forEach(r=>{const o=make('option',r.label);o.value=r.id;select.append(o);});
    select.disabled=$('role-add').disabled=!select.options.length;if(!select.options.length){const none=make('option','추가할 선택 역할 없음');none.value='';select.append(none);}
  }
  $('role-domain').addEventListener('change',()=>{state.roles.domain=$('role-domain').value;const has=state.roles.order.includes('fmea-expert');if(state.roles.domain==='fmea'&&!has)insertCatalogRole('fmea-expert');if(state.roles.domain==='generic'&&has)removeRole('fmea-expert');changed();});
  $('modules').addEventListener('input',()=>{if(state.roles)renderSessionControls();});
  $('session-mode').addEventListener('change',()=>{if($('session-mode').checked)state.roles.session_mode=true;else delete state.roles.session_mode;changed();});
  ['analysis','design'].forEach(k=>$('gate-'+k).addEventListener('change',()=>{state.roles.gates={...(state.roles.gates||{analysis:true}),[k]:$('gate-'+k).checked};changed();}));
  $('role-add').addEventListener('click',()=>{const id=$('role-add-select').value;if(!id||state.roles.order.includes(id))return;insertCatalogRole(id);changed();});
  $('custom-role-create').addEventListener('click',()=>{
    const name=$('custom-role-name').value.trim(),mode=$('custom-role-mode').value,instruction=$('custom-role-instruction').value.trim();
    if(!name||!instruction){notice('새 역할의 이름과 지시문을 입력해주세요.',true);(name?$('custom-role-instruction'):$('custom-role-name')).focus();return;}
    const id=`custom-${mode}-${crypto.randomUUID().slice(0,8)}`;state.roles.custom.push({id,name,mode,instruction});state.roles.order.push(id);
    $('custom-role-name').value='';$('custom-role-instruction').value='';changed();notice(`'${name}' 역할을 순서 마지막에 추가했습니다. 위치를 조정한 뒤 설정을 저장해주세요.`);
  });
  function cards(id) {return [...$(id).children].filter(card=>card.dataset.remove!=='true').map(card=>Object.fromEntries([...card.querySelectorAll('[data-field]')].map(input=>[input.dataset.field,input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value])));}
  function collectRunners() {
    return Object.fromEntries([...$('runners').children].map(card=>{
      const row=Object.fromEntries([...card.querySelectorAll('[data-field]')].map(input=>[input.dataset.field,input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value]));
      row.routing={mode:card.querySelector('[data-route-field=mode]').value,max_effort:card.querySelector('[data-route-field=max_effort]').value,profiles:Object.fromEntries([...card.querySelectorAll('[data-tier]')].map(group=>[group.dataset.tier,{model:group.querySelector('[data-route-field=model]').value,supported_efforts:group.querySelector('[data-route-field=supported_efforts]').value.split(',').map(v=>v.trim()).filter(Boolean)}]))};
      return [card.dataset.provider,row];
    }));
  }
  function collect() {
    if(!state.settings||$('root').value!==state.settings.root)throw new Error('변경한 프로젝트 폴더의 설정을 먼저 불러와주세요.');
    return {...state.settings,context_policy:{token_mode:$('context-token').value,ponytail:$('context-ponytail').value},name:$('name').value,project_id:$('project-id').value,create_directories:$('create-directories').checked,
      modules:cards('modules').map(row=>({...row,allowed_paths:row.allowed_paths.split(',').map(v=>v.trim()).filter(Boolean)})),
      ai:{provider:$('provider').value,runners:collectRunners()},roles:state.roles,
      verification:cards('gates').map(row=>({...row,command:parseArgs(row.command)})),environment:cards('env-rows'),
      preview_services:cards('preview-services').map(row=>({id:row.id,module:row.module,argv:parseArgs(row.argv),cwd:row.cwd||'.',port:row.port,readiness_path:row.readiness_path||'/',startup_timeout:row.startup_timeout,env_refs:row.env_refs.split(',').map(v=>v.trim()).filter(Boolean),allowed_origins:row.allowed_origins.split(',').map(v=>v.trim()).filter(Boolean)})),
      usage_policy:collectPolicy(),skills:collectSkills(),ai_tools:collectAiTools(),
      verification_scope:$('verification-scope').value,completion_mode:$('completion-mode').value,test_paths:splitPaths($('test-paths').value),document_paths:splitPaths($('document-paths').value),wiki_dir:$('wiki-dir').value.trim(),limits:Object.fromEntries([...$('limits').querySelectorAll('[data-field]')].map(i=>[i.dataset.field,Number(i.value)])),ports:Object.fromEntries([...$('ports').querySelectorAll('[data-field]')].map(i=>[i.dataset.field,Number(i.value)]))};
  }
  function busy(value) {state.pending=value;workspaceStatus();DevRosterProgress.busyAfter($('notice'),value,'설정 작업 처리 중');document.querySelectorAll('#settings-form input,#settings-form select,#settings-form textarea,#settings-form button').forEach(el=>el.disabled=value||el.dataset.catalogUnavailable==='true'||(el.id==='project-id'&&state.settings?.profile_version>0));}
  async function load(root) {const data=await api('bootstrap'+(root?'?root='+encodeURIComponent(root):''));state.token=data.token;state.presets=data.presets;state.catalog=data.model_catalog||{};state.providers=data.providers;state.roleCatalog=data.role_catalog||[];state.skillCatalog=data.skill_catalog||{bundles:{}};await loadSharedAi();render(data.settings);refreshCliAuth();notice('설정을 확인하고 저장해주세요.');}
  $('settings-form').addEventListener('input',e=>{if(!['routing-task','routing-phase'].includes(e.target.id))state.dirty=true;});
  $('settings-form').addEventListener('submit',async e=>{e.preventDefault();try{const settings=collect();busy(true);notice('설정을 저장하고 있습니다. 로그인 확인을 선택했다면 잠시 기다려주세요.');const result=await api('save',{root:sourceRoot(),settings,check_auth:authCheck()});if(result.shared_ai)await loadSharedAi();render(result.settings);await refreshWorkspaces(result.settings.root);notice(result.warnings.length?result.warnings.join(' '):result.shared_ai?'설정을 저장했습니다. 이 프로젝트의 AI 도구가 공통 AI 도구가 되었습니다. 로그인은 AI 도구 메뉴에서 진행하고, 환경 진단으로 준비 상태를 확인해주세요.':'설정을 저장했습니다. 환경 진단으로 준비 상태를 확인해주세요.',!!result.warnings.length);}catch(err){notice(errText(err),true);}finally{busy(false);}});
  $('load-root').addEventListener('click',async()=>{if(state.dirty&&!(await ask('저장하지 않은 설정이 있습니다','선택한 폴더의 설정을 불러오면 지금 수정한 내용이 사라집니다.','변경 버리고 불러오기',{cancel:'계속 편집'})))return;try{busy(true);await load($('root').value);}catch(e){notice(errText(e),true);}finally{busy(false);}});
  $('provider').addEventListener('change',()=>{const card=[...$('runners').children].find(c=>c.dataset.provider===$('provider').value);if(card){card.open=true;card.querySelector('[data-field=enabled]').checked=true;}state.dirty=true;});
  $('add-module').addEventListener('click',()=>{const ids=cards('modules').map(m=>m.id),key=!ids.includes('frontend')?'frontend':!ids.includes('backend')?'backend':'module'+(ids.length+1),preset=key==='frontend'?'react':key==='backend'?'spring-boot':'generic';moduleCard({id:key,path:key,preset,allowed_paths:state.presets[preset].paths,base_ref:'HEAD'});state.dirty=true;});
  $('add-gate').addEventListener('click',()=>{gateCard({name:'check_'+($('gates').children.length+1),module:cards('modules')[0]?.id,command:['git','diff','--check']});state.dirty=true;});
  $('preset-gates').addEventListener('click',async()=>{if(!(await ask('검증 명령을 기본 명령으로 바꿀까요?','지금 입력한 검증 명령이 모듈 기술의 기본 명령으로 대체됩니다. 설정을 저장하기 전까지는 되돌릴 수 있습니다.','기본 명령으로 바꾸기')))return;
    // The server leaves out `npm run` scripts the module's package.json does not have.
    try{busy(true);const data=await api('preset-gates',{root:sourceRoot(),modules:cards('modules').map(({id,preset,path})=>({id,preset,path}))});
      $('gates').replaceChildren();data.gates.forEach(g=>gateCard({...g,required:true,timeout_seconds:900}));state.dirty=true;
      notice(data.skipped.length?'기본 명령을 가져왔습니다. 제외한 명령: '+data.skipped.map(s=>s.name+' ('+s.reason+')').join(', '):'기본 명령을 가져왔습니다. 저장 전에 확인해주세요.');}
    catch(e){notice(errText(e),true);}finally{busy(false);}});
  $('add-env').addEventListener('click',()=>{envRow();state.dirty=true;});
  $('add-price').addEventListener('click',()=>{priceCard({provider:$('provider').value});state.dirty=true;});
  $('add-preview').addEventListener('click',()=>{previewCard({module:cards('modules')[0]?.id});state.dirty=true;});
  $('diagnose').addEventListener('click',async()=>{if(state.dirty){notice('수정 내용을 저장한 뒤 진단해주세요.',true);return;}try{busy(true);notice('환경을 확인하고 있습니다.');const data=await api('check',{root:sourceRoot(),check_auth:authCheck()});$('checks').replaceChildren();data.checks.forEach(row=>{const el=make('div',undefined,'result'),badge=make('span',({pass:'준비됨',warn:'확인 필요',fail:'조치 필요'})[row.status]||row.status,'badge '+row.status),body=make('div');body.append(make('strong',row.label||row.name),make('p',row.summary||row.message));if(row.status!=='pass'&&row.action)body.append(make('p',row.action));const detail=make('details');detail.append(make('summary','자세히 보기'),make('p',row.message));if(row.fix&&row.status!=='pass')detail.append(make('p',row.fix));body.append(detail);el.append(badge,body);$('checks').append(el);});notice(data.ready?'검사를 마쳤습니다. 확인 필요 항목도 읽어주세요.':'진단 결과에 표시된 조치부터 진행해주세요.',!data.ready);}catch(e){notice(errText(e),true);}finally{busy(false);}});
  $('skills-suggest').addEventListener('click',async()=>{try{busy(true);const data=await api('skills/status',{root:sourceRoot(),modules:cards('modules').map(({id,path})=>({id,path}))});let changed=0;for(const [m,row] of Object.entries(data.suggestions)){for(const b of row.suggested){const box=$('skill-bundles').querySelector(`input[data-bundle="${CSS.escape(b)}"][data-module="${CSS.escape(b==='core'?'':m)}"]`);if(box&&!box.checked){box.checked=true;changed++;}}}if(changed)state.dirty=true;renderSkillStatus(data);notice(changed?'감지한 스택으로 스킬 묶음을 제안했습니다. 확인한 뒤 설정을 저장해주세요.':'현재 선택이 감지한 스택과 같습니다.');}catch(e){notice(errText(e),true);}finally{busy(false);}});
  $('skills-sync').addEventListener('click',async()=>{if(state.dirty){notice('스킬 선택을 바꿨다면 먼저 설정을 저장해주세요.',true);return;}try{busy(true);notice('고정한 버전의 스킬을 받아 저장소에 설치하고 있습니다.');const data=await api('skills/sync',{root:sourceRoot()});renderSkillStatus(data);const failed=Object.values(data.repositories).some(r=>Object.keys(r.failed).length||r.conflicts.length);notice(failed?'일부 스킬을 설치하지 못했습니다. 아래 결과를 확인해주세요.':'스킬을 저장소에 설치했습니다. Claude Code·Codex를 다시 열면 보입니다.',failed);}catch(e){notice(errText(e),true);}finally{busy(false);}});
  // Project AI rules (.ai/rules): this project only. DevRoster's own rules stay read only.
  function renderRules(data){
    $('rules-builtin').textContent='DevRoster 기본 규칙(수정 불가): '+(data.builtin||[]).join(', ');
    const list=$('rules-list');list.replaceChildren();
    if(data.error)list.append(make('li',data.error,'field-error'));
    else if(!data.items.length)list.append(make('li','아직 프로젝트 규칙이 없습니다. Markdown 파일을 첨부하거나 .ai/rules에 직접 넣으세요.','hint'));
    for(const item of data.items){const li=make('li');const name=item.path.split('/').pop();li.append(make('code',item.path),make('span',Math.max(1,Math.round(item.bytes/1024))+'KB','hint'));const del=make('button','삭제','ghost sm');del.type='button';del.setAttribute('aria-label',name+' 규칙 삭제');del.addEventListener('click',()=>removeRule(name));li.append(del);list.append(li);}
    const skills=$('ai-skills-list');skills.replaceChildren();
    if(!(data.skills||[]).length)skills.append(make('li','프로젝트 스킬이 없습니다.','hint'));
    for(const skill of data.skills||[]){const li=make('li');li.append(make('code',skill.name),make('span',skill.description+(skill.roles.length?' · 역할: '+skill.roles.join(', '):' · 모든 역할'),'hint'));skills.append(li);}
    const labels={current:'최신',update:'다시 생성 필요',create:'새로 만들 예정',absent:'만들 내용 없음',conflict:'충돌',remove:'정리 예정',created:'생성함',updated:'갱신함',removed:'정리함'};
    $('rules-files').textContent=[...(data.warnings||[]),...(data.files||[]).map(f=>`${f.file}: ${labels[f.state]||f.state}${f.warning?' ('+f.warning+')':''}${f.reason?' ('+f.reason+')':''}`)].join(' · ');
    $('rules-preview').textContent=data.preview||'전달할 프로젝트 규칙이 없습니다.';
  }
  // Project hooks (.ai/hooks): reviewed and approved by this user before anything runs.
  const hookLabels={after_edit:'코드 수정 직후',before_verify:'검증 전',before_publish:'게시 전',after_verify:'검증 후',after_publish:'게시 후',on_problem:'문제 발생 시',phase_start:'단계 시작',phase_end:'단계 끝',studio_confirmed:'Studio 확정·인계'};
  let hooksData=null;
  function renderHooks(data){
    hooksData=data;const list=$('ai-hooks-list');list.replaceChildren();
    const events=Object.entries(data.events||{});
    if(data.error)list.append(make('li',data.error,'field-error'));
    else if(!events.length)list.append(make('li','프로젝트 훅이 없습니다.','hint'));
    for(const [event,hooks] of events)for(const hook of hooks){const li=make('li');li.append(make('span',hookLabels[event]||event,'badge '+(data.modes[event]==='block'?'warning':'')),make('code',hook.run.join(' ')),make('span',data.modes[event]==='block'?'실패 시 막음':'기록만','hint'));list.append(li);}
    $('hooks-approve').hidden=!events.length||data.approved;
    $('hooks-status').textContent=!events.length?'':data.approved?'승인된 훅입니다. 새로 승인하는 작업부터 실행됩니다.':'승인 전입니다. 승인하기 전에는 작업을 승인할 수 없습니다.';
    const last=(data.recent||[])[0];if(last)$('hooks-status').textContent+=` 최근 Studio 훅: ${last.results.every(r=>r.ok)?'성공':'실패'} (${new Date(last.at*1000).toLocaleString('ko-KR')})`;
  }
  async function loadHooks(){try{renderHooks(await api('hooks',{root:sourceRoot()}));}catch(e){$('ai-hooks-list').replaceChildren();$('hooks-approve').hidden=true;$('hooks-status').textContent='';}}
  $('hooks-approve').addEventListener('click',async()=>{if(!hooksData)return;const lines=Object.entries(hooksData.events).flatMap(([event,hooks])=>hooks.map(h=>`${hookLabels[event]||event}: ${h.run.join(' ')}`));
    if(!await ask('이 명령들을 실행해도 될까요?','저장소의 .ai/hooks에 있는 명령입니다. 승인하면 정해진 순간에 이 컴퓨터에서 실행됩니다.\n\n'+lines.join('\n'),'승인'))return;
    try{busy(true);renderHooks(await api('hooks/approve',{root:sourceRoot(),digest:hooksData.digest}));notice('프로젝트 훅을 승인했습니다. 새로 승인하는 작업부터 실행됩니다.');}catch(e){notice(errText(e),true);}finally{busy(false);}});
  async function loadRules(){loadHooks();try{renderRules(await api('rules',{root:sourceRoot()}));}catch(e){$('rules-builtin').textContent='';$('rules-list').replaceChildren(make('li','프로젝트 설정을 저장하면 규칙을 관리할 수 있습니다.','hint'));$('rules-files').textContent='';$('rules-preview').textContent='';}}
  async function removeRule(name){if(!await ask(`${name} 규칙을 삭제할까요?`,'이 프로젝트의 .ai/rules에서 파일을 지웁니다. 이미 승인된 작업에는 영향이 없습니다.','삭제',{danger:true}))return;try{busy(true);renderRules(await api('rules/remove',{root:sourceRoot(),name}));notice(`${name} 규칙을 삭제했습니다.`);}catch(e){notice(errText(e),true);}finally{busy(false);}}
  $('rules-upload').addEventListener('change',async e=>{const file=e.target.files[0];e.target.value='';if(!file)return;if(file.size>65536){notice('규칙 파일은 64KB 이하만 첨부할 수 있습니다.',true);return;}
    try{busy(true);const content=await file.text();let data;
      try{data=await api('rules/attach',{root:sourceRoot(),name:file.name,content});}
      catch(err){if(!/같은 이름/.test(err.message))throw err;if(!await ask(`${file.name} 규칙을 바꿀까요?`,'같은 이름의 규칙이 이미 있습니다. 첨부한 파일 내용으로 바꿉니다.','바꾸기'))return;data=await api('rules/attach',{root:sourceRoot(),name:file.name,content,replace:true});}
      renderRules(data);notice(`${file.name} 규칙을 이 프로젝트에 추가했습니다. 다음에 승인하는 작업부터 적용됩니다.`);}
    catch(err){notice(errText(err),true);}finally{busy(false);}});
  $('rules-sync').addEventListener('click',async()=>{try{busy(true);renderRules(await api('rules/sync',{root:sourceRoot()}));notice('CLAUDE.md·AGENTS.md·GEMINI.md와 스킬 연결을 .ai 내용으로 맞췄습니다. 표시 영역 밖 내용은 그대로 둡니다.');}catch(e){notice(errText(e),true);}finally{busy(false);}});
  $('shared-ai-import').addEventListener('click',async()=>{if(state.dirty){notice('변경 내용을 먼저 저장한 뒤 전환해주세요.',true);return;}try{busy(true);const call=async apply=>{const r=await fetch('/api/settings/ai/import',{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':state.token},body:JSON.stringify({root:sourceRoot(),apply})});const d=await r.json();if(!r.ok)throw new Error(d.error||'전환하지 못했습니다.');return d;};const plan=await call(false);const names=plan.selected.map(p=>state.providers[p]?.label||p).join(', ')||'없음';const diff=plan.differences.map(d=>`${state.providers[d.provider]?.label||d.provider} ${d.field}: 프로젝트 ${JSON.stringify(d.project)} → 공통 ${JSON.stringify(d.shared)}`);const message=(plan.creates_shared?'이 프로젝트의 AI 설정으로 공통 AI 도구를 처음 만듭니다.\n':'이미 있는 공통 AI 도구를 사용합니다.\n')+`사용할 도구: ${names}`+(diff.length?`\n\n공통 설정과 다른 값(전환 후 공통 값을 씁니다):\n${diff.join('\n')}`:'\n실행에 쓰는 도구·모델·강도는 그대로입니다.');if(!(await ask('공통 AI 도구로 전환할까요?',message,'공통 AI 도구로 전환',{danger:false,iconName:'sliders'}))){notice('전환을 취소했습니다.');return;}await call(true);await load(sourceRoot());notice('공통 AI 도구로 전환했습니다. 로그인·모델은 AI 도구 메뉴에서 관리합니다.');}catch(e){notice(errText(e),true);}finally{busy(false);}});
  $('preview-routing').addEventListener('click',async()=>{try{const settings=collect();busy(true);const data=await api('preview',{root:sourceRoot(),settings,task:{title:$('routing-task').value},phase_id:$('routing-phase').value});$('routing-result').replaceChildren();data.selections.forEach(row=>$('routing-result').append(make('p',`${state.providers[row.provider]?.label||row.provider} · ${row.model||'CLI 기본 모델'} · ${row.effort||'CLI 기본 강도'} · ${row.reason}`)));notice('미리보기 완료: 실제 AI 호출 없이 선택 정책만 확인했습니다.');}catch(e){notice(errText(e),true);}finally{busy(false);}});
  $('export').addEventListener('click',()=>{try{const value=collect();value.environment=value.environment.map(row=>({...row,value:'••••••••'}));const blob=new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}),a=make('a');a.href=URL.createObjectURL(blob);a.download='project-settings.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);notice('설정 파일을 내보냈습니다. 환경변수 값은 포함하지 않습니다. 다른 프로젝트에 적용할 때 revision·profile_version·경로를 다시 확인해주세요.');}catch(e){notice(errText(e),true);}});
  let folderPath='';async function browse(path){try{const data=await api('folders?path='+encodeURIComponent(path));folderPath=data.path;$('folder-path').value=data.path;$('folders').replaceChildren();const up=make('button','↑ 상위 폴더');up.type='button';up.addEventListener('click',()=>browse(data.parent));$('folders').append(up);data.directories.forEach(dir=>{const b=make('button','📁 '+dir.name);b.type='button';b.addEventListener('click',()=>browse(dir.path));$('folders').append(b);});$('folder-info').textContent=data.truncated?'처음 200개 폴더를 표시합니다. 경로를 직접 입력할 수도 있습니다.':'현재 위치의 폴더만 표시합니다.';}catch(e){$('folder-info').textContent=errText(e);folderPath='';}}
  function openFolders(target){state.folderTarget=target;$('folder-dialog').showModal();const path=target.value||$('root').value;browse(path.startsWith('/')||/^[A-Za-z]:/.test(path)?path:$('root').value+'/'+path);}
  $('browse-root').addEventListener('click',()=>openFolders($('root')));$('folder-go').addEventListener('click',()=>browse($('folder-path').value));$('folder-cancel').addEventListener('click',()=>$('folder-dialog').close());$('folder-select').addEventListener('click',()=>{if(folderPath){state.folderTarget.value=folderPath;state.dirty=true;$('folder-dialog').close();}});
  const integrations=[['앱 서버·DB','DATABASE_URL / DB_PASSWORD / VITE_API_BASE_URL','애플리케이션의 환경변수 이름에 맞춰 입력합니다.'],['Redis·스토리지','REDIS_URL / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY','프로젝트가 사용하는 서비스만 추가합니다.'],['AI API 인증 (선택)','OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY','API 카드의 인증 환경변수 이름과 같은 키를 등록합니다. 값은 비공개로 저장합니다.'],['Stitch·Figma·GitHub','STITCH_API_KEY / FIGMA_ACCESS_TOKEN / GITHUB_PAT_TOKEN','CLI의 MCP 연결 설정과 작업별 권한 승인이 별도로 필요합니다.'],['알림·Discord','DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID','기존 알림 설정 또는 앱 실행 환경에도 연결해야 합니다.'],['추가 도구·Docker','프로젝트가 요구하는 환경변수','npm·Java·Python·Docker 설치는 터미널에서 준비합니다.']];
  integrations.forEach(([title,keys,description])=>{const card=make('article');card.append(make('strong',title),make('pre',keys),make('p',description));$('integrations').append(card);});
  window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});
  let workspaceRows=[],activeWorkspace='',workspaceMode='new',workspaceOpener=null,workspaceBusy=false;
  function workspaceStatus(){const row=workspaceRows.find(p=>p.root===$('workspace-select').value);$('workspace-select').disabled=workspaceBusy||state.pending;$('workspace-open').disabled=workspaceBusy||state.pending||!row?.available||row.current;$('workspace-status').textContent=!row?'등록한 프로젝트가 없습니다.':row.error||((row.current?'현재 열린 프로젝트입니다. ':'')+(row.configured?'설정이 저장된 프로젝트입니다.':'준비 전 · 열면 설정부터 진행합니다.'));}
  async function refreshWorkspaces(selected){const data=await api('projects');if(!state.token)state.token=data.token;workspaceRows=data.projects;activeWorkspace=data.active_root;$('workspace-active').textContent='현재 작업 폴더: '+activeWorkspace;$('workspace-select').replaceChildren();for(const row of workspaceRows){const option=make('option',row.name+' · '+row.root+(row.current?' · 현재 작업':''));option.value=row.root;option.disabled=!row.available;$('workspace-select').append(option);}$('workspace-select').value=selected||state.settings?.root||activeWorkspace;if(!$('workspace-select').value)$('workspace-select').value=activeWorkspace;workspaceStatus();}
  async function discardWorkspaceDraft(){if(document.activeElement?.closest('#settings-form'))document.activeElement.blur();return !state.dirty||!!(await ask('저장하지 않은 설정이 있습니다','다른 프로젝트로 전환하면 지금 수정한 설정이 사라집니다.','변경 버리고 전환',{cancel:'계속 편집'}));}
  $('workspace-select').addEventListener('change',async()=>{if(workspaceBusy||state.pending)return;const selected=$('workspace-select').value;if(!(await discardWorkspaceDraft())){$('workspace-select').value=state.settings?.root||activeWorkspace;workspaceStatus();return;}try{workspaceBusy=true;workspaceStatus();const row=workspaceRows.find(p=>p.root===selected);await load(row?.configuration_root||selected);}catch(e){notice(errText(e),true);}finally{workspaceBusy=false;workspaceStatus();}});
  $('workspace-open').addEventListener('click',async()=>{if(workspaceBusy||state.pending||!(await discardWorkspaceDraft()))return;const root=$('workspace-select').value;try{workspaceBusy=true;workspaceStatus();const data=await api('project-open',{root});const destination=new URL(data.url);if(destination.protocol!=='http:'||destination.hostname!=='127.0.0.1'||!['/setup','/agent'].includes(destination.pathname))throw new Error('작업 공간 주소를 확인하지 못했습니다.');state.dirty=false;location.assign(destination.href);}catch(e){notice(errText(e),true);}finally{workspaceBusy=false;workspaceStatus();}});
  function showWorkspaceDialog(mode,opener){if(state.pending||workspaceBusy)return;workspaceMode=mode;workspaceOpener=opener;$('workspace-dialog-title').textContent=mode==='new'?'새 프로젝트':'기존 폴더 등록';$('workspace-name').hidden=mode!=='new';document.querySelector('label[for=workspace-name]').hidden=mode!=='new';$('workspace-name').required=mode==='new';$('workspace-name').value='';$('workspace-path').value='';$('workspace-dialog-error').textContent='';$('workspace-dialog-submit').textContent=mode==='new'?'설정 준비':'폴더 등록';$('workspace-dialog-help').textContent=mode==='new'?'독립 최상위 폴더를 지정하세요. 설정을 저장할 때 폴더가 생성되고 프로젝트 목록에 등록됩니다.':'프로젝트의 기존 최상위 폴더를 등록하세요. 폴더나 데이터를 이동하지 않습니다.';$('workspace-dialog').showModal();}
  $('workspace-new').addEventListener('click',()=>showWorkspaceDialog('new',$('workspace-new')));$('workspace-register').addEventListener('click',()=>showWorkspaceDialog('register',$('workspace-register')));$('workspace-dialog-cancel').addEventListener('click',()=>$('workspace-dialog').close());$('workspace-dialog').addEventListener('close',()=>workspaceOpener?.focus());
  $('workspace-form').addEventListener('submit',async event=>{event.preventDefault();if(workspaceBusy||!(await discardWorkspaceDraft()))return;const path=$('workspace-path').value.trim();if(!path.startsWith('/')&&!/^[A-Za-z]:[\\/]/.test(path)){$('workspace-dialog-error').textContent='최상위 폴더의 절대 경로를 입력해주세요.';return;}try{workspaceBusy=true;$('workspace-dialog-submit').disabled=true;if(workspaceMode==='register'){await api('project-register',{root:path});await load(path);await refreshWorkspaces(state.settings.root);}else{await load(path);if(state.settings.revision>0)throw new Error('이미 설정된 프로젝트입니다. 기존 폴더 등록을 사용해주세요.');$('name').value=$('workspace-name').value.trim();$('create-directories').checked=true;state.dirty=true;activateStep('project');syncContext();notice('새 프로젝트 설정을 준비했습니다. 설정을 저장하면 폴더와 프로젝트 목록이 생성됩니다.');}$('workspace-dialog').close();}catch(e){$('workspace-dialog-error').textContent=errText(e);}finally{workspaceBusy=false;$('workspace-dialog-submit').disabled=false;workspaceStatus();}});
  load().then(()=>refreshWorkspaces()).catch(e=>notice(errText(e),true));

})();
