// shared.js — common helpers used across all pages
// Loaded in base.html before page-specific JS

// ---------- global state (shared) ----------
let currentConfig = null;
let serverStatus = { running: false };

// ---------- helpers ----------
async function fetchJSON(url, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body && !headers['Content-Type'] && !headers['content-type']) {
    headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(url, Object.assign({}, opts, { headers }));
  let data = null;
  try { data = await r.json(); } catch(_) {}
  return { ok: r.ok, status: r.status, data };
}
function fmt(n, d=2) { return (n || 0).toLocaleString(undefined, {maximumFractionDigits: d}); }
function fmtCost(n) { return '$' + fmt(n, 4); }
function esc(s) { return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}
function fmtUptime(s) {
  if (s == null) return '—';
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  if (h > 0) return h+'h '+m+'m';
  if (m > 0) return m+'m '+sec+'s';
  return sec+'s';
}
function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}
function fmtPrice(p) {
  if (p == null) return '—';
  return '$' + (p * 1e6).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// ---------- toast & confirm ----------
const TOAST_ICONS = { success: '✓', error: '!', info: 'i' };
function toast(message, type='info', timeout=3800) {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = '<div class="toast-icon">'+(TOAST_ICONS[type]||'i')+'</div><div class="toast-body">'+esc(message)+'</div><button class="toast-close" onclick="dismissToast(this.parentElement)">&times;</button>';
  container.appendChild(el);
  if (timeout) setTimeout(() => dismissToast(el), timeout);
}
function dismissToast(el) {
  if (!el || el.classList.contains('hiding')) return;
  el.classList.add('hiding');
  setTimeout(() => el.remove(), 200);
}

let _activeModal = null;
function closeModal(overlay) {
  const el = overlay || _activeModal;
  if (!el) return;
  el.remove();
  if (_activeModal === el) _activeModal = null;
  document.removeEventListener('keydown', _modalEscHandler);
}
function _modalEscHandler(e) {
  if (e.key === 'Escape') closeModal();
}
/**
 * Open a shared modal. opts: { title, bodyHtml, widthClass, onMount }
 * Returns the overlay element. Call closeModal() to dismiss.
 */
function openModal({ title = '', bodyHtml = '', widthClass = '', onMount = null } = {}) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  if (title) overlay.setAttribute('aria-label', title);
  overlay.innerHTML =
    `<div class="modal ${widthClass}">` +
    (title ? `<h3>${esc(title)}</h3>` : '') +
    `<div class="modal-body">${bodyHtml}</div></div>`;
  document.body.appendChild(overlay);
  _activeModal = overlay;
  overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(overlay); });
  document.addEventListener('keydown', _modalEscHandler);
  const focusable = overlay.querySelector('input, select, textarea, button');
  if (focusable) focusable.focus();
  if (onMount) onMount(overlay);
  return overlay;
}

async function confirm2(message) {
  return showConfirm('Confirm', message);
}
function showConfirm(title, message) {
  return new Promise(resolve => {
    const overlay = openModal({
      title,
      bodyHtml:
        `<p class="modal-sub">${esc(message)}</p>` +
        `<div class="modal-actions">` +
        `<button class="secondary" data-act="no">Cancel</button>` +
        `<button class="danger" data-act="yes">Confirm</button></div>`,
    });
    overlay.querySelector('[data-act="yes"]').onclick = () => { closeModal(overlay); resolve(true); };
    overlay.querySelector('[data-act="no"]').onclick = () => { closeModal(overlay); resolve(false); };
  });
}

// ---------- server status / control (server pill) ----------
async function loadServerStatus() {
  try {
    const { data } = await fetchJSON('/admin/server/status');
    serverStatus = data || { running: false };
  } catch(_) { serverStatus = { running: false }; }
  updateServerUI();
}
function updateServerUI() {
  const running = serverStatus.running;
  const dot = document.getElementById('pill-dot');
  if (dot) dot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  const pillText = document.getElementById('pill-text');
  if (pillText) pillText.textContent = running ? 'Gateway running' : 'Gateway stopped';

  const stateHtml = running
    ? '<span class="badge badge-green">running</span>'
    : '<span class="badge badge-red">stopped</span>';
  for (const prefix of ['dash', 'log']) {
    const state = document.getElementById(prefix + '-state');
    const pid = document.getElementById(prefix + '-pid');
    const up = document.getElementById(prefix + '-uptime');
    if (state) state.innerHTML = stateHtml;
    if (pid) pid.textContent = serverStatus.pid != null ? serverStatus.pid : '—';
    if (up) up.textContent = fmtUptime(serverStatus.uptime);
  }
  const dp = document.getElementById('dash-port-input');
  if (dp && serverStatus.port != null) dp.value = serverStatus.port;

  for (const prefix of ['dash', 'log']) {
    const start = document.getElementById(prefix + '-start');
    const stop = document.getElementById(prefix + '-stop');
    if (start) start.disabled = running;
    if (stop) stop.disabled = !running;
  }
}
async function startServer() {
  toast('Starting gateway…', 'info', 2000);
  await fetch('/admin/server/start', { method: 'POST' });
  setTimeout(async () => { await loadServerStatus(); toast('Gateway started', 'success'); }, 800);
}
async function stopServer() {
  if (!await confirm2('Stop the gateway? The dashboard stays up, but model requests will fail until you start it again.')) return;
  await fetch('/admin/server/stop', { method: 'POST' });
  await loadServerStatus();
  toast('Gateway stopped', 'info');
}
async function restartServer() {
  toast('Restarting gateway…', 'info', 2000);
  await fetch('/admin/server/restart', { method: 'POST' });
  setTimeout(async () => { await loadServerStatus(); toast('Gateway restarted', 'success'); }, 1000);
}

