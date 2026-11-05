// Shared progress bar for long waits. AI tools and checks report no exact progress, so a
// bar is either a known fraction (done steps / all steps, elapsed / usual time) or
// "unknown length" (a sliding bar). The bar is not a live region: pages keep their own
// status text, and the bar's aria-valuetext carries the detail.
(function () {
  function create(label) {
    const bar = document.createElement('div');
    bar.className = 'ui-progress';
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', label);
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    const fill = document.createElement('div');
    fill.className = 'ui-progress-fill';
    bar.append(fill);
    // fraction: 0..1, or null when the length is unknown. state: '' | 'active' | 'over'.
    function set(fraction, text, state) {
      const known = typeof fraction === 'number';
      bar.classList.toggle('indeterminate', !known);
      bar.classList.toggle('active', state === 'active');
      bar.classList.toggle('over', state === 'over');
      if (known) {
        const percent = Math.max(0, Math.min(100, Math.round(fraction * 100)));
        fill.style.width = percent + '%';
        bar.setAttribute('aria-valuenow', String(percent));
      } else {
        fill.style.width = '';
        bar.removeAttribute('aria-valuenow');
      }
      if (text) bar.setAttribute('aria-valuetext', text);
      else bar.removeAttribute('aria-valuetext');
    }
    return {element: bar, set};
  }
  // An unknown-length bar shown right after `anchor` while `busy` is true.
  function busyAfter(anchor, busy, label) {
    if (!anchor) return;
    let next = anchor.nextElementSibling;
    const shown = next && next.classList.contains('ui-progress') && next.dataset.busyFor === anchor.id;
    if (!busy) { if (shown) next.remove(); return; }
    if (shown) return;
    const bar = create(label || '처리 중');
    bar.element.dataset.busyFor = anchor.id;
    bar.set(null, label);
    anchor.after(bar.element);
  }
  window.DevRosterProgress = {create, busyAfter};
})();
