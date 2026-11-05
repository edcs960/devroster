/* Declarative mock renderer: AI content is text only, never HTML, CSS or executable code. */
(() => {
  'use strict';
  const node = (tag, text, cls) => {
    const e = document.createElement(tag); if (text !== undefined) e.textContent = text;
    if (cls) e.className = cls; return e;
  };
  function render(canvas, mock, screenId, options = {}) {
    canvas.replaceChildren();
    const tone = ['indigo','blue','green','neutral'].includes(mock.tone) ? mock.tone : 'indigo';
    canvas.className = 'mock-canvas tone-' + tone + (options.mobile ? ' mobile' : '') + (options.commenting ? ' commenting' : '');
    const screen = mock.screens.find(s => s.id === screenId); if (!screen) return;
    const root = node('div', undefined, 'mock-root layout-' + screen.layout); canvas.append(root);
    const feedback = message => {
      let box = root.querySelector('.mock-feedback');
      if (!box) { box = node('div', undefined, 'mock-feedback'); box.setAttribute('role','status'); root.append(box); }
      box.textContent = message;
    };
    function element(item) {
      const wrap = node('div', undefined, 'mock-element kind-' + item.kind);
      wrap.dataset.elementId = item.id;
      if (options.selected === item.id) wrap.classList.add('selected');
      const comments = options.comments?.filter(c => c.element_id === item.id && c.screen_id === screenId && c.state === 'open').length || 0;
      if (comments) { wrap.classList.add('has-comment'); wrap.dataset.comments = comments; }
      if (options.commenting) {
        wrap.tabIndex = 0; wrap.setAttribute('role','button'); wrap.setAttribute('aria-label', item.label + ' 요소 선택');
        const choose = e => { e.preventDefault(); e.stopPropagation(); options.onSelect?.(item.id); };
        wrap.addEventListener('click',choose);
        wrap.addEventListener('keydown',e => { if (['Enter',' '].includes(e.key) && e.target === wrap) choose(e); });
      }
      const text = item.text || '';
      const label = () => wrap.append(node('h3',item.label));
      const passive = e => { if (options.commenting) { e.tabIndex = -1; if ('readOnly' in e) e.readOnly = true; } return e; };
      if (item.kind === 'heading') { wrap.append(node('h2',item.label)); if (text) wrap.append(node('p',text)); }
      else if (item.kind === 'text') { wrap.append(node('p',text || item.label)); }
      else if (['section','card'].includes(item.kind)) { label(); if (text) wrap.append(node('p',text)); }
      else if (item.kind === 'metric') { wrap.append(node('p',item.label),node('div',text || '—','metric-value')); }
      else if (item.kind === 'button') {
        const button = passive(node('button',item.label)); button.type = 'button';
        button.addEventListener('click',e => {
          if (options.commenting) return;
          e.stopPropagation();
          if (item.target_screen) options.onNavigate?.(item.target_screen);
          else {
            const missing = [...root.querySelectorAll('input:not([type=checkbox])')].some(i => !i.value.trim());
            feedback(missing ? '샘플 동작: 입력 내용을 작성해주세요.' : '샘플 동작: 요청이 완료되었습니다. 실제 데이터는 저장되지 않습니다.');
          }
        }); wrap.append(button);
      } else if (['input','select','checkbox'].includes(item.kind)) {
        const lab = node('label',item.label);
        const input = passive(node(item.kind === 'select' ? 'select' : 'input'));
        if (item.kind === 'checkbox') { input.type='checkbox'; lab.prepend(input); }
        else {
          if (item.kind === 'select') (item.options || ['선택해주세요']).forEach(t => input.append(node('option',t)));
          else { input.type='text'; input.placeholder=item.placeholder || ''; input.value=text; }
          lab.append(input);
        }
        wrap.append(lab); if (item.placeholder && item.kind === 'select') wrap.append(node('p',item.placeholder,'hint'));
      } else if (item.kind === 'table') {
        label(); const scroll=node('div',undefined,'mock-table-wrap'); const table=node('table');
        const head=node('thead'), tr=node('tr'); (item.columns || []).forEach(t=>tr.append(node('th',t))); head.append(tr);table.append(head);
        const body=node('tbody'); (item.rows || []).forEach(row=>{ const r=node('tr');row.forEach(t=>r.append(node('td',t)));body.append(r); });table.append(body);scroll.append(table);wrap.append(scroll);
        if (!item.rows?.length) wrap.append(node('p',text || '표시할 항목이 없습니다.','hint'));
      } else if (['nav','tabs'].includes(item.kind)) {
        const nav=node('div',undefined,'mock-nav'); (item.options || [item.label]).forEach(t=>{
          const button=passive(node('button',t));button.type='button';button.addEventListener('click',()=>{
            if (!options.commenting) { if(item.target_screen) options.onNavigate?.(item.target_screen);else feedback(t+' 샘플 화면을 선택했습니다.'); }
          });nav.append(button);
        });wrap.append(nav);
      } else if (item.kind === 'list') {label();const ul=node('ul');(item.options || [text]).forEach(t=>ul.append(node('li',t)));wrap.append(ul);}
      if (item.children?.length) { const group=node('div',undefined,'mock-children');item.children.forEach(c=>group.append(element(c)));wrap.append(group); }
      return wrap;
    }
    screen.elements.forEach(item=>root.append(element(item)));
  }
  window.StudioMock = {render};
})();
