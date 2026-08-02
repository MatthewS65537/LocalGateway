// shared.js — common helpers used across all pages
// Loaded in base.html before page-specific JS.

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
function fmtPrice(p) {
  if (p == null) return '—';
  return '$' + (p * 1e6).toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function fmtPricePrecise(p) {
  if (p == null) return '—';
  return '$' + (p * 1e6).toLocaleString(undefined, { maximumFractionDigits: 6 });
}
// Escape for HTML body/text content (& < > ") and double-quoted attributes.
function esc(s) { return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
// Escape for double-quoted attribute values, additionally handling single quotes
// so values survive when re-read via dataset (e.g. data-id="...").
function escAttr(s) { return esc(s).replace(/'/g,'&#39;'); }
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

// ---------- copy-to-clipboard ----------
function copyId(text, btn) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      if (btn) { const orig = btn.innerHTML; btn.innerHTML = '✓'; setTimeout(() => { btn.innerHTML = orig; }, 1400); }
      toast('Copied ' + text, 'info', 1600);
    }).catch(() => {});
  }
}

// ---------- provider / model avatar ----------
const _AVATAR_PALETTE = [
  ['#10a37f', '#1a7f64'], ['#d97757', '#b85a3d'], ['#7c5cf1', '#5b3fd6'],
  ['#6366f1', '#4f46e5'], ['#0078d4', '#005a9e'], ['#f59e0b', '#d97706'],
  ['#ef4444', '#dc2626'], ['#14b8a6', '#0d9488'],
];
function _hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
  return Math.abs(h);
}
function _avatarFontSize(text, size) {
  const len = (text || '').length;
  const base = size >= 40 ? 1.1 : (size <= 20 ? 0.62 : 0.74);
  if (len <= 1) return base;
  if (len === 2) return base * 0.74;
  if (len === 3) return base * 0.58;
  if (len <= 5) return base * 0.46;
  return base * 0.36;
}
function _avatarLabel(customText, fallback) {
  if (customText && customText.trim()) return customText.trim();
  return (fallback || '?').charAt(0).toUpperCase();
}
function _avatarMarkup(label, colors, size) {
  const [c1, c2] = colors;
  const fs = _avatarFontSize(label, size);
  const pad = label.length > 2 ? 'padding:0 4px;' : '';
  return '<span class="avatar" style="width:'+size+'px;height:'+size+'px;font-size:'+fs+'rem;'+pad+'background:linear-gradient(135deg,'+c1+','+c2+')">'+esc(label)+'</span>';
}
function providerAvatar(providerId, size, avatarText) {
  const sz = size || 28;
  const label = _avatarLabel(avatarText, providerId);
  const [c1, c2] = _AVATAR_PALETTE[_hashStr(providerId || '') % _AVATAR_PALETTE.length];
  return _avatarMarkup(label, [c1, c2], sz);
}
function modelAvatar(modelId, displayName, size, avatarText) {
  const sz = size || 28;
  const label = _avatarLabel(avatarText, displayName || modelId);
  const [c1, c2] = _AVATAR_PALETTE[_hashStr(modelId || '') % _AVATAR_PALETTE.length];
  return _avatarMarkup(label, [c1, c2], sz);
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

// ---------- server status / control ----------
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

async function shutdownGateway() {
  const confirmed = await confirm2('Stop the gateway worker and close this dashboard?');
  if (!confirmed) return;
  try { await fetch('/admin/server/stop', { method: 'POST' }); } catch(e) {}
  document.body.innerHTML =
    '<div class="shutdown-screen">' +
    '<h1>Gateway shut down</h1>' +
    '<p>You can close this tab.</p></div>';
  setTimeout(() => { try { window.close(); } catch(e) {} }, 1500);
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

// ---------- command palette (fuzzy search: actions + models + providers) ----------
const COMMANDS = [
  { label: 'Dashboard', hint: 'Navigate to dashboard', action: () => location.href = '/' },
  { label: 'Models', hint: 'Navigate to model catalog', action: () => location.href = '/models' },
  { label: 'Providers', hint: 'Navigate to providers', action: () => location.href = '/providers' },
  { label: 'Usage', hint: 'View usage analytics', action: () => location.href = '/usage' },
  { label: 'Logs', hint: 'View system logs', action: () => location.href = '/logs' },
  { label: 'Settings', hint: 'View settings', action: () => location.href = '/settings' },
  { label: 'Toggle Theme', hint: 'Switch dark/light mode', action: () => toggleTheme() },
  { label: 'Reload Config', hint: 'Reload gateway configuration', action: () => fetch('/admin/config/reload', { method: 'POST' }).then(() => toast('Config reloaded', 'success')) },
  { label: 'Test Gateway Health', hint: 'Check gateway health', action: () => fetch('/admin/health').then(r => r.json()).then(d => toast('Health: ' + (d.providers ? 'OK' : 'Error'), 'success')) },
];

let paletteIndex = null;
async function ensurePaletteIndex() {
  if (paletteIndex) return paletteIndex;
  try {
    const { data } = await fetchJSON('/admin/config');
    const models = (data.models || []).map(m => ({
      label: m.display_name || m.id,
      hint: m.id,
      action: () => location.href = '/models/' + encodeURIComponent(m.id),
    }));
    const providers = (data.providers || []).map(p => ({
      label: p.name || p.id,
      hint: p.id,
      action: () => location.href = '/providers',
    }));
    paletteIndex = { models, providers };
  } catch(_) {
    paletteIndex = { models: [], providers: [] };
  }
  return paletteIndex;
}

function openCommandPalette() {
  document.getElementById('command-palette').classList.remove('hidden');
  const input = document.getElementById('palette-search');
  input.value = '';
  input.focus();
  renderPaletteResults('');
  ensurePaletteIndex().then(() => {
    const current = document.getElementById('palette-search').value;
    renderPaletteResults(current);
  });
}
function closeCommandPalette() {
  document.getElementById('command-palette').classList.add('hidden');
}

// Subsequence fuzzy scorer: returns a score >= 0 if every char of `q` appears
// in `text` in order, else -1. Lower score = better match; contiguous runs
// (and matches at word starts) rank higher.
function _fuzzyScore(q, text) {
  const t = String(text || '').toLowerCase();
  q = String(q || '').toLowerCase();
  if (!q) return 0;
  if (t === q) return 0;
  let score = 0, qi = 0, last = -2;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) {
      score += (i === last + 1) ? 0 : (i === 0 || t[i-1] === ' ' || t[i-1] === '-' || t[i-1] === '_' ? 0 : 3);
      last = i;
      qi++;
    }
  }
  if (qi < q.length) return -1;
  return score + t.length - q.length;
}

