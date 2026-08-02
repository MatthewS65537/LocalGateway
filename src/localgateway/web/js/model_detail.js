// model_detail.js — model detail page logic
const TIER_HUES = [
  { name: 'indigo',  h: 234, base: '#6366f1', cssVar: 'var(--accent)' },
  { name: 'emerald', h: 152, base: '#34d399', cssVar: 'var(--green)' },
  { name: 'amber',   h: 43,  base: '#fbbf24', cssVar: 'var(--yellow)' },
  { name: 'rose',    h: 351, base: '#f87171', cssVar: 'var(--red)' },
  { name: 'slate',   h: 215, base: '#94a3b8', cssVar: 'var(--text-dim)' },
];

function tierColorForTier(tierNum) {
  const idx = Math.max(0, Math.min(tierNum - 1, TIER_HUES.length - 1));
  return TIER_HUES[idx];
}

function shadeForBackend(tierNum, backendIndexInTier, totalInTier) {
  const tc = tierColorForTier(tierNum);
  if (totalInTier <= 1) return tc.base;
  const t = totalInTier === 1 ? 0.5 : backendIndexInTier / (totalInTier - 1);
  const lightness = 64 - t * 12;
  const sat = 64 + t * 22;
  return `hsl(${tc.h}, ${sat}%, ${lightness}%)`;
}

function tierBgForTier(tierNum) {
  const tc = tierColorForTier(tierNum);
  return `hsla(${tc.h}, 70%, 60%, 0.08)`;
}

// Context range for the stat strip: min–max across active (enabled, non-snoozed)
// backends. A model-level context_length override is shown as-is. Falls back to
// all enabled backends when every active one is snoozed.
function computeContextRange(cfgModel, backends) {
  if (cfgModel.context_length) return fmtTokens(cfgModel.context_length);
  const isActive = b => b.enabled && !(b.cooldown_remaining && b.cooldown_remaining !== 0);
  let pool = backends.filter(isActive);
  if (!pool.length) pool = backends.filter(b => b.enabled);
  const ctxs = pool.map(b => b.context_length).filter(v => v);
  if (!ctxs.length) return '—';
  const lo = Math.min(...ctxs);
  const hi = Math.max(...ctxs);
  if (lo === hi) return fmtTokens(hi);
  return fmtTokens(lo) + '–' + fmtTokens(hi);
}

function buildBackendColorMap(backends) {
  const byTier = {};
  backends.forEach(b => {
    const p = b.priority || 1;
    byTier[p] = byTier[p] || [];
    byTier[p].push(b);
  });
  const map = {};
  Object.keys(byTier).forEach(tier => {
    const t = Number(tier);
    const group = byTier[t];
    group.forEach((b, i) => {
      const key = b.provider + ':' + (b.backend_model || b.model);
      map[key] = shadeForBackend(t, i, group.length);
    });
  });
  return map;
}

let modelDetailState = { id: null, hours: 24, p: 'p50', chartHours: 24, chartYMin: null, chartYMax: 2000 };
let detailSeriesCache = null;
let detailSort = { col: 'priority', dir: 1 };
let detailStats = null;
let _detailDragProvider = null, _detailDragModel = null;

function loadModelDetailPage(id) {
  modelDetailState.id = decodeURIComponent(id);
  reloadModelDetail();
  startInflightPolling();
  document.getElementById('detail-rows').onclick = function(ev) {
    const dot = ev.target.closest('.routing-dot');
    if (!dot) return;
    if (dot.classList.contains('snoozed')) {
      showUnsnoozeConfirm(dot.dataset.provider, dot.dataset.model);
    } else {
      showSnoozeModal(dot.dataset.provider, dot.dataset.model);
    }
  };
  initSubnavScrollSpy();
}

function initSubnavScrollSpy() {
  const nav = document.getElementById('detail-subnav');
  if (!nav) return;
  const links = nav.querySelectorAll('a');
  links.forEach(link => {
    link.addEventListener('click', function(e) {
      e.preventDefault();
      const anchor = this.dataset.anchor;
      const target = document.getElementById(anchor);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      links.forEach(l => l.classList.remove('active'));
      this.classList.add('active');
    });
  });
  const sections = ['providers', 'chart'].map(id => document.getElementById(id)).filter(Boolean);
  if (!sections.length) return;
  let scrollTimer = null;
  window.addEventListener('scroll', function() {
    if (scrollTimer) return;
    scrollTimer = setTimeout(() => {
      scrollTimer = null;
      const scrollY = window.scrollY + 120;
      let current = sections[0];
      for (const s of sections) {
        if (s.offsetTop <= scrollY) current = s;
      }
      links.forEach(l => l.classList.toggle('active', l.dataset.anchor === current.id));
    }, 80);
  }, { passive: true });
}

let _inflightTimer = null;
function startInflightPolling() {
  stopInflightPolling();
  _inflightTimer = setInterval(pollInflight, 2000);
  pollInflight();
}
function stopInflightPolling() {
  if (_inflightTimer) { clearInterval(_inflightTimer); _inflightTimer = null; }
}
async function pollInflight() {
  if (!modelDetailState.id || document.hidden) return;
  try {
    const { data } = await fetchJSON('/admin/inflight');
    modelDetailState.inflight = data || {};
    updateInflightIndicators();
  } catch(e) {}
}
function updateInflightIndicators() {
  const inflight = modelDetailState.inflight || {};
  document.querySelectorAll('#detail-rows tr').forEach(tr => {
    const key = tr.dataset.provider + ':' + tr.dataset.model;
    const dot = tr.querySelector('.routing-dot');
    if (!dot) return;
    const active = (inflight[key] || 0) > 0;
    dot.classList.toggle('active', active);
  });
}

// ---------- snooze (shared modal) ----------
function showUnsnoozeConfirm(provider, model) {
  const overlay = openModal({
    title: provider + ':' + model,
    bodyHtml:
      '<p class="modal-sub">This backend is currently snoozed.</p>' +
      '<div class="modal-actions">' +
        '<button class="secondary" data-cancel>Cancel</button>' +
        '<button class="primary" data-ok>Unsnooze</button></div>',
    onMount: (ov) => {
      ov.querySelector('[data-ok]').onclick = () => { closeModal(ov); unsnoozeBackend(provider, model); };
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
    },
  });
}

