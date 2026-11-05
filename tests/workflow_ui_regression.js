'use strict';
const fs=require('fs'), vm=require('vm'), assert=require('assert');
class Element {
  constructor(){this.value='';this.disabled=false;this.children=[];this.listeners={};this.files=[];this.dataset={};this.attributes={};this.textContent='';}
  append(...children){for(const child of children)if(child&&typeof child==='object')child.parent=this;this.children.push(...children);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);this.parent=null;}
  replaceChildren(...children){for(const child of children)if(child&&typeof child==='object')child.parent=this;this.children=children;}
  setAttribute(key,value){this.attributes[key]=value;}
  removeAttribute(key){delete this.attributes[key];}
  focus(){focused=this;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  click(){if(this.listeners.click)return this.listeners.click({preventDefault(){}});if(this.onclick)return this.onclick();}
  // Only the [data-key] lookup the node field editor uses.
  querySelectorAll(selector){const found=[];const walk=el=>{for(const c of el.children||[]){if(c&&typeof c==='object'){if(selector==='[data-key]'&&c.dataset&&c.dataset.key!==undefined)found.push(c);walk(c);}}};walk(this);return found;}
}
const elements=new Map();
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let calls=0,focused=null;
const key=(name,shiftKey=false)=>({key:name,shiftKey,preventDefault(){}});
const nodeEl=id=>get('graph').children.find(c=>c.dataset.node===id);
const run=code=>vm.runInContext(code,context);
const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),createElementNS:()=>new Element()},window:{addEventListener(){}},navigator:{onLine:true},structuredClone,localStorage:{setItem(){},getItem(){return null;},removeItem(){}},URL,Blob,fetch:async()=>{calls++;return {ok:true,json:async()=>({token:'fixture',projects:[{id:'P',name:'Project',version:0}],presets:[],workflows:[]})};}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
(async()=>{
  await new Promise(resolve=>setTimeout(resolve,10));
  get('project').value='P';
  vm.runInContext("projects=[{id:'P',version:0}];open({schema_version:1,id:'custom',name:'Saved',nodes:[{id:'developer',kind:'role',role_id:'developer'}],edges:[]},1);",context);
  assert.equal(get('apply').disabled,false);
  get('workflow-name').value='Unsaved change';
  get('workflow-name').listeners.input();
  assert.equal(get('apply').disabled,true);
  const prior=calls;
  await assert.rejects(vm.runInContext('apply()',context),/먼저 저장/);
  assert.equal(calls,prior,'unsaved Apply never submits the older stored version');
  vm.runInContext('draftChanged=false;render()',context);
  assert.equal(get('apply').disabled,false);
  console.log('Workflow draft Apply interaction passed');

  // Automatic layout puts each dependency level on its own row.
  const auto=run("autoLayout({nodes:[{id:'a'},{id:'b'},{id:'c'},{id:'d'}],edges:[{from:'a',to:'b'},{from:'a',to:'c'},{from:'b',to:'d'},{from:'c',to:'d'}]})");
  assert.deepEqual([auto.a.y,auto.b.y,auto.c.y,auto.d.y],[10,100,100,190]);
  assert.notEqual(auto.b.x,auto.c.x);

  // Keyboard arrows move the focused node, pin every node, and keep focus.
  run("open({schema_version:1,id:'custom',name:'Saved',nodes:[{id:'plan',kind:'role',role_id:'planner'},{id:'dev',kind:'role',role_id:'developer'}],edges:[{from:'plan',to:'dev'}]},2);");
  nodeEl('dev').listeners.keydown(key('ArrowRight'));
  assert.deepEqual(run('documentDraft.layout'),{plan:{x:10,y:10},dev:{x:225,y:10}});
  assert.equal(run('draftChanged'),true);
  assert.equal(focused,nodeEl('dev'),'focus returns to the moved node after redraw');
  nodeEl('dev').listeners.keydown(key('ArrowUp',true));
  assert.equal(run('documentDraft.layout.dev.y'),0,'coordinates clamp at 0');

  // Pointer drag commits the delta; a tiny move is a click (select) only.
  const plan=nodeEl('plan');
  plan.listeners.pointerdown({clientX:0,clientY:0,pointerId:1});
  plan.listeners.pointermove({clientX:40,clientY:30});
  plan.listeners.pointerup({clientX:40,clientY:30});
  assert.deepEqual(run('documentDraft.layout.plan'),{x:50,y:40});
  const before=JSON.stringify(run('documentDraft.layout'));
  nodeEl('dev').listeners.pointerdown({clientX:5,clientY:5,pointerId:1});
  nodeEl('dev').listeners.pointerup({clientX:6,clientY:6});
  assert.equal(JSON.stringify(run('documentDraft.layout')),before);
  assert.equal(run('selected'),'dev');

  // Renaming a node through JSON keeps its coordinates under the new ID.
  get('node-config').value=JSON.stringify({id:'build',kind:'role',role_id:'developer'});
  get('update-node').listeners.click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.ok(run('documentDraft.layout.build')&&!run('documentDraft.layout.dev'));
  assert.deepEqual(run('documentDraft.edges'),[{from:'plan',to:'build'}]);

  // Structured server locations select the node and focus the offending input.
  const draftBefore=JSON.stringify(run('documentDraft'));
  run("focusError({message:'Unknown module: api',location:{node:'build',field:'module'}})");
  assert.equal(run('selected'),'build');
  assert.match(nodeEl('build').attributes.class,/invalid/);
  const module=run("fieldInputs.module.input");
  assert.equal(focused,module,'the module input receives focus');
  assert.equal(module.attributes['aria-invalid'],'true');
  const message=run("fieldInputs.module.wrap").children.find(c=>c.className==='field-error');
  assert.equal(message.attributes.role,'alert');
  assert.equal(module.attributes['aria-describedby'],message.id);
  assert.match(message.textContent,/Unknown module: api/);
  assert.equal(JSON.stringify(run('documentDraft')),draftBefore,'errors never change the draft');
  run("focusError({message:'Invalid role setting: provider',location:{node:'plan',field:'provider'}})");
  assert.ok(!run("fieldInputs.module.wrap").children.includes(message),'the previous error is removed, not hidden');
  assert.equal(run("$('advanced-json').open"),true,'fields without a form input open the JSON editor');
  assert.equal(focused,get('node-config'));
  const firstJson=get('node-config-wrap').children.filter(c=>c.className==='field-error');
  run("focusError({message:'Invalid role setting: role_file',location:{node:'plan',field:'role_file'}})");
  const jsonErrors=get('node-config-wrap').children.filter(c=>c.className==='field-error');
  assert.equal(jsonErrors.length,1,'only the current JSON error remains');
  assert.notEqual(jsonErrors[0].id,firstJson[0].id,'error ids are never reused');
  assert.equal(get('node-config').attributes['aria-describedby'],jsonErrors[0].id);
  assert.match(jsonErrors[0].textContent,/role_file/);
  run("focusError({message:'Duplicate edge.',location:{edge:0}})");
  assert.equal(focused,get('edges').children[0],'edge errors focus the connection row, not its delete button');
  // Layout errors point at the graph node or the automatic layout action, never the node JSON.
  const layoutDraft=JSON.stringify(run('documentDraft'));
  run("focusError({message:'Invalid layout coordinates: plan',location:{node:'plan',field:'layout'}})");
  assert.equal(focused,nodeEl('plan'),'the graph node takes focus so arrows can move it');
  assert.equal(nodeEl('plan').attributes['aria-invalid'],'true');
  const graphAlert=get('graph-errors').children.find(c=>c.className==='field-error');
  assert.equal(nodeEl('plan').attributes['aria-describedby'],graphAlert.id);
  assert.match(graphAlert.textContent,/자동 배치/);
  run("focusError({message:'Layout references unknown node: ghost',location:{field:'layout'}})");
  assert.equal(focused,get('auto-layout'),'unknown layout entries are fixed by automatic layout');
  assert.equal(get('graph-errors').children.filter(c=>c.className==='field-error').length,1);
  assert.equal(JSON.stringify(run('documentDraft')),layoutDraft,'layout errors never change the draft');
  run("focusError({message:'Workflow name must be 1–200 characters.'})");
  assert.equal(run('selected'),'plan','errors without a location leave selection alone');

  // Execution states colour the graph nodes.
  run("execStates={plan:'passed',build:'running'};render()");
  assert.match(nodeEl('build').attributes.class,/state-running/);
  assert.match(nodeEl('build').attributes['aria-label'],/실행 중/);

  // Version diff explains layout-only changes.
  run("renderDiff({base:1,version:2,diff:{nodes:{added:[],removed:[],changed:[]},edges:{added:[],removed:[]},config:[],layout:{moved:['plan'],added:[],removed:[]},semantic_changed:false}})");
  assert.match(get('diff').children[0].textContent,/배치만/);
  run("diffChoices([{version:2},{version:1}])");
  assert.equal(get('diff-base').value,'1');
  assert.equal(get('diff-target').value,'2');
  assert.equal(get('compare').disabled,false);
  assert.equal(get('compare-row').hidden,false);
  run("diffChoices([{version:1}])");
  assert.equal(get('compare-row').hidden,true,'one version has nothing to compare');
  // Session field (ADR 0002): suggestions, full effort range, label.
  run("documentDraft.nodes.find(n=>n.id==='build').session='develop';select('build')");
  const sessionInput=run("fieldInputs.session.input");
  assert.equal(sessionInput.value,'develop');
  assert.equal(sessionInput.attributes.list,'session-options');
  const suggestions=run("fieldInputs.session.wrap").children.find(c=>c.id==='session-options');
  assert.ok(suggestions&&['analysis','develop','docs'].every(v=>suggestions.children.some(o=>o.value===v)),'session suggestions include default sessions');
  assert.ok(['xhigh','max','ultra'].every(v=>run("fieldInputs.effort.input").children.some(o=>o.value===v)),'effort offers every level the graph accepts');
  assert.match(run("nodeLabel(documentDraft.nodes.find(n=>n.id==='build'))"),/세션 develop/);
  // Saving a role step validates at once and points at the session field.
  run("request=async(action)=>{if(action==='validate'){const error=new Error('Session must be contiguous: develop');error.location={node:'build',field:'session'};throw error;}return {};}");
  run("select('build');fieldInputs.session.input.value='develop'");
  get('save-node-fields').onclick();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(run("fieldInputs.session.input").attributes['aria-invalid'],'true','session contiguity error shows on save');
  // F13: applied roster takes priority over an unrelated preset, including project switches.
  run("documentDraft=null;request=async(op)=>op==='bootstrap'?{token:'fixture',projects:[{id:'P',name:'Project',version:1,workflow:{id:'project-roles',version:1,document:{schema_version:1,id:'project-roles',name:'Applied roles',nodes:[{id:'dba',kind:'role',role_id:'dba'}],edges:[]}}}],presets:[{schema_version:1,id:'quick-fix',name:'Quick fix',nodes:[{id:'developer',kind:'role',role_id:'developer'}],edges:[]}]}:{workflows:[]}");
  await run('reload()');
  assert.equal(run('documentDraft.id'),'project-roles');
  assert.match(get('status').textContent,/今|지금 적용된/);
  assert.equal(get('save').disabled,true);
  assert.equal(get('apply').disabled,true);
  const snapshot=JSON.stringify(run('documentDraft'));
  await assert.rejects(run('apply()'),/복제해서 저장/);
  // Export uses the local snapshot, not an absent workflow_versions row.
  assert.equal(run('version'),0);
  get('project').listeners.change();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(JSON.stringify(run('documentDraft')),snapshot);
  assert.equal(get('apply').disabled,true);
  get('clone').listeners.click();
  assert.equal(run('documentDraft.id'),'project-roles-copy');
  assert.equal(get('save').disabled,false);
  assert.equal(get('apply').disabled,true);
  assert.equal(run('version'),0);
  console.log('Applied Workflow snapshot and clone interactions passed');
  console.log('Workflow layout editor interactions passed');
})();