// Global invalidation so callers can drop the cached palette index after any
// config mutation (add/rename/delete model or provider).
function invalidatePalette() { paletteIndex = null; }

// Save a scoped change to config with optimistic-concurrency handling.
// mutate(data) applies the edit to a fresh GET copy; on a 409 (config changed
// since load) we re-GET and retry up to `maxRetries` times. On success we
// invalidate the palette so the fuzzy command index stays fresh. Returns the
// {ok, status} of the final attempt.
async function saveConfigSection(mutate, { maxRetries = 2, onConflict = null } = {}) {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    let data;
    try {
      const res = await fetchJSON('/admin/config');
      if (!res.ok) { toast('Failed to load config', 'error'); return { ok: false, status: res.status }; }
      data = res.data;
    } catch (e) { toast('Error: ' + e.message, 'error'); return { ok: false, status: 0 }; }
    mutate(data);
    const put = await fetchJSON('/admin/config', { method: 'PUT', body: JSON.stringify(data) });
    if (put.status === 409) {
      if (onConflict) onConflict();
      continue; // re-GET on next iteration and retry
    }
    if (put.ok) invalidatePalette();
    return { ok: put.ok, status: put.status };
  }
  toast('Config kept changing; please retry.', 'error');
  return { ok: false, status: 409 };
}