// ---------- routing help ----------
function toggleRoutingHelp() {
  const panel = document.getElementById('routing-help-panel');
  if (panel) panel.classList.toggle('hidden');
}

// ---------- theme ----------
const THEME_KEY = 'lg-theme';
function currentTheme() { return document.documentElement.dataset.theme || 'dark'; }
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch(_) {}
  document.querySelectorAll('[data-theme-toggle]').forEach(b => {
    b.textContent = theme === 'dark' ? '\u263E' : '\u2600';
  });
}
function toggleTheme() { setTheme(currentTheme() === 'dark' ? 'light' : 'dark'); }
window.addEventListener('DOMContentLoaded', () => setTheme(currentTheme()));

// ---------- command palette ----------
const COMMANDS = [
  { label: 'Go to Dashboard', hint: 'Navigate to dashboard', action: () => location.href = '/' },
  { label: 'Go to Models', hint: 'Navigate to models catalog', action: () => location.href = '/models' },
  { label: 'Go to Providers & Models', hint: 'Navigate to config editor', action: () => location.href = '/providers' },
  { label: 'Go to Usage', hint: 'View usage analytics', action: () => location.href = '/usage' },
  { label: 'Go to Logs', hint: 'View system logs', action: () => location.href = '/logs' },
  { label: 'Go to Settings', hint: 'View settings', action: () => location.href = '/settings' },
  { label: 'Toggle Theme', hint: 'Switch dark/light mode', action: () => toggleTheme() },
  { label: 'Reload Config', hint: 'Reload gateway configuration', action: () => fetch('/admin/config/reload', { method: 'POST' }).then(() => toast('Config reloaded', 'success')) },
  { label: 'Test Gateway Health', hint: 'Check gateway health', action: () => fetch('/admin/health').then(r => r.json()).then(d => toast('Health: ' + (d.providers ? 'OK' : 'Error'), 'success')) },
];

function openCommandPalette() {
  document.getElementById('command-palette').classList.remove('hidden');
  const input = document.getElementById('palette-search');
  input.value = '';
  input.focus();
  renderPaletteResults('');
}

function closeCommandPalette() {
  document.getElementById('command-palette').classList.add('hidden');
}

function renderPaletteResults(query) {
  const results = document.getElementById('palette-results');
  const q = query.toLowerCase();
  const filtered = COMMANDS.filter(c => c.label.toLowerCase().includes(q) || c.hint.toLowerCase().includes(q));
  if (!filtered.length) {
    results.innerHTML = '<div class="palette-item"><span class="label">No commands found</span></div>';
    return;
  }
  results.innerHTML = filtered.map((c, i) =>
    `<div class="palette-item ${i === 0 ? 'selected' : ''}" onclick="executeCommand(${COMMANDS.indexOf(c)})">
      <span class="label">${esc(c.label)}</span>
      <span class="hint">${esc(c.hint)}</span>
    </div>`
  ).join('');
}

function executeCommand(index) {
  COMMANDS[index].action();
  closeCommandPalette();
}

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault();
    const palette = document.getElementById('command-palette');
    if (palette.classList.contains('hidden')) openCommandPalette();
    else closeCommandPalette();
  }
  if (e.key === 'Escape') {
    const palette = document.getElementById('command-palette');
    if (!palette.classList.contains('hidden')) closeCommandPalette();
  }
});

document.addEventListener('DOMContentLoaded', () => {
  const paletteInput = document.getElementById('palette-search');
  if (paletteInput) {
    paletteInput.addEventListener('input', (e) => renderPaletteResults(e.target.value));
    paletteInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const selected = document.querySelector('.palette-item.selected');
        if (selected) selected.click();
      }
    });
  }
  const globalSearch = document.getElementById('global-search');
  if (globalSearch) {
    globalSearch.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const q = globalSearch.value.trim();
        if (q) {
          location.href = '/models?q=' + encodeURIComponent(q);
        }
      }
    });
  }
});

// ---------- sidebar toggle ----------
const SIDEBAR_KEY = 'lg-sidebar';
function currentSidebarCollapsed() { return document.body.classList.contains('sidebar-collapsed'); }
function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  document.body.classList.remove('sidebar-open');
  try { localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0'); } catch(_) {}
  const btn = document.getElementById('sidebar-toggle');
  if (btn) btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}
function toggleSidebar() {
  if (window.innerWidth < 900) {
    const open = document.body.classList.toggle('sidebar-open');
    const btn = document.getElementById('sidebar-toggle');
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  } else {
    setSidebarCollapsed(!currentSidebarCollapsed());
  }
}
window.addEventListener('DOMContentLoaded', () => {
  try { if (localStorage.getItem(SIDEBAR_KEY) === '1') setSidebarCollapsed(true); } catch(_) {}
  const btn = document.getElementById('sidebar-toggle');
  if (btn && window.innerWidth >= 900) {
    btn.setAttribute('aria-expanded', currentSidebarCollapsed() ? 'false' : 'true');
  }
});
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'b') {
    e.preventDefault();
    toggleSidebar();
  }
});

// ---------- shared init ----------
loadServerStatus();
setInterval(loadServerStatus, 5000);