/* Shared feedback and popups for every product route (window.DevRosterUI).
   Rules
   - toast: bottom-right (bottom-centre ≤767px), at most 3, success/info close after 4 s (paused while
     hovered or focused), errors stay until closed, an identical message bumps a ×N counter.
   - dialog: native <dialog>.showModal() (focus containment, Esc, inert page). Default focus is the safe
     action for destructive dialogs, the first field for input dialogs, otherwise the primary action.
     Focus returns to the opener. Buttons: [secondary … primary] right-aligned.
   - menu/listbox: arrow keys, Enter, Esc, outside click; focus returns to the anchor.
   - drawer: Esc / backdrop closes, focus returns to the opener.
   - busy(button, work): blocks duplicate submits and shows the button spinner.
   Uses only DOM/CSSOM APIs allowed by the product CSP (no inline style attributes or scripts). */
(() => {
  'use strict';
  if (globalThis.DevRosterUI) return;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = (name, cls = 'i sm') => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><use href="/brand-mark.svg#nav-${name}"/></svg>`;
  const reduced = () => globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const region = () => {
    let r = document.getElementById('ui-toasts');
    if (!r) { r = document.createElement('div'); r.id = 'ui-toasts'; r.className = 'toast-region'; r.setAttribute('aria-live', 'polite'); r.setAttribute('aria-relevant', 'additions'); document.body.append(r); }
    return r;
  };

  /* Toasts */
  const MAX = 3, LIFETIME = 4000, live = new Map();
  function toast(message, { type = 'success', title = '', action = null, duration } = {}) {
    const key = type + '|' + title + '|' + message;
    if (live.has(key)) {
      const t = live.get(key); t.count++;
      const rep = t.el.querySelector('.t-repeat'); rep.hidden = false; rep.textContent = '×' + t.count;
      t.restart(); return t;
    }
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    const ico = { success: 'check-circle', error: 'alert-circle', info: 'info' }[type] || 'info';
    el.innerHTML = `${icon(ico)}<div class="t-body">${title ? `<b>${esc(title)}</b>\n` : ''}${esc(message)}<span class="t-repeat" hidden></span></div>` +
      (action ? `<button class="t-action" type="button">${esc(action.label)}</button>` : '') +
      `<button class="t-close" type="button" aria-label="알림 닫기">${icon('x')}</button>`;
    const auto = duration ?? (type === 'error' ? 0 : LIFETIME);
    let timer = null;
    const t = {
      el, count: 1,
      close() {
        if (!live.has(key)) return; live.delete(key); clearTimeout(timer);
        if (reduced()) el.remove(); else { el.classList.add('leaving'); el.addEventListener('animationend', () => el.remove(), { once: true }); setTimeout(() => el.remove(), 400); }
      },
      restart() { clearTimeout(timer); if (auto) timer = setTimeout(() => t.close(), auto); }
    };
    el.querySelector('.t-close').onclick = () => t.close();
    if (action) el.querySelector('.t-action').onclick = () => { t.close(); action.run(); };
    el.addEventListener('mouseenter', () => clearTimeout(timer));
    el.addEventListener('mouseleave', () => t.restart());
    el.addEventListener('focusin', () => clearTimeout(timer));
    el.addEventListener('focusout', () => t.restart());
    live.set(key, t);
    region().prepend(el);
    const all = [...live.values()];
    if (all.length > MAX) (all.find(x => !x.el.classList.contains('error')) || all[0]).close();
    t.restart();
    return t;
  }

  /* Ctrl+Enter (⌘+Enter on macOS) presses the button that finishes what is being written.
     Held keys do not repeat it, and Korean input still being composed is left alone. */
  const submitKey = e => e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey
    && !e.repeat && !e.isComposing && e.keyCode !== 229;
  const usable = b => b instanceof HTMLButtonElement && !b.disabled && b.getAttribute('aria-disabled') !== 'true' && b.getClientRects().length > 0;
  /* The button a field submits with: ``data-submit="button-id"``, or the submit button of the
     text area's form. Plain inputs already submit their form with Enter. */
  function submitTarget(field) {
    if (!field || field.disabled || field.readOnly) return null;
    if (field.dataset?.submit) return document.getElementById(field.dataset.submit);
    if (field.tagName === 'TEXTAREA' && field.form) return field.form.querySelector('button[type=submit]:not([data-cancel]), button:not([type])');
    return null;
  }
  document.addEventListener('keydown', e => {
    if (!submitKey(e) || e.defaultPrevented || document.querySelector('dialog[open]')) return;
    const target = submitTarget(e.target.closest?.('textarea,input,select'));
    if (!usable(target)) return;
    e.preventDefault();
    target.click();
  });

  /* Dialogs */
  function dialog({ title, body = '', tone = 'accent', iconName, confirm = '확인', cancel = '취소', danger = false, input = null, wide = false, content = null }) {
    return new Promise(resolve => {
      const opener = document.activeElement;
      const d = document.createElement('dialog');
      d.className = 'modal' + (wide ? ' wide' : '');
      const id = 'dlg-' + Math.random().toString(36).slice(2, 8);
      d.setAttribute('aria-labelledby', id + '-t');
      if (body) d.setAttribute('aria-describedby', id + '-d');
      const ico = iconName || (danger || tone === 'warning' ? 'alert-triangle' : 'info');
      d.innerHTML = `<div class="modal-head"><span class="m-ico ${danger ? 'danger' : tone}">${icon(ico, 'i')}</span>
          <div class="m-text"><h2 id="${id}-t">${esc(title)}</h2>${body ? `<p id="${id}-d">${esc(body)}</p>` : ''}</div>
          <button class="ghost icon sm modal-x" type="button" aria-label="닫기">${icon('x')}</button></div>
        ${input || content ? '<div class="modal-body"></div>' : ''}
        <form method="dialog" class="modal-foot">${cancel ? `<button value="cancel" type="submit" data-cancel>${esc(cancel)}</button>` : ''}<button class="${danger ? 'danger' : 'primary'}" value="ok" type="submit" data-ok>${esc(confirm)}</button></form>`;
      const bodyEl = d.querySelector('.modal-body');
      let field = null;
      if (input) {
        const tag = input.multiline ? 'textarea' : 'input';
        bodyEl.innerHTML = `<div class="field"><label for="${id}-i">${esc(input.label)}</label>
          <${tag} id="${id}-i" maxlength="${input.max || 120}" placeholder="${esc(input.placeholder || '')}" aria-describedby="${id}-h ${id}-e"${tag === 'input' ? ` value="${esc(input.value || '')}"` : ''}>${tag === 'textarea' ? esc(input.value || '') + '</textarea>' : ''}
          ${input.help ? `<span class="help" id="${id}-h">${esc(input.help)}</span>` : ''}
          <div class="field-error" id="${id}-e" hidden>${icon('alert-circle')}<span></span></div></div>`;
        field = bodyEl.querySelector(tag);
      }
      if (content) bodyEl.append(content);
      document.body.append(d);
      const finish = value => {
        if (!d.open) return;
        // Close at once so the caller sees the decision immediately (selects revert, focus returns);
        // only the opening is animated.
        d.close(); d.remove(); if (opener?.isConnected) opener.focus?.(); resolve(value);
      };
      d.querySelector('.modal-x').onclick = () => finish(null);
      d.querySelector('form').addEventListener('submit', e => {
        e.preventDefault();
        const ok = e.submitter?.value === 'ok';
        if (ok && field) {
          const err = input.validate?.(field.value.trim());
          const box = d.querySelector('#' + id + '-e');
          if (err) { field.setAttribute('aria-invalid', 'true'); box.hidden = false; box.querySelector('span').textContent = err; field.focus(); return; }
          return finish(field.value.trim());
        }
        finish(ok ? true : null);
      });
      if (field) field.addEventListener('input', () => { field.removeAttribute('aria-invalid'); d.querySelector('#' + id + '-e').hidden = true; });
      d.addEventListener('keydown', e => {
        // Ctrl+Enter confirms, except a dangerous action: that one needs its button pressed.
        if (submitKey(e)) { e.preventDefault(); if (!danger) d.querySelector('[data-ok]').click(); return; }
        if (e.key !== 'Tab') return;
        const f = [...d.querySelectorAll('button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),a[href]')];
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
        else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
      });
      d.addEventListener('cancel', e => { e.preventDefault(); finish(null); });
      d.addEventListener('click', e => { if (e.target === d) finish(null); });
      d.showModal();
      (field || (danger ? d.querySelector('[data-cancel]') : d.querySelector('[data-ok]')) || d.querySelector('[data-ok]')).focus();
    });
  }
  /* Drop-in replacement for window.confirm with an explicit, action-named destructive button. */
  const confirmAction = (title, body, confirm, opts = {}) => dialog({ title, body, confirm, danger: opts.danger ?? true, cancel: opts.cancel || '취소', iconName: opts.iconName });

  /* Menus and list boxes */
  function closeMenus() { document.querySelectorAll('.ui-menu').forEach(m => m.remove()); }
  function place(m, anchor, alignRight) {
    const r = anchor.getBoundingClientRect();
    m.style.top = (r.bottom + window.scrollY + 6) + 'px';
    const left = alignRight ? r.right - m.offsetWidth : r.left;
    m.style.left = Math.max(8, Math.min(left, window.innerWidth - m.offsetWidth - 8)) + 'px';
  }
  function trackOutside(m, anchor, done) {
    const outside = e => { if (!m.contains(e.target) && !anchor.contains(e.target)) { m.remove(); anchor.setAttribute('aria-expanded', 'false'); document.removeEventListener('pointerdown', outside, true); } };
    document.addEventListener('pointerdown', outside, true);
    return () => { document.removeEventListener('pointerdown', outside, true); done(); };
  }
  function menu(anchor, items) {
    closeMenus();
    const m = document.createElement('div'); m.className = 'ui-menu'; m.setAttribute('role', 'menu');
    items.forEach(it => {
      if (it === '-') { const s = document.createElement('div'); s.className = 'menu-sep'; s.setAttribute('role', 'separator'); m.append(s); return; }
      const b = document.createElement('button'); b.type = 'button'; b.setAttribute('role', 'menuitem');
      if (it.danger) b.classList.add('danger');
      b.innerHTML = (it.icon ? icon(it.icon) : '') + `<span class="opt-label">${esc(it.label)}</span>`;
      if (it.disabled) {
        b.setAttribute('aria-disabled', 'true');
        // The reason stays visible (not only a tooltip) so touch and keyboard users see it too.
        if (it.reason) { b.title = it.reason; b.classList.add('has-reason'); const r = document.createElement('span'); r.className = 'opt-reason'; r.textContent = it.reason; b.append(r); }
      }
      b.onclick = () => { if (it.disabled) return; finish(); it.run(); };
      m.append(b);
    });
    document.body.append(m); place(m, anchor, true);
    anchor.setAttribute('aria-expanded', 'true');
    const btns = [...m.querySelectorAll('[role=menuitem]')];
    const finish = trackOutside(m, anchor, () => { m.remove(); anchor.setAttribute('aria-expanded', 'false'); anchor.focus(); });
    btns[0]?.focus();
    m.addEventListener('keydown', e => {
      const i = btns.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { btns[(i + 1) % btns.length].focus(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { btns[(i - 1 + btns.length) % btns.length].focus(); e.preventDefault(); }
      else if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); finish(); }
    });
  }
  function listbox(anchor, { items, selected, label, onSelect, footer }) {
    closeMenus();
    const m = document.createElement('div'); m.className = 'ui-menu';
    m.innerHTML = `<div class="menu-label">${esc(label)}</div><div role="listbox" aria-label="${esc(label)}" tabindex="-1"></div>`;
    const lb = m.querySelector('[role=listbox]');
    items.forEach((it, i) => {
      const o = document.createElement('div');
      o.setAttribute('role', 'option'); o.id = 'ui-opt-' + i; o.setAttribute('aria-selected', String(it.value === selected)); o.title = it.label;
      o.innerHTML = `<span class="opt-label">${esc(it.label)}</span>${it.meta ? `<span class="badge ${esc(it.tone || '')}">${esc(it.meta)}</span>` : ''}${it.value === selected ? icon('check') : ''}`;
      o.onclick = () => { finish(); onSelect(it.value); };
      lb.append(o);
    });
    (footer || []).forEach((f, i) => {
      if (!i) { const s = document.createElement('div'); s.className = 'menu-sep'; m.append(s); }
      const b = document.createElement('button'); b.type = 'button'; b.setAttribute('role', 'menuitem');
      b.innerHTML = icon(f.icon || 'plus') + `<span class="opt-label">${esc(f.label)}</span>`;
      b.onclick = () => { finish(); f.run(); }; m.append(b);
    });
    document.body.append(m); m.style.width = Math.min(360, window.innerWidth - 16) + 'px'; place(m, anchor, false);
    anchor.setAttribute('aria-expanded', 'true');
    const opts = [...lb.children];
    let idx = Math.max(0, opts.findIndex(o => o.getAttribute('aria-selected') === 'true'));
    const mark = () => { opts.forEach((o, i) => o.classList.toggle('active', i === idx)); if (opts[idx]) { lb.setAttribute('aria-activedescendant', opts[idx].id); opts[idx].scrollIntoView({ block: 'nearest' }); } };
    const finish = trackOutside(m, anchor, () => { m.remove(); anchor.setAttribute('aria-expanded', 'false'); anchor.focus(); });
    lb.focus(); mark();
    m.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { idx = Math.min(opts.length - 1, idx + 1); mark(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { idx = Math.max(0, idx - 1); mark(); e.preventDefault(); }
      else if (e.key === 'Home') { idx = 0; mark(); e.preventDefault(); }
      else if (e.key === 'End') { idx = opts.length - 1; mark(); e.preventDefault(); }
      else if (e.key === 'Enter' && e.target === lb) { opts[idx]?.click(); e.preventDefault(); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(); }
    });
  }

  /* Drawer (detail panel on narrow screens) */
  let drawerState = null;
  function openDrawer(panel, opener) {
    closeDrawer();
    let bd = document.getElementById('ui-drawer-backdrop');
    if (!bd) { bd = document.createElement('div'); bd.id = 'ui-drawer-backdrop'; bd.className = 'drawer-backdrop'; document.body.append(bd); }
    bd.hidden = false; panel.classList.add('open'); panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true');
    drawerState = { panel, opener, bd }; bd.onclick = closeDrawer;
    (panel.querySelector('[aria-selected="true"]') || panel.querySelector('button,input,textarea,select'))?.focus();
  }
  function closeDrawer() {
    if (!drawerState) return;
    const { panel, opener, bd } = drawerState; drawerState = null;
    panel.classList.remove('open'); panel.removeAttribute('role'); panel.removeAttribute('aria-modal'); bd.hidden = true;
    opener?.focus?.();
  }
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && drawerState && !document.querySelector('dialog[open]')) closeDrawer(); });
  document.addEventListener('click', e => { if (e.target.closest?.('[data-close-drawer]')) closeDrawer(); });

  /* Tabs with roving tabindex */
  function tabs(list, onChange) {
    const t = [...list.querySelectorAll('[role=tab]')];
    const select = tab => {
      t.forEach(x => { const on = x === tab; x.setAttribute('aria-selected', String(on)); x.tabIndex = on ? 0 : -1; const p = document.getElementById(x.getAttribute('aria-controls')); if (p) p.hidden = !on; });
      onChange?.(tab);
    };
    t.forEach(x => x.addEventListener('click', () => select(x)));
    list.addEventListener('keydown', e => {
      const i = t.indexOf(document.activeElement); if (i < 0) return;
      const n = e.key === 'ArrowRight' ? t[(i + 1) % t.length] : e.key === 'ArrowLeft' ? t[(i - 1 + t.length) % t.length] : null;
      if (n) { n.focus(); select(n); e.preventDefault(); }
    });
    return { select: id => select(document.getElementById(id)) };
  }

  /* Busy buttons: block duplicate submits */
  async function busy(btn, work) {
    if (!btn || btn.getAttribute('aria-busy') === 'true') return undefined;
    const was = btn.disabled;
    btn.setAttribute('aria-busy', 'true'); btn.disabled = true;
    try { return await work(); } finally { btn.removeAttribute('aria-busy'); btn.disabled = was; }
  }

  /* Human-readable message for network/server failures instead of raw "Failed to fetch". */
  function errorMessage(error, fallback = '요청을 처리하지 못했습니다.') {
    const text = String(error?.message || error || '');
    if (!text || /failed to fetch|networkerror|load failed|network request failed/i.test(text)) return '서버에 연결하지 못했습니다. DevRoster 앱이 실행 중인지 확인하고 다시 시도하세요.';
    if (/unexpected token|json/i.test(text)) return fallback + ' 서버 응답을 읽지 못했습니다.';
    return text;
  }

  globalThis.DevRosterUI = Object.freeze({ toast, dialog, confirmAction, menu, listbox, closeMenus, openDrawer, closeDrawer, tabs, busy, esc, icon, errorMessage });
})();
