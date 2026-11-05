'use strict';
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const navPath = process.argv[2];
const brandingPath = navPath.replace('navigation.js', 'branding.js');
const brandingScript = fs.existsSync(brandingPath) ? fs.readFileSync(brandingPath, 'utf8') : '';
const script = fs.readFileSync(navPath, 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.classList = {add() {}, remove() {}}; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener() {}
}
const pages = ['home', 'traceability', 'changes', 'studio', 'ai', 'setup', 'agent', 'dashboard', 'review', 'workflows', 'integrations', 'references', 'source', 'wiki', 'messengers'];
for (const pathname of [...pages.flatMap(page => ['/' + page, '/' + page + '/']), '/', '/studio//', '/studio/unknown', '/unknown/']) {
  const body = new Element('body');
  const ctx = {
    location: {pathname},
    document: {body, getElementById() { return null; }, querySelector() { return null; }, createElement(tag) { return new Element(tag); }}
  };
  if (brandingScript) vm.runInNewContext(brandingScript, ctx);
  vm.runInNewContext(script, ctx);
  const descendants = node => node.children.flatMap(child => [child, ...descendants(child)]);
  const links = descendants(body).filter(node => node.tag === 'a');
  const selected = links.filter(node => node.attributes['aria-current'] === 'page');
  const expected = pages.find(page => pathname === '/' + page || pathname === '/' + page + '/');
  assert.equal(selected.length, expected ? 1 : 0, pathname + ' has exactly the expected current link');
  if (expected) assert.equal(selected[0].href, '/' + expected, pathname + ' points to its canonical route');
  assert.deepEqual(links.map(link => link.href).sort(), pages.map(page => '/' + page).sort(), 'menu destinations remain allowlisted');
}
console.log('Navigation canonical/trailing-slash and unknown-route regression passed');

// Behavioral contract: desktop collapse, mobile modality and keyboard focus.
{
  let document;
  class UIElement {
    constructor(tag) {
      this.tag=tag;this.children=[];this.attributes={};this.events={};
      this.classes=new Set();
      this.classList={add:(...v)=>v.forEach(x=>this.classes.add(x)),remove:(...v)=>v.forEach(x=>this.classes.delete(x))};
    }
    append(...v){this.children.push(...v);}
    setAttribute(k,v){this.attributes[k]=v;}
    removeAttribute(k){delete this.attributes[k];}
    addEventListener(k,cb){this.events[k]=cb;}
    focus(){document.activeElement=this;}
    querySelectorAll(){return descendants(this).filter(x=>['button','a'].includes(x.tag));}
    contains(el){return descendants(this).includes(el);}
  }
  const descendants=node=>node.children.flatMap(x=>[x,...descendants(x)]);
  const media={matches:true,addEventListener(k,cb){this.onchange=cb;}};
  const body=new UIElement('body');
  document={body,activeElement:null,getElementById(){return null;},querySelector(){return null;},createElement:tag=>new UIElement(tag)};
  const ctx={document,location:{pathname:'/studio'},matchMedia:()=>media};
  vm.runInNewContext(script,ctx);
  const all=descendants(body),panel=all.find(x=>x.id==='app-menu'),toggle=all.find(x=>x.id==='app-menu-toggle');
  const close=panel.children[0].children[1];
  assert.equal(panel.hidden,false,'desktop navigation starts visible');
  assert.equal(panel.attributes.role,'navigation','desktop is a navigation landmark');
  assert.equal(panel.attributes['aria-modal'],undefined,'desktop navigation is not modal');
  toggle.focus();toggle.onclick();
  assert.equal(panel.hidden,true,'desktop can collapse');
  toggle.onclick();
  assert.equal(panel.hidden,false,'desktop can reopen');
  media.matches=false;media.onchange();
  assert.equal(panel.hidden,true,'narrow viewport starts with closed drawer');
  toggle.focus();toggle.onclick();
  assert.equal(document.activeElement,close,'opening mobile drawer focuses its close action');
  assert.equal(panel.attributes['aria-modal'],'true');
  const last=panel.querySelectorAll().at(-1);
  panel.events.keydown({key:'Tab',shiftKey:true,preventDefault(){}});
  assert.equal(document.activeElement,last,'Shift+Tab remains in mobile drawer');
  panel.events.keydown({key:'Tab',shiftKey:false,preventDefault(){}});
  assert.equal(document.activeElement,close,'Tab wraps back inside drawer');
  panel.events.keydown({key:'Escape',preventDefault(){}});
  assert.equal(panel.hidden,true);
  assert.equal(document.activeElement,toggle,'closing restores opener focus');
  assert.equal(body.classes.has('app-menu-open'),false,'closing releases page scrolling');
}
console.log('Desktop/mobile menu and focus regression passed');
