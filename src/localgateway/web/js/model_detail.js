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

function fmtPricePrecise(p) {
  if (p == null) return '—';
  return '$' + (p * 1e6).toLocaleString(undefined, { maximumFractionDigits: 6 });
}

function shadeForBackend(tierNum, backendIndexInTier, totalInTier) {
  const tc = tierColorForTier(tierNum);
  if (totalInTier <= 1) return tc.base;
  const t = totalInTier === 1 ? 0.5 : backendIndexInTier / (totalInTier - 1);
  // Keep lightness in a readable band; vary saturation for distinction so
  // shaded text never drops too dark to read on either theme.
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
let tierEditorState = { modelId: null, backends: [], draggingIdx: null };
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
  const sections = ['providers', 'metadata', 'chart'].map(id => document.getElementById(id)).filter(Boolean);
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
  if (!modelDetailState.id) return;
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

function showUnsnoozeConfirm(provider, model) {
  const modal = document.createElement('div');
  modal.className = 'snooze-modal';
  modal.style.cssText = 'display:flex;align-items:center;justify-content:center;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:1001';
  modal.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:340px;max-width:90vw;text-align:center">
      <h3 style="margin:0 0 8px"><code>${esc(provider)}:${esc(model)}</code></h3>
      <p style="font-size:0.85rem;color:var(--text-dim);margin:0 0 20px">This backend is currently snoozed.</p>
      <button class="primary" style="width:100%;padding:12px;font-size:0.9rem" onclick="unsnoozeBackend('${esc(provider)}','${esc(model)}',this)">Unsnooze</button>
      <button class="secondary" style="width:100%;padding:10px;margin-top:10px" onclick="this.closest('.snooze-modal').remove()">Cancel</button>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

function showSnoozeModal(provider, model) {
  const modal = document.createElement('div');
  modal.className = 'snooze-modal';
  modal.style.cssText = 'display:flex;align-items:center;justify-content:center;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:1001';
  const presets = [300, 900, 1800, 3600, 7200, 14400, 86400, 604800];
  modal.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:400px;max-width:90vw">
      <h3 style="margin:0 0 4px">Snooze — <code>${esc(provider)}:${esc(model)}</code></h3>
      <p style="font-size:0.78rem;color:var(--text-dim);margin:0 0 18px">Temporarily remove this backend from routing. Useful when you hit a plan limit.</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
        ${presets.map(s => '<button class="secondary snooze-preset" data-s="'+s+'" style="padding:8px 14px;font-size:0.82rem">'+fmtSnoozeDuration(s)+'</button>').join('')}
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:16px">
        <input type="text" id="snooze-custom" placeholder="e.g. 1d 2h 30m 15s" style="width:200px;padding:8px 10px;font-size:0.85rem;font-family:var(--font-mono)">
        <button class="secondary" onclick="snoozeBackend('${esc(provider)}','${esc(model)}',null,this)">Snooze custom</button>
      </div>
      <button class="secondary" style="width:100%;padding:10px" onclick="snoozeBackendPermanent('${esc(provider)}','${esc(model)}',this)">Snooze until manually removed</button>
      <div style="display:flex;justify-content:flex-end;margin-top:18px">
        <button class="secondary" onclick="this.closest('.snooze-modal').remove()">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  modal.querySelectorAll('.snooze-preset').forEach(btn => {
    btn.onclick = () => snoozeBackend(provider, model, parseInt(btn.dataset.s, 10), btn);
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

async function snoozeBackend(provider, model, seconds, btn) {
  if (seconds == null) {
    const input = document.getElementById('snooze-custom');
    seconds = parseDuration(input && input.value);
  }
  if (!seconds || seconds < 1) { toast('Enter a valid duration (e.g. 1d 2h 30m)', 'error'); return; }
  try {
    await fetchJSON('/admin/backends/snooze', { method: 'POST', body: JSON.stringify({ provider, model, seconds }) });
    toast('Snoozed for ' + fmtSnoozeDuration(seconds), 'success');
    btn.closest('.snooze-modal').remove();
    await reloadModelDetail();
  } catch(e) { toast('Failed to snooze: ' + e.message, 'error'); }
}

async function snoozeBackendPermanent(provider, model, btn) {
  try {
    await fetchJSON('/admin/backends/snooze', { method: 'POST', body: JSON.stringify({ provider, model, permanent: true }) });
    toast('Snoozed until manually removed', 'success');
    btn.closest('.snooze-modal').remove();
    await reloadModelDetail();
  } catch(e) { toast('Failed to snooze: ' + e.message, 'error'); }
}

async function unsnoozeBackend(provider, model, btn) {
  try {
    await fetchJSON('/admin/backends/unsnooze', { method: 'POST', body: JSON.stringify({ provider, model }) });
    toast('Snooze removed', 'success');
    if (btn && btn.closest) btn.closest('.snooze-modal')?.remove();
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

  tierEditorState.modelId = id;

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
    + '<button class="copy-btn" onclick="copyId(\''+esc(id)+'\',this)" title="Copy model ID">⧉</button>'
    + '<span style="color:var(--text-dim);font-size:0.72rem">· '+((cfgModel.backends || []).filter(b=>b.enabled!==false).length)+' backends</span>';
  document.getElementById('detail-desc').textContent = cfgModel.description || '';

  // Update metadata display
  const setMeta = (field, value, elId) => {
    const row = document.querySelector('.meta-row[data-field="' + field + '"]');
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

  // ---- Stat strip ----
  const chartEnabled = currentConfig && currentConfig.server && currentConfig.server.chart_enabled !== false;
  const chartCard = document.getElementById('detail-chart').parentElement;
  if (chartCard) chartCard.style.display = chartEnabled ? '' : 'none';
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
    renderDetailStatStrip(cfgModel, priceStr, ctxStr, bestP50, uptimeStr);
  } catch(e) {
    document.getElementById('detail-rows').innerHTML = '<tr><td colspan="14" class="empty">Gateway is stopped — start it to see live stats.</td></tr>';
    document.getElementById('detail-chart').innerHTML = '<div class="empty">No data.</div>';
    renderDetailStatStrip(cfgModel, '—', '—', '—', '—');
  }
}

function renderDetailStatStrip(cfgModel, priceStr, ctxStr, bestP50, uptimeStr) {
  const el = document.getElementById('detail-stat-strip');
  if (!el) return;
  const cells = [
    { cap: 'Modality', num: cfgModel.modality || '—' },
    { cap: 'In / Out', num: priceStr, sub: 'per 1M tokens' },
    { cap: 'Context', num: ctxStr },
    { cap: 'Best TPS P50', num: bestP50, accent: true, sub: 'tokens/sec' },
    { cap: 'Uptime', num: uptimeStr },
  ];
  el.innerHTML = cells.map(c =>
    '<div class="detail-stat-cell">'
    + '<div class="cap">'+c.cap+'</div>'
    + '<div class="num'+(c.accent?' accent':'')+'">'+c.num+'</div>'
    + (c.sub ? '<div class="sub">'+c.sub+'</div>' : '')
    + '</div>'
  ).join('');
}

function toggleMetadataEdit() {
  const display = document.getElementById('metadata-display');
  const edit = document.getElementById('metadata-edit');
  const btn = document.getElementById('metadata-edit-btn');
  const isHidden = edit.classList.contains('hidden');

  if (isHidden) {
    // Populate edit form
    const id = modelDetailState.id;
    const cfgModel = (currentConfig && currentConfig.models || []).find(m => m.id === id) || {};
    document.getElementById('edit-display-name').value = cfgModel.display_name || '';
    document.getElementById('edit-description').value = cfgModel.description || '';
    document.getElementById('edit-modality').value = cfgModel.modality || '';
    document.getElementById('edit-max-output').value = cfgModel.max_output_tokens || '';
    document.getElementById('edit-tags').value = (cfgModel.tags || []).join(',');
    document.getElementById('edit-aliases').value = (cfgModel.aliases || []).join(',');
    document.getElementById('edit-default-params').value = Object.keys(cfgModel.default_params || {}).length
      ? JSON.stringify(cfgModel.default_params, null, 2) : '';

    const caps = cfgModel.capabilities || {};
    ['text', 'vision', 'audio', 'tools', 'json_mode', 'parallel_tool_calls', 'streaming'].forEach(cap => {
      document.getElementById('cap-' + cap).checked = caps[cap] || false;
    });

    display.classList.add('hidden');
    edit.classList.remove('hidden');
    btn.textContent = 'Cancel';
  } else {
    display.classList.remove('hidden');
    edit.classList.add('hidden');
    btn.textContent = 'Edit';
  }
}

async function saveModelMetadata() {
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
    const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(id), { method: 'PUT', body: JSON.stringify(body) });
    if (ok) {
      toast('Model metadata updated', 'success');
      toggleMetadataEdit();
      currentConfig = (await fetchJSON('/admin/config')).data;
      await reloadModelDetail();
    } else {
      toast('Failed to update metadata', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function editModelSlug() {
  const oldId = modelDetailState.id;
  if (!oldId) return;
  const modal = document.createElement('div');
  modal.className = 'command-palette';
  modal.style.cssText = 'display:flex;align-items:center;justify-content:center;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:1000';
  modal.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:420px;max-width:90vw">
      <h3 style="margin:0 0 8px">Edit Model Slug</h3>
      <p style="font-size:0.78rem;color:var(--text-dim);margin:0 0 16px">Changing the slug will update the model ID and migrate all existing usage data to the new ID.</p>
      <input type="text" id="slug-input" value="${esc(oldId)}" style="width:100%;padding:8px 10px;font-family:var(--font-mono)">
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">
        <button class="secondary" onclick="this.closest('.command-palette').remove()">Cancel</button>
        <button class="primary" onclick="saveModelSlug('${esc(oldId)}',this)">Save</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.getElementById('slug-input').focus();
  document.getElementById('slug-input').select();
}

async function saveModelSlug(oldId, btn) {
  const newId = document.getElementById('slug-input').value.trim();
  if (!newId || newId === oldId) { btn.closest('.command-palette').remove(); return; }
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const r = await fetch('/admin/rename', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ type: 'model', old_id: oldId, new_id: newId }),
    });
    const res = r.ok ? await r.json() : null;
    if (r.ok) {
      const msg = res && res.usage_rows_updated
        ? 'Slug updated (' + res.usage_rows_updated + ' usage rows migrated)'
        : 'Slug updated';
      toast(msg, 'success');
      btn.closest('.command-palette').remove();
      location.href = '/models/' + encodeURIComponent(newId);
    } else {
      const err = res && res.error ? res.error : 'Failed to update slug';
      toast(err, 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
  btn.disabled = false; btn.textContent = 'Save';
}

// ---------- model avatar editor ----------
function editModelAvatar() {
  const id = modelDetailState.id;
  if (!id) return;
  const cfgModel = (currentConfig && currentConfig.models || []).find(m => m.id === id) || {};
  const current = cfgModel.avatar || '';
  const previewId = 'avatar-preview';
  const renderPreview = () => {
    const v = (document.getElementById('avatar-text-input') || {}).value || '';
    document.getElementById(previewId).innerHTML = modelAvatar(id, cfgModel.display_name, 48, v);
  };
  const overlay = openModal({
    title: 'Edit Model Icon',
    bodyHtml:
      '<p class="modal-sub">Custom text to show in this model\'s avatar tile. Leave blank to use the first letter of the display name / ID. Longer text auto-shrinks to fit.</p>' +
      '<div style="display:flex;align-items:center;gap:16px;margin-bottom:14px">' +
        '<div id="'+previewId+'" style="flex-shrink:0"></div>' +
        '<div style="flex:1"><input type="text" id="avatar-text-input" maxlength="12" value="'+esc(current)+'" placeholder="e.g. GPT-4o, Claude, 4o-mini" style="width:100%;padding:8px 12px"></div>' +
      '</div>' +
      '<div class="modal-actions">' +
        '<button class="secondary" onclick="clearModelAvatar()">Reset to default</button>' +
        '<button class="secondary" data-act="cancel">Cancel</button>' +
        '<button class="primary" data-act="save">Save</button>' +
      '</div>',
  });
  renderPreview();
  document.getElementById('avatar-text-input').addEventListener('input', renderPreview);
  overlay.querySelector('[data-act="cancel"]').onclick = () => closeModal(overlay);
  overlay.querySelector('[data-act="save"]').onclick = async () => {
    const v = document.getElementById('avatar-text-input').value;
    try {
      const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(id), { method: 'PUT', body: JSON.stringify({ avatar: v }) });
      if (ok) {
        toast('Model icon updated', 'success');
        closeModal(overlay);
        currentConfig = (await fetchJSON('/admin/config')).data;
        await reloadModelDetail();
      } else {
        toast('Failed to update icon', 'error');
      }
    } catch(e) { toast('Error: ' + e.message, 'error'); }
  };
  // expose clear for inline button
  window.clearModelAvatar = () => {
    document.getElementById('avatar-text-input').value = '';
    renderPreview();
  };
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

  const existingModal = document.querySelector('.probe-report-modal');
  if (existingModal) existingModal.remove();
  
  const modal = document.createElement('div');
  modal.className = 'probe-report-modal';
  modal.style.cssText = 'display:flex;align-items:center;justify-content:center;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:1001';
  const rows = results.map(r => {
    const tierNum = r.priority || 1;
    const tc = tierColorForTier(tierNum);
    if (r.skipped) {
      const reason = r.error === 'snoozed' ? 'snoozed' : 'disabled';
      return '<tr style="opacity:0.5"><td style="color:'+tc.cssVar+';font-weight:600">'+tierNum+'</td><td><code>'+esc(r.provider)+'</code>:<code>'+esc(r.model)+'</code></td><td colspan="3" style="color:var(--text-dim)">'+reason+'</td></tr>';
    }
    if (!r.ok) {
      return '<tr><td style="color:'+tc.cssVar+';font-weight:600">'+tierNum+'</td><td><code>'+esc(r.provider)+'</code>:<code>'+esc(r.model)+'</code></td><td colspan="3" style="color:var(--danger);font-weight:600">ERROR</td></tr>';
    }
    const tps = r.tps != null ? r.tps.toFixed(1) : '—';
    return '<tr style="background:'+tierBgForTier(tierNum)+'">'
      + '<td style="color:'+tc.cssVar+';font-weight:600">'+tierNum+'</td>'
      + '<td><code style="color:'+tc.base+'">'+esc(r.provider)+'</code>:<code>'+esc(r.model)+'</code></td>'
      + '<td>'+(r.ttft_ms != null ? r.ttft_ms+'ms' : '—')+'</td>'
      + '<td>'+(r.latency_ms != null ? (r.latency_ms/1000).toFixed(2)+'s' : '—')+'</td>'
      + '<td style="font-weight:600;color:'+tc.base+'">'+tps+' tps</td>'
      + '</tr>';
  }).join('');
  modal.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:600px;max-width:90vw;max-height:85vh;overflow:auto">
      <h3 style="margin:0 0 4px">Probe Report — <code>${esc(id)}</code></h3>
      <p style="font-size:0.78rem;color:var(--text-dim);margin:0 0 18px">Live probe results (${results.filter(r=>r.ok).length}/${results.length} ok).</p>
      <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <thead><tr style="text-align:left;color:var(--text-dim);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em">
          <th style="padding:6px 8px">Tier</th>
          <th style="padding:6px 8px">Provider</th>
          <th style="padding:6px 8px">TTFT</th>
          <th style="padding:6px 8px">Latency</th>
          <th style="padding:6px 8px">Throughput</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div style="display:flex;justify-content:flex-end;margin-top:18px">
        <button class="primary" onclick="this.closest('.probe-report-modal').remove()">Close</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

function probeReport(results) {
  // probeReport is handled inline within probeModelNow; this stub keeps any external calls safe.
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
    th.style.cursor = 'pointer';
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
  const tierIndexInGroup = {};
  tierKeys.forEach(t => {
    tierGroups[t].forEach((b, i) => { tierIndexInGroup[b.provider + ':' + b.backend_model] = i; });
  });

  el.innerHTML = rows.map((b, i) => {
    const off = !b.enabled;
    const cooldown = b.cooldown_remaining || 0;
    const isSnoozed = cooldown !== 0;
    const isPermanent = cooldown < 0;
    const snoozeBadge = isSnoozed 
      ? (isPermanent 
          ? '<span class="badge badge-gray" style="opacity:0.9">(snoozed)</span>'
          : '<span class="badge badge-gray" style="opacity:0.9">(snoozed '+fmtSnoozeDurationRounded(cooldown)+')</span>')
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

    return '<tr draggable="true" data-provider="'+esc(b.provider)+'" data-model="'+esc(b.backend_model)+'" data-priority="'+tierNum+'" style="background:'+rowBg+';'+rowOpacity+'" ondragstart="detailRowDragStart(event)" ondragover="detailRowDragOver(event)" ondrop="detailRowDrop(event)" ondragend="detailRowDragEnd(event)">'
      + '<td style="cursor:grab;color:var(--text-dim);padding-left:12px" title="Drag to reorder">⋮⋮</td>'
      + '<td style="font-weight:600;color:'+tc.cssVar+';border-left:3px solid '+shade+';padding-left:8px">'+tierNum+'</td>'
      + '<td><span class="routing-dot '+(isSnoozed?'snoozed':'')+'" data-provider="'+esc(b.provider)+'" data-model="'+esc(b.backend_model)+'" title="'+(isSnoozed?'Click to manage snooze':'Click to snooze')+'"></span>'+providerAvatar(b.provider, 20, (b.provider_avatar || ''))+'<code style="color:'+shade+'">'+esc(b.provider)+'</code>:<code>'+esc(b.backend_model)+'</code> '+badge+' <button class="icon-btn" style="padding:1px 5px;font-size:0.7rem" onclick="editProviderPricing(\''+esc(b.provider)+'\',\''+esc(b.backend_model)+'\')" title="Edit pricing">$</button></td>'
      + '<td>'+(b.context_length ? fmtTokens(b.context_length) : '—')+'</td>'
      + '<td>'+(b.max_output_tokens ? fmtTokens(b.max_output_tokens) : '—')+'</td>'
      + '<td>'+fmtPricePrecise(b.input_price)+'</td>'
      + '<td>'+fmtPricePrecise(b.output_price)+'</td>'
      + '<td style="font-size:0.78rem;color:var(--text-dim)">'+(b.cache_read_price != null || b.cache_write_price != null ? fmtPricePrecise(b.cache_read_price)+' / '+fmtPricePrecise(b.cache_write_price) : '— / —')+'</td>'
      + '<td>'+(b.ttft_ms != null ? fmt(b.ttft_ms, 0)+'ms' : '—')+'</td>'
      + '<td style="font-weight:600;color:'+shade+'">'+(b.tps != null ? fmt(b.tps, 0)+' tps' : '—')+'</td>'
      + '<td>'+(b.latency_ms != null ? fmt(b.latency_ms/1000, 2)+'s' : '—')+'</td>'
      + '<td>'+up+'</td>'
      + '<td>'+fmt(b.requests, 0)+'</td>'
      + '<td style="padding:4px 8px"><button class="icon-btn" style="padding:1px 5px;font-size:0.7rem;color:var(--text-dim)" onclick="removeBackendFromModel(\''+esc(b.provider)+'\',\''+esc(b.backend_model)+'\')" title="Remove provider">×</button></td>'
      + '</tr>';
  }).join('');
}

function editProviderPricing(provider, backendModel) {
  const pricing = (currentConfig && currentConfig.pricing && currentConfig.pricing[provider + ':' + backendModel]) || {};
  const modal = document.createElement('div');
  modal.className = 'command-palette';
  modal.style.cssText = 'display:flex;align-items:center;justify-content:center;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000';
  modal.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:380px;max-width:90vw">
      <h3 style="margin:0 0 4px">Pricing — <code>${esc(provider)}:${esc(backendModel)}</code></h3>
      <p style="font-size:0.78rem;color:var(--text-dim);margin:0 0 18px">All prices per 1M tokens.</p>
      <div style="display:grid;gap:12px">
        <div><label style="display:block;font-size:0.78rem;color:var(--text-dim);margin-bottom:4px">Input ($/M)</label><input type="number" id="px-input" step="0.0001" min="0" value="${pricing.input != null ? (pricing.input * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : ''}" style="width:100%;padding:8px 10px"></div>
        <div><label style="display:block;font-size:0.78rem;color:var(--text-dim);margin-bottom:4px">Output ($/M)</label><input type="number" id="px-output" step="0.0001" min="0" value="${pricing.output != null ? (pricing.output * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : ''}" style="width:100%;padding:8px 10px"></div>
        <div><label style="display:block;font-size:0.78rem;color:var(--text-dim);margin-bottom:4px">Cache read ($/M)</label><input type="number" id="px-cache-read" step="0.0001" min="0" value="${pricing.cache_read != null ? (pricing.cache_read * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : ''}" placeholder="same as input" style="width:100%;padding:8px 10px"></div>
        <div><label style="display:block;font-size:0.78rem;color:var(--text-dim);margin-bottom:4px">Cache write ($/M)</label><input type="number" id="px-cache-write" step="0.0001" min="0" value="${pricing.cache_write != null ? (pricing.cache_write * 1e6).toLocaleString(undefined,{maximumFractionDigits:6}) : ''}" placeholder="same as input" style="width:100%;padding:8px 10px"></div>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">
        <button class="secondary" onclick="this.closest('.command-palette').remove()">Cancel</button>
        <button class="primary" onclick="saveProviderPricing('${esc(provider)}','${esc(backendModel)}',this)">Save</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

async function saveProviderPricing(provider, backendModel, btn) {
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
      btn.closest('.command-palette').remove();
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
  if (detailSeriesCache) renderTpsChart(detailSeriesCache);
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
  if (detailSeriesCache) renderTpsChart(detailSeriesCache);
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
    grid += '<line x1="'+PL+'" y1="'+yy+'" x2="'+(W-PR)+'" y2="'+yy+'" stroke="#2d3343" stroke-width="1"/>'
      + '<text x="'+(PL-7)+'" y="'+(+yy+3)+'" text-anchor="end" font-size="9" fill="#a5adc0">'+Math.round(v)+'</text>';
  }
  const fmtTick = t => { const d = new Date(t * 1000); return (d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':00'; };
  [t0, (t0+t1)/2, t1].forEach(t => {
    if (t1 > t0) paths += '<text x="'+x(t).toFixed(1)+'" y="'+(H-8)+'" text-anchor="middle" font-size="9" fill="#a5adc0">'+fmtTick(t)+'</text>';
  });
  entries.forEach(([k, pts], i) => {
    const c = colorMap[k] || tierColorForTier(i + 1).base;
    pts = pts.slice().sort((a, b) => a.t - b.t);
    const d = pts.map((p, j) => (j ? 'L' : 'M') + x(p.t).toFixed(1) + ' ' + y(p.tps).toFixed(1)).join(' ');
    paths += '<path d="'+d+'" fill="none" stroke="'+c+'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
    pts.forEach(p => { paths += '<circle cx="'+x(p.t).toFixed(1)+'" cy="'+y(p.tps).toFixed(1)+'" r="2.6" fill="'+c+'"><title>'+esc(k)+' · '+p.tps+' tps</title></circle>'; });
    legend += '<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;font-size:0.76rem;color:var(--text-dim)">'
      + '<span style="width:12px;height:3px;background:'+c+';border-radius:2px;display:inline-block"></span><code>'+esc(k)+'</code></span>';
  });
  el.innerHTML = '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto">'+grid+paths+'</svg><div style="margin-top:8px">'+legend+'</div>';
}

// ---------- tier editor (drag & drop) ----------
function initTierEditor(modelId, backends) {
  tierEditorState.modelId = modelId;
  tierEditorState.backends = backends.slice().sort((a, b) => a.priority - b.priority);
  tierEditorState.draggingIdx = null;
}

function renderTierEditor() {
  const container = document.getElementById('tier-editor-container');
  if (!container || !tierEditorState.modelId) return;

  const tiers = {};
  tierEditorState.backends.forEach((b, i) => {
    const p = b.priority || 1;
    tiers[p] = tiers[p] || [];
    tiers[p].push({ ...b, _idx: i });
  });
  const tierKeys = Object.keys(tiers).map(Number).sort((a, b) => a - b);

  let html = '<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start">';
  tierKeys.forEach((tierNum, tierPos) => {
    const backends = tiers[tierNum];
    const primary = tierPos === 0;
    const tc = tierColorForTier(tierNum);
    const tierBg = tierBgForTier(tierNum);
    const tierBorder = tc.base;
    const tierText = tc.cssVar;
    html += `<div class="tier-column" data-tier="${tierNum}" style="min-width:200px;flex:1;background:${tierBg};border:1px solid ${tierBorder};border-radius:10px;padding:12px;transition:border-color 0.15s">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <h3 style="font-size:0.82rem;color:${tierText};margin:0">
          Tier ${tierNum}${primary ? ' (primary)' : ''}
          <span style="color:var(--text-dim);font-size:0.7rem;margin-left:6px">${backends.length} provider${backends.length !== 1 ? 's' : ''}</span>
        </h3>
        ${tierPos > 0 ? `<button class="icon-btn" style="padding:2px 6px;font-size:0.7rem" onclick="removeTier(${tierNum})" title="Merge into previous tier">×</button>` : ''}
      </div>
      <div class="tier-backends" data-tier="${tierNum}" style="display:flex;flex-direction:column;gap:6px;min-height:50px;border-radius:6px;transition:background 0.15s"
        ondragover="tierDragOver(event, this)" ondragleave="tierDragLeave(event, this)" ondrop="tierDrop(event, ${tierNum})">
        ${backends.map(b => {
          const shade = shadeForBackend(tierNum, backends.indexOf(b), backends.length);
          return `
          <div class="tier-chip" draggable="true" data-idx="${b._idx}"
            ondragstart="tierDragStart(event, ${b._idx})" ondragend="tierDragEnd(event)"
            style="display:flex;align-items:center;gap:8px;padding:10px;background:var(--surface);border:1px solid ${shade};border-radius:8px;cursor:grab;font-size:0.82rem">
            <span style="width:8px;height:8px;border-radius:50%;background:${shade};flex-shrink:0"></span>
            <span style="cursor:grab;color:var(--text-dim)">⋮⋮</span>
            <code style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${shade}" title="${esc(b.provider)}:${esc(b.model)}">${esc(b.provider)}:${esc(b.model)}</code>
          </div>`;
        }).join('')}
        ${backends.length === 0 ? '<div style="text-align:center;color:var(--text-dim);font-size:0.78rem;padding:14px">drop providers here</div>' : ''}
      </div>
    </div>`;
  });
  html += `<div class="tier-column" data-tier="new" style="min-width:140px;background:transparent;border:2px dashed var(--border);border-radius:10px;padding:12px;display:flex;align-items:center;justify-content:center"
    ondragover="tierDragOver(event, this)" ondragleave="tierDragLeave(event, this)" ondrop="tierDropNew(event)">
    <span style="color:var(--text-dim);font-size:0.8rem;text-align:center">+ New tier<br><span style="font-size:0.7rem">drop here</span></span>
  </div>`;
  html += '</div>';
  html += '<div style="margin-top:12px;font-size:0.72rem;color:var(--text-dim)">Drag providers between tiers. Multiple providers in the same tier share traffic round-robin.</div>';

  container.innerHTML = html;
}

function tierDragStart(ev, idx) {
  tierEditorState.draggingIdx = idx;
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setData('text/plain', String(idx));
  setTimeout(() => ev.target.style.opacity = '0.4', 0);
}

function tierDragEnd(ev) {
  ev.target.style.opacity = '1';
  tierEditorState.draggingIdx = null;
  document.querySelectorAll('.tier-backends').forEach(el => {
    el.style.background = '';
  });
}

function tierDragOver(ev, el) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
  if (el) el.style.background = 'var(--surface)';
}

function tierDragLeave(ev, el) {
  if (el) el.style.background = '';
}

function tierDrop(ev, tierNum) {
  ev.preventDefault();
  const idx = tierEditorState.draggingIdx;
  if (idx == null) return;
  const b = tierEditorState.backends[idx];
  if (b.priority === tierNum) return;
  b.priority = tierNum;
  renderTierEditor();
  saveTierChanges();
}

function tierDropNew(ev) {
  ev.preventDefault();
  const idx = tierEditorState.draggingIdx;
  if (idx == null) return;
  const maxTier = Math.max(0, ...tierEditorState.backends.map(b => b.priority || 1));
  tierEditorState.backends[idx].priority = maxTier + 1;
  renderTierEditor();
  saveTierChanges();
}

function removeTier(tierNum) {
  const tierKeys = [...new Set(tierEditorState.backends.map(b => b.priority || 1))].sort((a, b) => a - b);
  const pos = tierKeys.indexOf(tierNum);
  if (pos <= 0) return;
  const targetTier = tierKeys[pos - 1];
  tierEditorState.backends.forEach(b => {
    if ((b.priority || 1) === tierNum) b.priority = targetTier;
  });
  renderTierEditor();
  saveTierChanges();
}

function moveBackendTier(idx, delta) {
  const b = tierEditorState.backends[idx];
  const currentTiers = [...new Set(tierEditorState.backends.map(x => x.priority || 1))].sort((a, b) => a - b);
  const currentIdx = currentTiers.indexOf(b.priority || 1);
  const newTierIdx = Math.max(0, Math.min(currentTiers.length - 1, currentIdx + delta));
  const newTier = currentTiers[newTierIdx] || (currentTiers.length + 1);
  b.priority = newTier;
  saveTierChanges();
}

function addNewTier() {
  const cfgModel = currentConfig && currentConfig.models.find(m => m.id === tierEditorState.modelId);
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
    const cm = currentConfig && currentConfig.models.find(m => m.id === tierEditorState.modelId);
    if (!cm) return;
    const draggedBackend = cm.backends.find(b => b.provider === _detailDragProvider && b.model === _detailDragModel);
    if (!draggedBackend) return;
    draggedBackend.priority = newTier;
    saveTierChanges();
  };
  tr.innerHTML = '<td colspan="14" style="text-align:center;padding:14px;color:'+tc.cssVar+';font-size:0.82rem">Drop a provider here to create Tier '+newTier+'</td>';
  el.appendChild(tr);
  toast(`New tier ${newTier} ready — drag a provider into it.`, 'info');
}

async function saveTierChanges() {
  const cfgModel = currentConfig && currentConfig.models.find(m => m.id === tierEditorState.modelId);
  if (!cfgModel) { toast('Model not found in config', 'error'); return; }
  const tiers = {};
  cfgModel.backends.forEach((b, i) => {
    const p = b.priority || 1;
    tiers[p] = tiers[p] || [];
    tiers[p].push(i);
  });
  const tierList = Object.keys(tiers).map(Number).sort((a, b) => a - b).map(p => tiers[p]);
  try {
    const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(tierEditorState.modelId) + '/backends/tiers', {
      method: 'PUT',
      body: JSON.stringify({ tiers: tierList })
    });
    if (ok) {
      toast('Tier layout saved', 'success');
      if (typeof reloadModelDetail === 'function') reloadModelDetail();
    } else {
      toast('Failed to save tier layout', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function removeBackendFromModel(provider, backendModel) {
  const cfgModel = currentConfig && currentConfig.models.find(m => m.id === tierEditorState.modelId);
  if (!cfgModel) return;
  const idx = cfgModel.backends.findIndex(b => b.provider === provider && b.model === backendModel);
  if (idx < 0) return;
  const confirmed = await showConfirm('Remove Provider', 'Remove ' + provider + ':' + backendModel + ' from this model?');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/models/' + encodeURIComponent(tierEditorState.modelId) + '/backends/' + idx, { method: 'DELETE' });
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
  const modelId = tierEditorState.modelId;
  if (!modelId) { toast('No model selected', 'error'); return; }
  const providers = (currentConfig && currentConfig.providers || []).filter(p => p.enabled);
  const modal = document.createElement('div');
  modal.className = 'command-palette';
  modal.style.cssText = 'display:flex;align-items:center;justify-content:center;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:1000';
  modal.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:460px;max-width:90vw">
      <h3 style="margin:0 0 16px">Add Provider to Model</h3>
      <div style="display:grid;gap:12px">
        <div><label style="display:block;font-size:0.78rem;color:var(--text-dim);margin-bottom:4px">Provider *</label>
          <select id="new-backend-provider" style="width:100%;padding:8px 10px" onchange="loadProviderModelSuggestions()">
            <option value="">Select provider</option>
            ${providers.map(p => '<option value="'+esc(p.id)+'">'+esc(p.name || p.id)+'</option>').join('')}
          </select>
        </div>
        <div>
          <label style="display:block;font-size:0.78rem;color:var(--text-dim);margin-bottom:4px">Backend Model *</label>
          <input type="text" id="new-backend-model" placeholder="Pick from list or type a custom model ID" style="width:100%;padding:8px 10px" oninput="filterProviderModelSuggestions()">
          <div id="new-backend-model-status" style="font-size:0.72rem;color:var(--text-dim);margin-top:4px;min-height:14px"></div>
          <div id="new-backend-model-list" style="margin-top:4px;max-height:180px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;display:none"></div>
        </div>
        <div><label style="display:block;font-size:0.78rem;color:var(--text-dim);margin-bottom:4px">Priority (Tier)</label><input type="number" id="new-backend-priority" value="1" min="1" style="width:100%;padding:8px 10px"></div>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">
        <button class="secondary" onclick="this.closest('.command-palette').remove()">Cancel</button>
        <button class="primary" onclick="addBackend(this)">Add</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
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
    `<div style="padding:7px 10px;border-bottom:1px solid var(--border);cursor:pointer;font-family:var(--font-mono);font-size:0.82rem" onmousedown="pickAddBackendModel('${esc(id)}');return false">${esc(id)}</div>`
  ).join('');
}

function pickAddBackendModel(id) {
  document.getElementById('new-backend-model').value = id;
  document.getElementById('new-backend-model-list').style.display = 'none';
}

async function addBackend(btn) {
  const provider = document.getElementById('new-backend-provider').value;
  const model = document.getElementById('new-backend-model').value.trim();
  const priority = parseInt(document.getElementById('new-backend-priority').value, 10) || 1;
  if (!provider || !model) { toast('Provider and model are required', 'error'); return; }
  btn.disabled = true;
  btn.textContent = 'Adding…';
  try {
    const cfgModel = currentConfig && currentConfig.models.find(m => m.id === tierEditorState.modelId);
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
      btn.closest('.command-palette').remove();
      reloadModelDetail();
    } else {
      toast('Failed to add provider', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
  btn.disabled = false;
  btn.textContent = 'Add';
}

// ---------- init ----------
document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('model-id');
  if (el) {
    const id = el.dataset.modelId;
    if (id) loadModelDetailPage(id);
  }
});