function showSnoozeModal(provider, model) {
  const presets = [300, 900, 1800, 3600, 7200, 14400, 86400, 604800];
  const overlay = openModal({
    title: 'Snooze — ' + provider + ':' + model,
    bodyHtml:
      '<p class="modal-sub">Temporarily remove this backend from routing. Useful when you hit a plan limit.</p>' +
      '<div class="hstack" style="margin-bottom:14px">' +
        presets.map(s => `<button class="secondary btn-sm" data-s="${s}">${fmtSnoozeDuration(s)}</button>`).join('') +
      '</div>' +
      '<div class="hstack" style="margin-bottom:14px">' +
        '<input type="text" id="snooze-custom" placeholder="e.g. 1d 2h 30m 15s" class="mono" style="flex:1;min-width:160px">' +
        '<button class="secondary" data-custom>Snooze custom</button>' +
      '</div>' +
      '<button class="secondary" data-perm style="width:100%">Snooze until manually removed</button>' +
      '<div class="modal-actions"><button class="secondary" data-cancel>Cancel</button></div>',
    onMount: (ov) => {
      ov.querySelectorAll('[data-s]').forEach(b => b.onclick = () => snoozeBackend(provider, model, parseInt(b.dataset.s, 10), ov));
      ov.querySelector('[data-custom]').onclick = () => snoozeBackend(provider, model, null, ov);
      ov.querySelector('[data-perm]').onclick = () => snoozeBackendPermanent(provider, model, ov);
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
    },
  });
}

function parseDuration(str) {
  if (!str) return null;
  const units = { w: 604800, d: 86400, h: 3600, m: 60, s: 1 };
  const re = /(\d+)\s*([wdhms])/gi;
  let total = 0, matched = false;
  let m;
  while ((m = re.exec(str)) !== null) {
    matched = true;
    total += parseInt(m[1], 10) * units[m[2].toLowerCase()];
  }
  return matched ? total : null;
}

function fmtSnoozeDuration(s) {
  if (s <= 0) return '0s';
  const weeks = Math.floor(s / 604800); s %= 604800;
  const days = Math.floor(s / 86400); s %= 86400;
  const hours = Math.floor(s / 3600); s %= 3600;
  const mins = Math.floor(s / 60); s %= 60;
  const parts = [];
  if (weeks) parts.push(weeks + 'w');
  if (days) parts.push(days + 'd');
  if (hours) parts.push(hours + 'h');
  if (mins) parts.push(mins + 'm');
  if (s) parts.push(Math.round(s) + 's');
  return parts.join(' ') || '0s';
}

function fmtSnoozeDurationRounded(s) {
  if (s < 0) return 'snoozed';
  const weeks = Math.floor(s / 604800); s %= 604800;
  const days = Math.floor(s / 86400); s %= 86400;
  const hours = Math.floor(s / 3600); s %= 3600;
  const mins = Math.round(s / 60);
  const parts = [];
  if (weeks) parts.push(weeks + 'w');
  if (days) parts.push(days + 'd');
  if (hours) parts.push(hours + 'h');
  if (mins) parts.push(mins + 'm');
  return parts.join(' ') || '<1m';
}

async function snoozeBackend(provider, model, seconds, overlay) {
  if (seconds == null) {
    const input = document.getElementById('snooze-custom');
    seconds = parseDuration(input && input.value);
  }
  if (!seconds || seconds < 1) { toast('Enter a valid duration (e.g. 1d 2h 30m)', 'error'); return; }
  try {
    await fetchJSON('/admin/backends/snooze', { method: 'POST', body: JSON.stringify({ provider, model, seconds }) });
    toast('Snoozed for ' + fmtSnoozeDuration(seconds), 'success');
    if (overlay) closeModal(overlay);
    await reloadModelDetail();
  } catch(e) { toast('Failed to snooze: ' + e.message, 'error'); }
}

async function snoozeBackendPermanent(provider, model, overlay) {
  try {
    await fetchJSON('/admin/backends/snooze', { method: 'POST', body: JSON.stringify({ provider, model, permanent: true }) });
    toast('Snoozed until manually removed', 'success');
    if (overlay) closeModal(overlay);
    await reloadModelDetail();
  } catch(e) { toast('Failed to snooze: ' + e.message, 'error'); }
}

async function unsnoozeBackend(provider, model) {
  try {
    await fetchJSON('/admin/backends/unsnooze', { method: 'POST', body: JSON.stringify({ provider, model }) });
    toast('Snooze removed', 'success');
    await reloadModelDetail();
  } catch(e) { toast('Failed to unsnooze: ' + e.message, 'error'); }
}

