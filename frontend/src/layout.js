// Persist only the panel ratio, never case details or searches.
export function panelBounds(width) {
  const available = Math.max(0, width - 8);
  return { min: Math.min(320, available / 2), max: Math.max(available / 2, Math.min(800, available - 320)), available };
}

export function panelWidth(width, ratio = .36) {
  const { min, max, available } = panelBounds(width);
  const preferred = Number.isFinite(ratio) ? ratio : .36;
  return Math.round(Math.min(max, Math.max(min, available * preferred)));
}

export function initWorkspaceLayout() {
  const workspace = document.querySelector('.workspace');
  const divider = document.querySelector('#workspace-divider');
  const narrow = matchMedia('(max-width: 800px)');
  const storageKey = 'carepath.panel-ratio.v1';
  let ratio = .36, pointer = null;
  try {
    const stored = Number(localStorage.getItem(storageKey));
    if (stored > 0 && stored < 1) ratio = stored;
  } catch { /* Layout still works when storage is unavailable. */ }
  const save = () => { try { localStorage.setItem(storageKey, String(ratio)); } catch {} };
  function render() {
    divider.hidden = narrow.matches;
    if (narrow.matches) return;
    const width = workspace.getBoundingClientRect().width;
    const bounds = panelBounds(width), current = panelWidth(width, ratio);
    workspace.style.setProperty('--search-width', `${current}px`);
    divider.setAttribute('aria-valuemin', String(Math.round(bounds.min)));
    divider.setAttribute('aria-valuemax', String(Math.round(bounds.max)));
    divider.setAttribute('aria-valuenow', String(current));
    divider.setAttribute('aria-valuetext', `${Math.round(current / bounds.available * 100)}% search, ${Math.round((bounds.available - current) / bounds.available * 100)}% map`);
  }
  function resizeTo(x) {
    const rect = workspace.getBoundingClientRect(), bounds = panelBounds(rect.width);
    ratio = Math.min(bounds.max, Math.max(bounds.min, x - rect.left)) / bounds.available;
    render();
  }
  divider.addEventListener('pointerdown', event => {
    if (event.button !== 0 || narrow.matches) return;
    event.preventDefault(); divider.focus(); pointer = event.pointerId;
    divider.setPointerCapture(pointer); workspace.classList.add('is-resizing');
  });
  divider.addEventListener('pointermove', event => { if (event.pointerId === pointer) resizeTo(event.clientX); });
  const finish = () => { pointer = null; workspace.classList.remove('is-resizing'); save(); };
  divider.addEventListener('pointerup', finish);
  divider.addEventListener('pointercancel', finish);
  divider.addEventListener('lostpointercapture', finish);
  divider.addEventListener('dblclick', () => { ratio = .36; render(); save(); });
  divider.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const rect = workspace.getBoundingClientRect(), bounds = panelBounds(rect.width);
    const step = event.shiftKey ? 48 : 16;
    const current = panelWidth(rect.width, ratio);
    resizeTo(rect.left + (event.key === 'Home' ? bounds.min : event.key === 'End' ? bounds.max : current + (event.key === 'ArrowLeft' ? -step : step)));
    save();
  });
  new ResizeObserver(render).observe(workspace);
  narrow.addEventListener('change', render);
  render();
}