function _paletteMatches(q, item) {
  if (!q) return true;
  return _fuzzyScore(q, item.label) >= 0 || _fuzzyScore(q, item.hint) >= 0;
}

function renderPaletteResults(query) {
  const results = document.getElementById('palette-results');
  const q = query.toLowerCase();

  const _score = (item) => {
    const sl = _fuzzyScore(q, item.label);
    const sh = _fuzzyScore(q, item.hint);
    return Math.min(sl >= 0 ? sl : Infinity, sh >= 0 ? sh : Infinity);
  };

  const actionMatches = COMMANDS.map((c, i) => ({ ...c, _i: i }))
    .filter(c => _paletteMatches(q, c))
    .sort((a, b) => _score(a) - _score(b));
  const modelMatches = (paletteIndex ? paletteIndex.models : [])
    .filter(m => _paletteMatches(q, m))
    .sort((a, b) => _score(a) - _score(b));
  const providerMatches = (paletteIndex ? paletteIndex.providers : [])
    .filter(p => _paletteMatches(q, p))
    .sort((a, b) => _score(a) - _score(b));

  if (!actionMatches.length && !modelMatches.length && !providerMatches.length) {
    results.innerHTML = '<div class="palette-item"><span class="label">No results</span></div>';
    return;
  }

  let html = '';
  const selClass = (active) => active ? ' selected' : '';
  const track = (() => { let n = 0; return () => n++; })();

  if (actionMatches.length) {
    html += '<div class="palette-section">Actions</div>';
    actionMatches.forEach(c => {
      html += `<div class="palette-item${selClass(track() === 0)}" data-action="${c._i}">
        <span class="label">${esc(c.label)}</span>
        <span class="hint">${esc(c.hint)}</span></div>`;
    });
  }
  if (modelMatches.length) {
    html += '<div class="palette-section">Models</div>';
    modelMatches.forEach(m => {
      html += `<div class="palette-item${selClass(track() === 0)}" data-model="${escAttr(m.hint)}">
        <span class="label">${esc(m.label)}</span>
        <span class="hint">${esc(m.hint)}</span></div>`;
    });
  }
  if (providerMatches.length) {
    html += '<div class="palette-section">Providers</div>';
    providerMatches.forEach(p => {
      html += `<div class="palette-item${selClass(track() === 0)}" data-provider="${escAttr(p.hint)}">
        <span class="label">${esc(p.label)}</span>
        <span class="hint">${esc(p.hint)}</span></div>`;
    });
  }
  results.innerHTML = html;
}

function executePaletteItem(el) {
  const act = el.dataset.action;
  if (act != null) { COMMANDS[Number(act)].action(); closeCommandPalette(); return; }
  const modelId = el.dataset.model;
  if (modelId != null) { location.href = '/models/' + encodeURIComponent(modelId); closeCommandPalette(); return; }
  const providerId = el.dataset.provider;
  if (providerId != null) { location.href = '/providers'; closeCommandPalette(); }
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
        if (selected) executePaletteItem(selected);
      }
    });
    paletteInput.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const items = document.querySelectorAll('.palette-item');
        if (!items.length) return;
        let idx = [...items].findIndex(i => i.classList.contains('selected'));
        if (idx === -1) idx = 0;
        else idx = e.key === 'ArrowDown' ? Math.min(items.length - 1, idx + 1) : Math.max(0, idx - 1);
        items.forEach(i => i.classList.remove('selected'));
        items[idx].classList.add('selected');
      }
    });
  }
  document.getElementById('palette-results').addEventListener('click', (e) => {
    const item = e.target.closest('.palette-item');
    if (item) executePaletteItem(item);
  });
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
let _statusTimer = null;
function startStatusPolling() {
  if (_statusTimer) return;
  _statusTimer = setInterval(loadServerStatus, 5000);
}
function stopStatusPolling() {
  if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null; }
}
// Pause background polling when the tab is hidden to avoid wasteful requests.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopStatusPolling();
  else { loadServerStatus(); startStatusPolling(); }
});
loadServerStatus();
startStatusPolling();