function setDetailRange(hours, btn) {
  modelDetailState.hours = hours;
  document.querySelectorAll('#detail-range button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  reloadModelDetail();
}

async function reloadModelDetail() {
  const id = modelDetailState.id;
  if (!id) return;

  if (!currentConfig) {
    try { currentConfig = (await fetchJSON('/admin/config')).data; } catch(e) { console.error(e); }
  }

  const p = document.getElementById('detail-p').value;
  modelDetailState.p = p;
  const cfgModel = (currentConfig && currentConfig.models || []).find(m => m.id === id) || {};
  const disabled = cfgModel.enabled === false;

  // ---- Hero header ----
  const avatarEl = document.getElementById('detail-avatar');
  if (avatarEl) {
    avatarEl.innerHTML = modelAvatar(id, cfgModel.display_name, 48, cfgModel.avatar);
    avatarEl.style.cursor = 'pointer';
    avatarEl.title = 'Click to edit icon';
    avatarEl.onclick = () => editModelAvatar();
  }
  document.getElementById('detail-name').innerHTML =
    '<span>' + esc(cfgModel.display_name || id) + '</span>'
    + (disabled ? ' <span class="badge badge-gray">disabled</span>' : '');
  document.getElementById('detail-slug-row').innerHTML =
    '<code>' + esc(id) + '</code>'
    + '<button class="copy-btn" data-id="'+escAttr(id)+'" onclick="copyId(this.dataset.id,this)" title="Copy model ID">⧉</button>'
    + '<span class="filter-hint">· '+((cfgModel.backends || []).filter(b=>b.enabled!==false).length)+' backends</span>';
  document.getElementById('detail-desc').textContent = cfgModel.description || '';

  // Update metadata display (drawer)
  const setMeta = (field, value, elId) => {
    const row = document.querySelector('#metadata-drawer .meta-row[data-field="' + field + '"]');
    const el = document.getElementById(elId);
    if (!el) return;
    const hasValue = value && value !== '—' && value !== '';
    el.textContent = hasValue ? value : '';
    if (row) row.style.display = hasValue ? '' : 'none';
  };
  setMeta('display_name', cfgModel.display_name, 'meta-display-name');
  setMeta('description', cfgModel.description, 'meta-description');
  setMeta('slug', id, 'meta-slug');
  setMeta('modality', cfgModel.modality, 'meta-modality');
  setMeta('max_output', cfgModel.max_output_tokens ? fmtTokens(cfgModel.max_output_tokens) : null, 'meta-max-output');
  setMeta('tags', (cfgModel.tags || []).join(', '), 'meta-tags');
  setMeta('aliases', (cfgModel.aliases || []).join(', '), 'meta-aliases');
  const dp = cfgModel.default_params || {};
  setMeta('default_params', Object.keys(dp).length ? JSON.stringify(dp) : null, 'meta-default-params');
  const caps = cfgModel.capabilities || {};
  const enabledCaps = Object.entries(caps).filter(([k, v]) => v).map(([k]) => k).join(', ');
  setMeta('capabilities', enabledCaps, 'meta-capabilities');

  // Drawer title
  const drawerTitle = document.getElementById('drawer-model-title');
  if (drawerTitle) drawerTitle.textContent = cfgModel.display_name || id;

  // ---- Stat strip ----
  const chartEnabled = currentConfig && currentConfig.server && currentConfig.server.chart_enabled !== false;
  const chartSection = document.getElementById('chart');
  if (chartSection) chartSection.classList.toggle('hidden', !chartEnabled);
  try {
    const fetches = [
      fetchJSON('/admin/models/'+encodeURIComponent(id)+'/stats?hours='+modelDetailState.hours+'&p='+p),
    ];
    if (chartEnabled) {
      fetches.push(fetchJSON('/admin/models/'+encodeURIComponent(id)+'/series?hours='+modelDetailState.chartHours));
    }
    const [statsR, seriesR] = await Promise.all(fetches);
    detailStats = statsR.data;
    detailSeriesCache = seriesR ? (seriesR.data || {}) : null;
    renderDetailTable(statsR.data.backends || []);
    if (chartEnabled) renderTpsChart(detailSeriesCache);
    const bs = statsR.data.backends || [];
    const priced = bs.filter(b => b.input_price || b.output_price);
    const priceStr = priced.length ? fmtPricePrecise(Math.min(...priced.map(b=>b.input_price)))+' / '+fmtPricePrecise(Math.min(...priced.map(b=>b.output_price))) : '—';
    const p50Vals = bs.map(b => b.tps_p50).filter(v => v != null);
    const bestP50 = p50Vals.length ? fmt(Math.max(...p50Vals), 0) : '—';
    const ctxStr = computeContextRange(cfgModel, bs);
    const successVals = bs.map(b => b.success_rate).filter(v => v != null);
    const uptimeStr = successVals.length ? Math.min(...successVals).toFixed(1)+'%' : '—';
    const totals = statsR.data.totals || {};
    const tokensStr = totals.tokens != null ? fmtTokens(totals.tokens) : '—';
    const spendStr = totals.cost != null ? '$' + totals.cost.toFixed(4) : '—';
    renderDetailStatStrip(cfgModel, priceStr, ctxStr, bestP50, uptimeStr, tokensStr, spendStr);
  } catch(e) {
    document.getElementById('detail-rows').innerHTML = '<tr><td colspan="14" class="empty">Gateway is stopped — start it to see live stats.</td></tr>';
    document.getElementById('detail-chart').innerHTML = '<div class="empty">No data.</div>';
    renderDetailStatStrip(cfgModel, '—', '—', '—', '—', '—', '—');
  }
}

function renderDetailStatStrip(cfgModel, priceStr, ctxStr, bestP50, uptimeStr, tokensStr, spendStr) {
  const bar1 = document.getElementById('detail-stat-strip');
  const bar2 = document.getElementById('detail-stat-strip-2');
  const cell = (cap, num, accent, sub) =>
    '<div class="detail-stat-cell">'
    + '<div class="cap">' + cap + '</div>'
    + '<div class="num' + (accent ? ' accent' : '') + '">' + num + '</div>'
    + (sub ? '<div class="sub">' + sub + '</div>' : '')
    + '</div>';
  if (bar1) bar1.innerHTML =
    cell('Modality', cfgModel.modality || '—') +
    cell('In / Out', priceStr, false, 'per 1M tokens') +
    cell('Context', ctxStr);
  if (bar2) bar2.innerHTML =
    cell('24h Spend', spendStr, false, 'estimated') +
    cell('24h Tokens', tokensStr, false, 'total') +
    cell('Best TPS P50', bestP50, true, 'tokens/sec') +
    cell('Uptime', uptimeStr);
}

// ---------- metadata drawer ----------
function _setChevron(dir) {
  const path = document.querySelector('#right-edge-toggle .chevron path');
  if (path) path.setAttribute('d', dir === 'left' ? 'M10 6l6 6-6 6' : 'M14 6l-6 6 6 6');
}
function showMetadataDrawer() {
  const drawer = document.getElementById('metadata-drawer');
  const backdrop = document.getElementById('metadata-drawer-backdrop');
  if (!drawer || !backdrop) return;
  drawer.classList.remove('hidden');
  backdrop.classList.remove('hidden');
  document.body.classList.add('metadata-open');
  _setChevron('left');
  requestAnimationFrame(() => {
    drawer.classList.add('open');
    backdrop.classList.add('visible');
  });
  document.addEventListener('keydown', _drawerEscHandler);
}
function hideMetadataDrawer() {
  const drawer = document.getElementById('metadata-drawer');
  const backdrop = document.getElementById('metadata-drawer-backdrop');
  if (!drawer) return;
  drawer.classList.remove('open');
  backdrop.classList.remove('visible');
  document.body.classList.remove('metadata-open');
  _setChevron('right');
  setTimeout(() => { drawer.classList.add('hidden'); backdrop.classList.add('hidden'); }, 220);
  document.removeEventListener('keydown', _drawerEscHandler);
}
function _drawerEscHandler(e) { if (e.key === 'Escape') hideMetadataDrawer(); }
function toggleMetadataDrawer() {
  if (document.getElementById('metadata-drawer')?.classList.contains('open')) hideMetadataDrawer();
  else showMetadataDrawer();
}

function editModelMetadata() {
  const id = modelDetailState.id;
  if (!id) return;
  const cfgModel = (currentConfig && currentConfig.models || []).find(m => m.id === id) || {};
  const caps = cfgModel.capabilities || {};
  const capItem = (key, label) =>
    '<label class="cap-item"><input type="checkbox" id="cap-' + key + '" ' + (caps[key] ? 'checked' : '') + '> ' + label + '</label>';

  const bodyHtml =
    '<div class="field-group">' +
      '<div class="field"><label class="field-label" for="edit-display-name">Display Name</label>' +
        '<input type="text" id="edit-display-name" value="' + esc(cfgModel.display_name || '') + '"></div>' +
      '<div class="field"><label class="field-label" for="edit-slug">Model Slug</label>' +
        '<input type="text" id="edit-slug" class="mono" value="' + esc(id) + '">' +
        '<div class="field-hint">Changing the slug migrates all usage data to the new ID.</div></div>' +
      '<div class="field"><label class="field-label" for="edit-description">Description</label>' +
        '<input type="text" id="edit-description" value="' + esc(cfgModel.description || '') + '" placeholder="Optional description"></div>' +
      '<div class="field"><label class="field-label" for="edit-modality">Modality</label>' +
        '<select id="edit-modality">' +
          '<option value=""' + (!cfgModel.modality ? ' selected' : '') + '>None</option>' +
          '<option value="text"' + (cfgModel.modality === 'text' ? ' selected' : '') + '>Text</option>' +
          '<option value="text+vision"' + (cfgModel.modality === 'text+vision' ? ' selected' : '') + '>Text + Vision</option>' +
          '<option value="multimodal"' + (cfgModel.modality === 'multimodal' ? ' selected' : '') + '>Multimodal</option>' +
        '</select></div>' +
      '<div class="field"><label class="field-label" for="edit-max-output">Max Output Tokens</label>' +
        '<input type="number" id="edit-max-output" value="' + (cfgModel.max_output_tokens || '') + '"></div>' +
      '<div class="field"><label class="field-label" for="edit-tags">Tags (comma-separated)</label>' +
        '<input type="text" id="edit-tags" value="' + esc((cfgModel.tags || []).join(',')) + '" placeholder="fast,cheap,smart"></div>' +
      '<div class="field"><label class="field-label" for="edit-aliases">Aliases (comma-separated)</label>' +
        '<input type="text" id="edit-aliases" value="' + esc((cfgModel.aliases || []).join(',')) + '" placeholder="gpt-4,gpt4"></div>' +
      '<div class="field"><label class="field-label" for="edit-default-params">Default Params (JSON)</label>' +
        '<textarea id="edit-default-params" rows="3" placeholder=\'{"temperature":0.7,"max_tokens":2048}\' style="font-family:var(--font-mono);font-size:0.82rem">' +
        esc(Object.keys(cfgModel.default_params || {}).length ? JSON.stringify(cfgModel.default_params, null, 2) : '') +
        '</textarea></div>' +
      '<div class="field"><label class="field-label">Capabilities</label>' +
        '<div class="cap-grid">' +
          capItem('text', 'Text') +
          capItem('vision', 'Vision') +
          capItem('audio', 'Audio') +
          capItem('tools', 'Tools') +
          capItem('json_mode', 'JSON Mode') +
          capItem('parallel_tool_calls', 'Parallel Tools') +
          capItem('streaming', 'Streaming') +
        '</div></div>' +
    '</div>' +
    '<div class="modal-actions">' +
      '<button class="secondary" data-cancel>Cancel</button>' +
      '<button class="primary" data-ok>Save</button></div>';

  const overlay = openModal({
    title: 'Edit Metadata — ' + id,
    widthClass: 'modal-lg',
    bodyHtml,
    onMount: (ov) => {
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
      ov.querySelector('[data-ok]').onclick = () => saveModelMetadata(ov);
    },
  });
}

async function saveModelMetadata(overlay) {
  const id = modelDetailState.id;
  if (!id) return;

  const capabilities = {};
  ['text', 'vision', 'audio', 'tools', 'json_mode', 'parallel_tool_calls', 'streaming'].forEach(cap => {
    capabilities[cap] = document.getElementById('cap-' + cap).checked;
  });

  const tagsStr = document.getElementById('edit-tags').value;
  const tags = tagsStr ? tagsStr.split(',').map(t => t.trim()).filter(t => t) : [];

  const aliasesStr = document.getElementById('edit-aliases').value;
  const aliases = aliasesStr ? aliasesStr.split(',').map(a => a.trim()).filter(a => a) : [];

  let default_params = {};
  const dpRaw = document.getElementById('edit-default-params').value.trim();
  if (dpRaw) {
    try {
      default_params = JSON.parse(dpRaw);
      if (typeof default_params !== 'object' || Array.isArray(default_params)) {
        toast('Default params must be a JSON object', 'error');
        return;
      }
    } catch (e) {
      toast('Invalid default params JSON', 'error');
      return;
    }
  }

  const maxOutput = document.getElementById('edit-max-output').value;
  const newSlug = document.getElementById('edit-slug').value.trim();

  const body = {
    display_name: document.getElementById('edit-display-name').value,
    description: document.getElementById('edit-description').value,
    modality: document.getElementById('edit-modality').value,
    max_output_tokens: maxOutput ? parseInt(maxOutput) : null,
    tags,
    aliases,
    default_params,
    capabilities,
  };

  try {
    let targetId = id;

    // Handle slug rename if changed
    if (newSlug && newSlug !== id) {
      const r = await fetch('/admin/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ type: 'model', old_id: id, new_id: newSlug }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: 'Failed to rename slug' }));
        toast(err.error || 'Failed to rename slug', 'error');
        return;
      }
      targetId = newSlug;
    }

    // Save metadata fields
    const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(targetId), { method: 'PUT', body: JSON.stringify(body) });
    if (ok) {
      toast('Model metadata updated', 'success');
      if (overlay) closeModal(overlay);
      currentConfig = (await fetchJSON('/admin/config')).data;
      if (targetId !== id) {
        location.href = '/models/' + encodeURIComponent(targetId);
      } else {
        await reloadModelDetail();
      }
    } else {
      toast('Failed to update metadata', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

// ---------- model avatar editor ----------
function editModelAvatar() {
  const id = modelDetailState.id;
  if (!id) return;
  const cfgModel = (currentConfig && currentConfig.models || []).find(m => m.id === id) || {};
  const current = cfgModel.avatar || '';
  const previewId = 'avatar-preview';
  const overlay = openModal({
    title: 'Edit Model Icon',
    bodyHtml:
      '<p class="modal-sub">Custom text to show in this model\'s avatar tile. Leave blank to use the first letter of the display name / ID. Longer text auto-shrinks to fit.</p>' +
      '<div class="hstack" style="margin-bottom:14px;justify-content:flex-start">' +
        '<div id="'+previewId+'"></div>' +
        '<input type="text" id="avatar-text-input" maxlength="12" value="'+esc(current)+'" placeholder="e.g. GPT-4o, Claude, 4o-mini" style="flex:1">' +
      '</div>' +
      '<div class="modal-actions">' +
        '<button class="secondary" data-reset>Reset to default</button>' +
        '<button class="secondary" data-cancel>Cancel</button>' +
        '<button class="primary" data-ok>Save</button></div>',
    onMount: (ov) => {
      const renderPreview = () => {
        const v = (ov.querySelector('#avatar-text-input') || {}).value || '';
        ov.querySelector('#'+previewId).innerHTML = modelAvatar(id, cfgModel.display_name, 48, v);
      };
      renderPreview();
      ov.querySelector('#avatar-text-input').addEventListener('input', renderPreview);
      ov.querySelector('[data-reset]').onclick = () => { ov.querySelector('#avatar-text-input').value = ''; renderPreview(); };
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
      ov.querySelector('[data-ok]').onclick = async () => {
        const v = ov.querySelector('#avatar-text-input').value;
        try {
          const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(id), { method: 'PUT', body: JSON.stringify({ avatar: v }) });
          if (ok) {
            toast('Model icon updated', 'success');
            closeModal(ov);
            currentConfig = (await fetchJSON('/admin/config')).data;
            await reloadModelDetail();
          } else {
            toast('Failed to update icon', 'error');
          }
        } catch(e) { toast('Error: ' + e.message, 'error'); }
      };
    },
  });
}

async function probeModelNow() {
  const id = modelDetailState.id;
  if (!id) return;
  const btn = document.getElementById('probe-now-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Probing…'; }
  const cfgModel = (currentConfig && currentConfig.models || []).find(m => m.id === id);
  if (!cfgModel) { if (btn) { btn.disabled = false; btn.textContent = 'Probe now'; } return; }

  const snoozedKeys = new Set();
  if (detailStats && detailStats.backends) {
    for (const b of detailStats.backends) {
      if (b.cooldown_remaining && b.cooldown_remaining !== 0) {
        snoozedKeys.add(b.provider + ':' + (b.backend_model || b.model));
      }
    }
  }

  const results = [];
  for (const b of cfgModel.backends) {
    if (!b.enabled) { results.push({provider: b.provider, model: b.model, priority: b.priority, ok: false, skipped: true}); continue; }
    if (snoozedKeys.has(b.provider + ':' + b.model)) { results.push({provider: b.provider, model: b.model, priority: b.priority, ok: false, skipped: true, error: 'snoozed'}); continue; }
    try {
      const r = await fetch('/admin/backends/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider: b.provider, model: b.model, stream: true, logical_model: id}),
      });
      const d = r.ok ? await r.json() : {};
      results.push({
        provider: b.provider,
        model: b.model,
        priority: b.priority,
        ok: d.ok || false,
        skipped: d.skipped || false,
        ttft_ms: d.ttft_ms,
        latency_ms: d.latency_ms,
        tps: d.tps,
        output_tokens: d.output_tokens,
        error: d.error,
      });
    } catch(e) {
      results.push({provider: b.provider, model: b.model, priority: b.priority, ok: false, error: e.message});
    }
  }

  if (btn) { btn.disabled = false; btn.textContent = 'Probe now'; }

  const okCount = results.filter(r => r.ok).length;
  const skipCount = results.filter(r => r.skipped).length;
  const failCount = results.length - okCount - skipCount;
  toast(`Probed ${okCount} ok, ${failCount} failed${skipCount ? ', '+skipCount+' snoozed' : ''}`, okCount > 0 ? 'success' : 'error');

  const rows = results.map(r => {
    const tierNum = r.priority || 1;
    const tc = tierColorForTier(tierNum);
    if (r.skipped) {
      const reason = r.error === 'snoozed' ? 'snoozed' : 'disabled';
      return '<tr class="probe-skipped"><td class="tier-num" style="--tc:'+tc.cssVar+'">'+tierNum+'</td><td><code>'+esc(r.provider)+'</code>:<code>'+esc(r.model)+'</code></td><td colspan="3" class="muted">'+reason+'</td></tr>';
    }
    if (!r.ok) {
      return '<tr class="probe-fail"><td class="tier-num" style="--tc:'+tc.cssVar+'">'+tierNum+'</td><td><code>'+esc(r.provider)+'</code>:<code>'+esc(r.model)+'</code></td><td colspan="3" style="color:var(--danger);font-weight:600">ERROR</td></tr>';
    }
    const tps = r.tps != null ? r.tps.toFixed(1) : '—';
    return '<tr>'
      + '<td class="tier-num" style="--tc:'+tc.cssVar+'">'+tierNum+'</td>'
      + '<td><code>'+esc(r.provider)+'</code>:<code>'+esc(r.model)+'</code></td>'
      + '<td>'+(r.ttft_ms != null ? r.ttft_ms+'ms' : '—')+'</td>'
      + '<td>'+(r.latency_ms != null ? (r.latency_ms/1000).toFixed(2)+'s' : '—')+'</td>'
      + '<td class="td-shade" style="font-weight:600;--tc:'+tc.base+'">'+tps+' tps</td>'
      + '</tr>';
  }).join('');

  openModal({
    title: 'Probe Report — ' + id,
    widthClass: 'modal-lg',
    bodyHtml:
      '<p class="modal-sub">Live probe results ('+results.filter(r=>r.ok).length+'/'+results.length+' ok).</p>' +
      '<table class="probe-table">' +
        '<thead><tr><th>Tier</th><th>Provider</th><th>TTFT</th><th>Latency</th><th>Throughput</th></tr></thead>' +
        '<tbody>'+rows+'</tbody></table>' +
      '<div class="modal-actions"><button class="primary" data-ok>Close</button></div>',
    onMount: (ov) => { ov.querySelector('[data-ok]').onclick = () => closeModal(ov); },
  });
}

function sortDetail(col) {
  if (detailSort.col === col) detailSort.dir *= -1;
  else { detailSort.col = col; detailSort.dir = 1; }
  if (detailStats) renderDetailTable(detailStats.backends || []);
}

function renderDetailTable(backends) {
  const { col, dir } = detailSort;
  const rows = backends.slice().sort((a, b) => {
    let va = a[col], vb = b[col];
    if (col === 'backend') { va = a.provider + a.backend_model; vb = b.provider + b.backend_model; }
    if (va == null) va = col === 'backend' ? '' : -Infinity;
    if (vb == null) vb = col === 'backend' ? '' : -Infinity;
    if (typeof va === 'string') return va.localeCompare(vb) * dir;
    return (va - vb) * dir;
  });
  document.querySelectorAll('#detail-table th').forEach(th => {
    th.classList.add('th-sort');
    const arrow = th.dataset.col === col ? (dir === 1 ? ' ↑' : ' ↓') : '';
    th.textContent = th.textContent.replace(/ [↑↓]$/, '') + arrow;
  });
  const el = document.getElementById('detail-rows');
  if (!rows.length) { el.innerHTML = '<tr><td colspan="14" class="empty">No providers configured.</td></tr>'; return; }

  const tierGroups = {};
  rows.forEach(b => {
    const p = b.priority || 1;
    tierGroups[p] = tierGroups[p] || [];
    tierGroups[p].push(b);
  });
  const tierKeys = Object.keys(tierGroups).map(Number).sort((a, b) => a - b);
  const colorMap = buildBackendColorMap(rows);

  el.innerHTML = rows.map(b => {
    const off = !b.enabled;
    const cooldown = b.cooldown_remaining || 0;
    const isSnoozed = cooldown !== 0;
    const isPermanent = cooldown < 0;
    const snoozeBadge = isSnoozed
      ? (isPermanent
          ? '<span class="badge badge-gray">(snoozed)</span>'
          : '<span class="badge badge-gray">(snoozed '+fmtSnoozeDurationRounded(cooldown)+')</span>')
      : '';
    const badge = off ? '<span class="badge badge-gray">off</span>' : snoozeBadge;
    const up = b.success_rate != null
      ? '<span class="badge '+(b.success_rate >= 95 ? 'badge-green' : b.success_rate >= 80 ? 'badge-yellow' : 'badge-red')+'">'+b.success_rate+'%</span>'
      : '—';

    const tierNum = b.priority || 1;
    const tc = tierColorForTier(tierNum);
    const shade = colorMap[b.provider + ':' + b.backend_model] || tc.base;
    const rowBg = (b.enabled && !isSnoozed) ? tierBgForTier(tierNum) : 'var(--surface)';
    const rowOpacity = (off || isSnoozed) ? 'opacity:0.5;' : '';

    return '<tr data-tier-row draggable="true" data-provider="'+escAttr(b.provider)+'" data-model="'+escAttr(b.backend_model)+'" data-priority="'+tierNum+'"'
      + ' style="--tc:'+tc.cssVar+';--shade:'+shade+';--tcbg:'+rowBg+';'+rowOpacity+'"'
      + ' ondragstart="detailRowDragStart(event)" ondragover="detailRowDragOver(event)" ondrop="detailRowDrop(event)" ondragend="detailRowDragEnd(event)">'
      + '<td class="grab-cell" title="Drag to reorder">⋮⋮</td>'
      + '<td class="tier-num">'+tierNum+'</td>'
      + '<td><span class="routing-dot '+(isSnoozed?'snoozed':'')+'" data-provider="'+escAttr(b.provider)+'" data-model="'+escAttr(b.backend_model)+'" title="'+(isSnoozed?'Click to manage snooze':'Click to snooze')+'"></span>'
        + providerAvatar(b.provider, 20, (b.provider_avatar || ''))
        + '<code style="color:var(--shade)">'+esc(b.provider)+'</code>:<code>'+esc(b.backend_model)+'</code> '+badge
        + ' <button class="icon-btn btn-sm" data-provider="'+escAttr(b.provider)+'" data-model="'+escAttr(b.backend_model)+'" onclick="editProviderPricing(this.dataset.provider, this.dataset.model)" title="Edit pricing">$</button></td>'
      + '<td>'+(b.context_length ? fmtTokens(b.context_length) : '—')+'</td>'
      + '<td>'+(b.max_output_tokens ? fmtTokens(b.max_output_tokens) : '—')+'</td>'
      + '<td>'+fmtPricePrecise(b.input_price)+'</td>'
      + '<td>'+fmtPricePrecise(b.output_price)+'</td>'
      + '<td class="td-dim">'+(b.cache_read_price != null || b.cache_write_price != null ? fmtPricePrecise(b.cache_read_price)+' / '+fmtPricePrecise(b.cache_write_price) : '— / —')+'</td>'
      + '<td>'+(b.ttft_ms != null ? fmt(b.ttft_ms, 0)+'ms' : '—')+'</td>'
      + '<td class="td-shade">'+(b.tps != null ? fmt(b.tps, 0)+' tps' : '—')+'</td>'
      + '<td>'+(b.latency_ms != null ? fmt(b.latency_ms/1000, 2)+'s' : '—')+'</td>'
      + '<td>'+up+'</td>'
      + '<td>'+fmt(b.requests, 0)+'</td>'
      + '<td style="padding:4px 8px"><button class="icon-btn btn-sm" data-provider="'+escAttr(b.provider)+'" data-model="'+escAttr(b.backend_model)+'" onclick="removeBackendFromModel(this.dataset.provider, this.dataset.model)" title="Remove provider">×</button></td>'
      + '</tr>';
  }).join('');
}

// ---------- pricing editor (shared modal) ----------
function editProviderPricing(provider, backendModel) {
  const pricing = (currentConfig && currentConfig.pricing && currentConfig.pricing[provider + ':' + backendModel]) || {};
  const fld = (id, label, val, placeholder) =>
    '<div class="field"><label class="field-label" for="'+id+'">'+label+'</label>' +
    '<input type="number" id="'+id+'" step="0.0001" min="0" value="'+(val != null ? val : '')+'" placeholder="'+placeholder+'"></div>';
  const overlay = openModal({
    title: 'Pricing — ' + provider + ':' + backendModel,
    bodyHtml:
      '<p class="modal-sub">All prices per 1M tokens.</p>' +
      fld('px-input', 'Input ($/M)', pricing.input != null ? (pricing.input * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : '', 'same as input') +
      fld('px-output', 'Output ($/M)', pricing.output != null ? (pricing.output * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : '', 'same as input') +
      fld('px-cache-read', 'Cache read ($/M)', pricing.cache_read != null ? (pricing.cache_read * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : '', 'same as input') +
      fld('px-cache-write', 'Cache write ($/M)', pricing.cache_write != null ? (pricing.cache_write * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : '', 'same as input') +
      '<div class="modal-actions">' +
        '<button class="secondary" data-cancel>Cancel</button>' +
        '<button class="primary" data-ok>Save</button></div>',
    onMount: (ov) => {
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
      ov.querySelector('[data-ok]').onclick = () => saveProviderPricing(provider, backendModel, ov);
    },
  });
}

async function saveProviderPricing(provider, backendModel, overlay) {
  const get = id => { const v = document.getElementById(id).value; return v === '' ? null : parseFloat(v) / 1e6; };
  const key = provider + ':' + backendModel;
  currentConfig.pricing = currentConfig.pricing || {};
  currentConfig.pricing[key] = {
    input: get('px-input'),
    output: get('px-output'),
    cache_read: get('px-cache-read'),
    cache_write: get('px-cache-write'),
  };
  try {
    const r = await fetch('/admin/config', { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(currentConfig) });
    if (r.ok) {
      toast('Pricing saved', 'success');
      if (overlay) closeModal(overlay);
      await reloadModelDetail();
    } else {
      toast('Failed to save pricing', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

function detailRowDragStart(ev) {
  _detailDragProvider = ev.currentTarget.dataset.provider;
  _detailDragModel = ev.currentTarget.dataset.model;
  ev.dataTransfer.effectAllowed = 'move';
  ev.currentTarget.style.opacity = '0.5';
}
function detailRowDragOver(ev) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
}
function detailRowDrop(ev) {
  ev.preventDefault();
  const targetProvider = ev.currentTarget.dataset.provider;
  const targetModel = ev.currentTarget.dataset.model;
  const targetPriority = parseInt(ev.currentTarget.dataset.priority, 10);

  if (!_detailDragProvider || !_detailDragModel) return;
  if (_detailDragProvider === targetProvider && _detailDragModel === targetModel) return;

  const cfgModel = currentConfig && currentConfig.models.find(m => m.id === modelDetailState.id);
  if (!cfgModel) return;

  const draggedBackend = cfgModel.backends.find(b => b.provider === _detailDragProvider && b.model === _detailDragModel);
  if (!draggedBackend) return;

  draggedBackend.priority = targetPriority;
  saveTierChanges();
}
function detailRowDragEnd(ev) {
  ev.currentTarget.style.opacity = '1';
  _detailDragProvider = null;
  _detailDragModel = null;
}

function setChartHours(v) {
  const h = parseInt(v, 10);
  if (!h || h < 1) return;
  modelDetailState.chartHours = h;
  reloadModelDetail();
}
function setChartYRange() {
  const minEl = document.getElementById('chart-ymin');
  const maxEl = document.getElementById('chart-ymax');
  modelDetailState.chartYMin = minEl && minEl.value !== '' ? parseFloat(minEl.value) : null;
  modelDetailState.chartYMax = maxEl && maxEl.value !== '' ? parseFloat(maxEl.value) : null;
  if (detailSeriesCache) renderTpsChart(detailSeriesCache);
}
function resetChartControls() {
  modelDetailState.chartHours = 24;
  modelDetailState.chartYMin = null;
  modelDetailState.chartYMax = 2000;
  const hEl = document.getElementById('chart-hours');
  const minEl = document.getElementById('chart-ymin');
  const maxEl = document.getElementById('chart-ymax');
  if (hEl) hEl.value = 24;
  if (minEl) minEl.value = '';
  if (maxEl) maxEl.value = 2000;
  reloadModelDetail();
}

function renderTpsChart(data) {
  const el = document.getElementById('detail-chart');
  const entries = Object.entries(data.series || {});
  if (!entries.length) { el.innerHTML = '<div class="empty">No throughput data in this window yet.</div>'; return; }
  const W = 780, H = 210, PL = 46, PR = 12, PT = 12, PB = 26;
  const allT = [], allY = [];
  entries.forEach(([, pts]) => pts.forEach(p => { allT.push(p.t); allY.push(p.tps); }));
  const t0 = Math.min(...allT), t1 = Math.max(...allT);
  const yMin = modelDetailState.chartYMin != null ? modelDetailState.chartYMin : 0;
  const yMax = modelDetailState.chartYMax != null ? modelDetailState.chartYMax : Math.min(Math.max(...allY) * 1.15 || 1, 2000);
  const yRange = yMax - yMin || 1;
  const x = t => PL + (t1 === t0 ? 0.5 : (t - t0) / (t1 - t0)) * (W - PL - PR);
  const y = v => PT + (1 - (Math.max(yMin, Math.min(v, yMax)) - yMin) / yRange) * (H - PT - PB);
  const cfgBackends = (currentConfig && currentConfig.models || []).find(m => m.id === modelDetailState.id);
  const colorMap = cfgBackends ? buildBackendColorMap(cfgBackends.backends.map(b => ({ provider: b.provider, backend_model: b.model, priority: b.priority }))) : {};
  let paths = '', legend = '', grid = '';
  for (let g = 0; g <= 4; g++) {
    const v = yMin + yRange * g / 4, yy = y(v).toFixed(1);
    grid += '<line x1="'+PL+'" y1="'+yy+'" x2="'+(W-PR)+'" y2="'+yy+'" stroke="var(--border)" stroke-width="1"/>'
      + '<text x="'+(PL-7)+'" y="'+(+yy+3)+'" text-anchor="end" font-size="9" fill="var(--text-dim)">'+Math.round(v)+'</text>';
  }
  const fmtTick = t => { const d = new Date(t * 1000); return (d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':00'; };
  [t0, (t0+t1)/2, t1].forEach(t => {
    if (t1 > t0) paths += '<text x="'+x(t).toFixed(1)+'" y="'+(H-8)+'" text-anchor="middle" font-size="9" fill="var(--text-dim)">'+fmtTick(t)+'</text>';
  });
  entries.forEach(([k, pts], i) => {
    const c = colorMap[k] || tierColorForTier(i + 1).base;
    pts = pts.slice().sort((a, b) => a.t - b.t);
    const d = pts.map((p, j) => (j ? 'L' : 'M') + x(p.t).toFixed(1) + ' ' + y(p.tps).toFixed(1)).join(' ');
    paths += '<path d="'+d+'" fill="none" stroke="'+c+'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
    pts.forEach(p => { paths += '<circle cx="'+x(p.t).toFixed(1)+'" cy="'+y(p.tps).toFixed(1)+'" r="2.6" fill="'+c+'"><title>'+esc(k)+' · '+p.tps+' tps</title></circle>'; });
    legend += '<span class="lg-item"><span class="lg-swatch" style="background:'+c+'"></span><code>'+esc(k)+'</code></span>';
  });
  el.innerHTML = '<div class="chart-wrap"><svg viewBox="0 0 '+W+' '+H+'">'+grid+paths+'</svg></div><div class="chart-legend">'+legend+'</div>';
}

function addNewTier() {
  const cfgModel = currentConfig && currentConfig.models.find(m => m.id === modelDetailState.id);
  if (!cfgModel) return;
  const maxTier = Math.max(0, ...cfgModel.backends.map(b => b.priority || 1));
  const newTier = maxTier + 1;
  const tc = tierColorForTier(newTier);
  const el = document.getElementById('detail-rows');
  const tr = document.createElement('tr');
  tr.dataset.dropTier = String(newTier);
  tr.style.background = tierBgForTier(newTier);
  tr.style.border = '2px dashed ' + tc.base;
  tr.style.cursor = 'grab';
  tr.ondragover = function(ev) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'move'; this.style.opacity = '0.6'; };
  tr.ondragleave = function(ev) { this.style.opacity = '1'; };
  tr.ondrop = function(ev) {
    ev.preventDefault();
    this.style.opacity = '1';
    if (!_detailDragProvider || !_detailDragModel) return;
    const cm = currentConfig && currentConfig.models.find(m => m.id === modelDetailState.id);
    if (!cm) return;
    const draggedBackend = cm.backends.find(b => b.provider === _detailDragProvider && b.model === _detailDragModel);
    if (!draggedBackend) return;
    draggedBackend.priority = newTier;
    saveTierChanges();
  };
  tr.innerHTML = '<td colspan="14" class="muted" style="text-align:center;padding:14px;color:'+tc.cssVar+';font-size:0.82rem">Drop a provider here to create Tier '+newTier+'</td>';
  el.appendChild(tr);
  toast('New tier '+newTier+' ready — drag a provider into it.', 'info');
}

async function saveTierChanges() {
  const cfgModel = currentConfig && currentConfig.models.find(m => m.id === modelDetailState.id);
  if (!cfgModel) { toast('Model not found in config', 'error'); return; }
  const tiers = {};
  cfgModel.backends.forEach((b, i) => {
    const p = b.priority || 1;
    tiers[p] = tiers[p] || [];
    tiers[p].push(i);
  });
  const tierList = Object.keys(tiers).map(Number).sort((a, b) => a - b).map(p => tiers[p]);
  try {
    const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(modelDetailState.id) + '/backends/tiers', {
      method: 'PUT',
      body: JSON.stringify({ tiers: tierList })
    });
    if (ok) {
      toast('Tier layout saved', 'success');
      reloadModelDetail();
    } else {
      toast('Failed to save tier layout', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function removeBackendFromModel(provider, backendModel) {
  const cfgModel = currentConfig && currentConfig.models.find(m => m.id === modelDetailState.id);
  if (!cfgModel) return;
  const idx = cfgModel.backends.findIndex(b => b.provider === provider && b.model === backendModel);
  if (idx < 0) return;
  const confirmed = await showConfirm('Remove Provider', 'Remove ' + provider + ':' + backendModel + ' from this model?');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/models/' + encodeURIComponent(modelDetailState.id) + '/backends/' + idx, { method: 'DELETE' });
    if (r.ok) {
      toast('Provider removed', 'success');
      reloadModelDetail();
    } else {
      toast('Failed to remove provider', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

let _addBackendModels = [];

function showAddBackendModal() {
  const modelId = modelDetailState.id;
  if (!modelId) { toast('No model selected', 'error'); return; }
  const providers = (currentConfig && currentConfig.providers || []).filter(p => p.enabled);
  const overlay = openModal({
    title: 'Add Provider to Model',
    bodyHtml:
      '<div class="field"><label class="field-label" for="new-backend-provider">Provider *</label>' +
        '<select id="new-backend-provider" onchange="loadProviderModelSuggestions()"><option value="">Select provider</option>' +
        providers.map(p => '<option value="'+esc(p.id)+'">'+esc(p.name || p.id)+'</option>').join('') + '</select></div>' +
      '<div class="field"><label class="field-label" for="new-backend-model">Backend Model *</label>' +
        '<input type="text" id="new-backend-model" placeholder="Pick from list or type a custom model ID" oninput="filterProviderModelSuggestions()">' +
        '<div id="new-backend-model-status" class="filter-hint" style="margin-top:4px;min-height:14px"></div>' +
        '<div id="new-backend-model-list" class="discover-list" style="display:none"></div></div>' +
      '<div class="field"><label class="field-label" for="new-backend-priority">Priority (Tier)</label>' +
        '<input type="number" id="new-backend-priority" value="1" min="1"></div>' +
      '<div class="modal-actions">' +
        '<button class="secondary" data-cancel>Cancel</button>' +
        '<button class="primary" data-ok>Add</button></div>',
    onMount: (ov) => {
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
      ov.querySelector('[data-ok]').onclick = () => addBackend(ov);
    },
  });
}

async function loadProviderModelSuggestions() {
  const providerId = document.getElementById('new-backend-provider').value;
  const status = document.getElementById('new-backend-model-status');
  const list = document.getElementById('new-backend-model-list');
  const input = document.getElementById('new-backend-model');
  _addBackendModels = [];
  list.innerHTML = '';
  list.style.display = 'none';
  input.value = '';
  if (!providerId) { status.textContent = ''; return; }
  status.textContent = 'Loading catalog…';
  try {
    const { ok, data } = await fetchJSON('/admin/providers/' + encodeURIComponent(providerId) + '/models');
    if (!ok) {
      status.textContent = data.error || 'Failed to load models — type a custom ID';
      return;
    }
    _addBackendModels = (data.models || []).map(m => typeof m === 'string' ? m : (m.id || '')).filter(Boolean);
    status.textContent = _addBackendModels.length + ' models found · type to filter or enter a custom ID';
    filterProviderModelSuggestions();
  } catch (e) {
    status.textContent = e.message + ' — type a custom ID';
  }
}

function filterProviderModelSuggestions() {
  const list = document.getElementById('new-backend-model-list');
  const q = (document.getElementById('new-backend-model').value || '').toLowerCase().trim();
  if (!_addBackendModels.length) { list.style.display = 'none'; return; }
  const filtered = q ? _addBackendModels.filter(id => id.toLowerCase().includes(q)) : _addBackendModels;
  if (!filtered.length) { list.style.display = 'none'; return; }
  list.style.display = 'block';
  list.innerHTML = filtered.slice(0, 100).map(id =>
    `<div class="discover-row" data-id="${escAttr(id)}" onmousedown="pickAddBackendModel(this.dataset.id);return false"><code>${esc(id)}</code></div>`
  ).join('');
}

function pickAddBackendModel(id) {
  document.getElementById('new-backend-model').value = id;
  document.getElementById('new-backend-model-list').style.display = 'none';
}

async function addBackend(overlay) {
  const provider = document.getElementById('new-backend-provider').value;
  const model = document.getElementById('new-backend-model').value.trim();
  const priority = parseInt(document.getElementById('new-backend-priority').value, 10) || 1;
  if (!provider || !model) { toast('Provider and model are required', 'error'); return; }
  const okBtn = overlay && overlay.querySelector('[data-ok]');
  if (okBtn) { okBtn.disabled = true; okBtn.textContent = 'Adding…'; }
  try {
    const cfgModel = currentConfig && currentConfig.models.find(m => m.id === modelDetailState.id);
    if (!cfgModel) { toast('Model not found', 'error'); return; }
    cfgModel.backends = cfgModel.backends || [];
    cfgModel.backends.push({ provider, model, priority, enabled: true });
    const r = await fetch('/admin/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(currentConfig)
    });
    if (r.ok) {
      toast('Provider added', 'success');
      if (overlay) closeModal(overlay);
      reloadModelDetail();
    } else {
      toast('Failed to add provider', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
  if (okBtn) { okBtn.disabled = false; okBtn.textContent = 'Add'; }
}

// ---------- init ----------
document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('model-id');
  if (el) {
    const id = el.dataset.modelId;
    if (id) loadModelDetailPage(id);
  }
});
