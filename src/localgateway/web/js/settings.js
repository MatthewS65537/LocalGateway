// settings.js — settings page logic
// P10: seven loaders each fetched /admin/config (11 requests total, 7
// duplicates). Memoize one fetch per page load; invalidated after any save
// via the window hook registered below.
let _configPromise = null;
function getSettingsConfig() {
  if (!_configPromise) {
    _configPromise = (async () => {
      const { ok, data } = await apiFetch('/admin/config', { silent: true });
      if (!ok) { _configPromise = null; return null; }
      return data;
    })();
  }
  return _configPromise;
}
function invalidateSettingsConfig() { _configPromise = null; }
window.__onConfigSaved = invalidateSettingsConfig;

function loadSettingsPage() {
  loadServerSettings();
  loadAccessKeys();
  loadRoutingSettings();
  loadCircuitSettings();
  loadCacheAffinitySettings();
  loadCacheAdmin();  // C3: response-cache stats + enable flag
  loadWarmthHeatmap();
  loadAlertSettings();
  loadProbeSettings();
  loadDisplaySettings();
  loadTimeRouting();  // U2: was never called — the time-slot editor was dead on arrival
  loadBackups();
}

const REDACTED = '\u2022\u2022\u2022\u2022\u2022\u2022';

async function loadServerSettings() {
  resetLoadError();
  const data = await getSettingsConfig();
  if (!data) { loadError('settings'); return; }
  const port = data.server?.port || 3456;
  document.getElementById('settings-port').value = port;
  const keyInput = document.getElementById('settings-api-key');
  const raw = data.server?.api_key || '';
  window.__lgLegacyKeySet__ = !!raw;
  keyInput.value = raw === REDACTED ? '' : raw;
  keyInput.placeholder = raw ? '(set — leave blank to keep)' : '(none — open access)';
  keyInput.type = 'password';
  const showBtn = keyInput.nextElementSibling;
  if (showBtn) showBtn.textContent = 'Show';
}

function toggleApiKeyVisibility() {
  const input = document.getElementById('settings-api-key');
  const btn = input.nextElementSibling;
  if (input.type === 'password') {
    input.type = 'text';
    if (btn) btn.textContent = 'Hide';
  } else {
    input.type = 'password';
    if (btn) btn.textContent = 'Show';
  }
}

async function copyApiKey() {
  const val = document.getElementById('settings-api-key').value;
  if (!val) { toast('No API key set', 'info'); return; }
  try {
    await navigator.clipboard.writeText(val);
    toast('API key copied', 'success');
  } catch(e) {
    toast('Could not copy', 'error');
  }
}

async function saveServerSettings() {
  const port = parseInt(document.getElementById('settings-port').value, 10);
  if (!port || port < 1 || port > 65535) { toast('Port must be 1-65535', 'error'); return; }
  const apiKey = document.getElementById('settings-api-key').value.trim();
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.port = port;
    if (window.__lgClearLegacyKey__) data.server.api_key = null;
    else data.server.api_key = apiKey || (window.__lgLegacyKeySet__ ? REDACTED : null);
  });
  if (ok) toast('Server settings saved. Restart required for port changes.', 'success');
  else toast('Failed to save server settings', 'error');
}

async function loadRoutingSettings() {
  const data = await getSettingsConfig();
  if (!data) { loadError('settings'); return; }
  const mode = data.server?.routing_mode || 'explore';
  const decay = data.server?.routing_decay || 0.4;
  document.getElementById('settings-routing-mode').value = mode;
  document.getElementById('settings-routing-decay').value = decay;
  const statsRouting = document.getElementById('settings-stats-routing');
  if (statsRouting) statsRouting.checked = data.server?.stats_routing_enabled !== false;
  updateDecayViz();
}

function toggleStatsRouting() {
  // Visual only; value is read on save.
}

function updateDecayViz() {
  const mode = document.getElementById('settings-routing-mode').value;
  const decay = parseFloat(document.getElementById('settings-routing-decay').value) || 0.4;
  document.getElementById('decay-value').textContent = decay.toFixed(2);
  document.getElementById('decay-section').classList.toggle('hidden', mode !== 'explore');

  const viz = document.getElementById('decay-viz');
  viz.innerHTML = '';
  const colors = ['var(--accent)', 'var(--green)', 'var(--yellow)', 'var(--red)', 'var(--text-dim)'];
  for (let tiers = 2; tiers <= 5; tiers++) {
    const weights = [];
    for (let i = 0; i < tiers; i++) weights.push(Math.pow(decay, i));
    const sum = weights.reduce((a, b) => a + b, 0);
    const norm = weights.map(w => (w / sum * 100));

    const row = document.createElement('div');
    row.className = 'decay-bar-row';
    row.innerHTML = `<span class="filter-hint">${tiers} tiers</span>
      <div class="decay-bar">
        ${norm.map((p, i) => `<div class="decay-seg" style="width:${p}%;background:${colors[i]};color:rgba(0,0,0,0.7)" title="Tier ${i+1}: ${p.toFixed(1)}%">${p >= 8 ? p.toFixed(0)+'%' : ''}</div>`).join('')}
      </div>`;
    viz.appendChild(row);
  }
}

async function saveRoutingSettings() {
  const mode = document.getElementById('settings-routing-mode').value;
  const decay = parseFloat(document.getElementById('settings-routing-decay').value) || 0.4;
  const statsRouting = document.getElementById('settings-stats-routing');
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.routing_mode = mode;
    data.server.routing_decay = decay;
    if (statsRouting) data.server.stats_routing_enabled = statsRouting.checked;
  });
  if (ok) toast('Routing settings saved', 'success');
  else toast('Failed to save routing settings', 'error');
}

async function loadCacheAffinitySettings() {
  const data = await getSettingsConfig();
  if (!data) { loadError('settings'); return; }
  const enabled = data.server?.cache_affinity_enabled === true;
  const ttl = data.server?.cache_affinity_ttl_sec || 300;
  const spill = data.server?.max_inflight_before_spill;
  document.getElementById('settings-cache-affinity').checked = enabled;
  const ttlSelect = document.getElementById('settings-cache-ttl');
  if (ttlSelect) ttlSelect.value = String(ttl);
  document.getElementById('settings-cache-spill').value = spill != null ? spill : '';
  toggleCacheAffinity();
}

function toggleCacheAffinity() {
  const enabled = document.getElementById('settings-cache-affinity').checked;
  document.getElementById('cache-affinity-options').classList.toggle('dimmed', !enabled);
}

async function saveCacheAffinitySettings() {
  const enabled = document.getElementById('settings-cache-affinity').checked;
  const ttl = parseInt(document.getElementById('settings-cache-ttl').value, 10) || 300;
  const spillRaw = document.getElementById('settings-cache-spill').value.trim();
  const spill = spillRaw === '' ? null : (parseInt(spillRaw, 10) || null);
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.cache_affinity_enabled = enabled;
    data.server.cache_affinity_ttl_sec = ttl;
    data.server.max_inflight_before_spill = spill;
  });
  if (ok) toast('Cache affinity settings saved', 'success');
  else toast('Failed to save cache affinity settings', 'error');
}

async function clearWarmth() {
  const confirmed = await showConfirm('Clear Warmth Data', 'This forgets every warm-cache entry. Routing returns to cold mode until requests warm up again.');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/warmth', { method: 'DELETE' });
    if (r.ok) { toast('Warmth data cleared', 'success'); }
    else {
      const d = await r.json().catch(() => ({}));
      toast(d.error || 'Failed to clear warmth data', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function loadProbeSettings() {
  const data = await getSettingsConfig();
  if (!data) { loadError('settings'); return; }
  const enabled = data.server?.probe_enabled !== false;
  const interval = data.server?.probe_interval_s || 3600;
  const maxTokens = data.server?.probe_max_tokens || 64;
  document.getElementById('settings-probe-enabled').checked = enabled;
  document.getElementById('settings-probe-interval').value = interval;
  document.getElementById('settings-probe-max-tokens').value = maxTokens;
  toggleProbeSettings();
}

function toggleProbeSettings() {
  const enabled = document.getElementById('settings-probe-enabled').checked;
  document.getElementById('probe-options').classList.toggle('dimmed', !enabled);
}

async function saveProbeSettings() {
  const enabled = document.getElementById('settings-probe-enabled').checked;
  const interval = parseInt(document.getElementById('settings-probe-interval').value, 10);
  const maxTokens = parseInt(document.getElementById('settings-probe-max-tokens').value, 10);
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.probe_enabled = enabled;
    data.server.probe_interval_s = interval;
    data.server.probe_max_tokens = maxTokens;
  });
  if (ok) toast('Probe settings saved', 'success');
  else toast('Failed to save probe settings', 'error');
}

async function loadDisplaySettings() {
  const data = await getSettingsConfig();
  if (!data) { loadError('settings'); return; }
  const enabled = data.server?.chart_enabled !== false;
  document.getElementById('settings-chart-enabled').checked = enabled;
}

async function saveDisplaySettings() {
  const chartEnabled = document.getElementById('settings-chart-enabled').checked;
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.chart_enabled = chartEnabled;
  });
  if (ok) toast('Display settings saved', 'success');
  else toast('Failed to save display settings', 'error');
}

async function clearUsageData() {
  const confirmed = await showConfirm('Clear Usage Data', 'This will permanently delete all token usage history. This action cannot be undone.');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/usage', { method: 'DELETE' });
    if (r.ok) {
      toast('Usage data cleared', 'success');
    } else {
      const d = await r.json();
      toast(d.error || 'Failed to clear usage data', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

// ---------- access keys (governance) ----------
async function loadAccessKeys() {
  const wrap = document.getElementById('keys-list-wrap');
  try {
    const { ok, data } = await apiFetch('/admin/keys/spend', { silent: true });
    if (!ok) { wrap.innerHTML = '<p class="field-hint">Failed to load keys.</p>'; return; }
    renderAccessKeys(data.keys || []);
  } catch(e) { wrap.innerHTML = '<p class="field-hint">Failed to load keys.</p>'; }
}

function renderAccessKeys(keys) {
  const wrap = document.getElementById('keys-list-wrap');
  if (!keys.length) {
    wrap.innerHTML = '<p class="field-hint">No access keys configured — the gateway is open (any client may call /v1). Add a key below to lock it down.</p>';
    return;
  }
  const fmtUsd = n => n == null ? 'unlimited' : '$' + n.toFixed(2);
  wrap.innerHTML = keys.map(k => {
    const budget = [k.daily_budget_usd, k.monthly_budget_usd].some(v => v != null)
      ? 'daily ' + fmtUsd(k.daily_budget_usd) + ' · monthly ' + fmtUsd(k.monthly_budget_usd)
      : 'no budget';
    const allow = k.model_allowlist && k.model_allowlist.length ? k.model_allowlist.join(', ') : 'all models';
    const warn = (k.daily_budget_usd && k.day_cost / k.daily_budget_usd >= 0.8) || (k.monthly_budget_usd && k.month_cost / k.monthly_budget_usd >= 0.8);
    return '<div class="key-row' + (k.enabled === false ? ' dimmed' : '') + '">' +
      '<div class="key-main">' +
        '<div class="hstack" style="gap:8px;align-items:center">' +
          '<span class="badge ' + (k.enabled === false ? 'badge-gray' : 'badge-green') + '">' + (k.enabled === false ? 'off' : 'on') + '</span>' +
          '<strong>' + esc(k.label || k.id) + '</strong>' +
          '<code style="font-size:0.74rem">' + esc(k.id) + '</code>' +
          '<span class="filter-hint">' + (k.rpm ? k.rpm + ' rpm' : 'no rpm') + '</span>' +
          '<span class="filter-hint">' + esc(budget) + '</span>' +
          '<span class="filter-hint">· ' + esc(allow) + '</span>' +
        '</div>' +
        '<div class="filter-hint" style="margin-top:4px">' +
          (k.day_requests || 0) + ' req today · $' + k.day_cost.toFixed(4) + ' · $' + k.month_cost.toFixed(4) + ' this month' +
          (k.current_rpm ? ' · ' + k.current_rpm + ' rpm in window' : '') +
          (warn ? ' <span class="badge badge-yellow">near budget</span>' : '') +
        '</div>' +
      '</div>' +
      '<div class="hstack" style="gap:8px">' +
        '<button class="secondary btn-sm" data-key="' + escAttr(k.id) + '" data-action="revealApiKey(this.dataset.key)">Reveal</button>' +
        '<button class="secondary btn-sm" data-key="' + escAttr(k.id) + '" data-action="editApiKey(this.dataset.key)">Edit</button>' +
        '<button class="secondary btn-sm" data-key="' + escAttr(k.id) + '" data-action="toggleApiKeyEnabled(this.dataset.key)">' + (k.enabled === false ? 'Enable' : 'Disable') + '</button>' +
        '<button class="danger btn-sm" data-key="' + escAttr(k.id) + '" data-action="deleteApiKey(this.dataset.key)">Delete</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

function _keyModalTitle(existing) { return existing ? 'Edit API Key' : 'Add API Key'; }

function showAddKeyModal() {
  openKeyModal(null);
}

function editApiKey(keyId) {
  openKeyModal(keyId);
}

async function openKeyModal(existingId) {
  let existing = null;
  if (existingId) {
    const { ok, data } = await apiFetch('/admin/config', { silent: true });
    if (!ok) return;
    existing = (data.server?.api_keys || []).find(k => k.id === existingId);
    if (!existing) { toast('Key not found', 'error'); return; }
  }
  const isEdit = !!existing;
  const k = existing || { id: '', key: '', label: '', rpm: null, daily_budget_usd: null, monthly_budget_usd: null, model_allowlist: [], enabled: true };
  const overlay = openModal({
    title: isEdit ? 'Edit API Key' : 'Add API Key',
    widthClass: 'modal-lg',
    bodyHtml:
      '<p class="modal-sub">Keys are sent as Bearer tokens to /v1 and /api/v1. Quotas and allowlists apply per key.</p>' +
      '<div class="field-group">' +
        '<div class="hstack">' +
          '<div class="field" style="flex:1"><label class="field-label" for="ak-id">Key ID (slug)</label>' +
            '<input type="text" id="ak-id" class="mono" value="' + esc(k.id) + '"' + (isEdit ? ' disabled' : '') + ' placeholder="e.g. home-agent"></div>' +
          '<div class="field" style="flex:1"><label class="field-label" for="ak-label">Label</label>' +
            '<input type="text" id="ak-label" value="' + esc(k.label || '') + '" placeholder="optional"></div>' +
        '</div>' +
        '<div class="field"><label class="field-label" for="ak-key">Secret Key</label>' +
          '<div class="api-key-wrap"><input type="password" id="ak-key" class="api-key-input" value="' + esc(k.key) + '" placeholder="' + (isEdit ? '(unchanged — leave blank to keep)' : 'required') + '" autocomplete="new-password">' +
          '<button class="reveal-btn" data-action="toggleKeyField()">show</button></div>' +
          '<div class="field-hint">' + (isEdit ? 'Stored securely; reveal via the list if you forget it.' : 'Generate something random — e.g. openssl rand -hex 24.') + '</div></div>' +
        '<div class="hstack">' +
          '<div class="field" style="flex:1"><label class="field-label" for="ak-rpm">Requests / minute</label>' +
            '<input type="number" id="ak-rpm" min="1" value="' + (k.rpm || '') + '" placeholder="unlimited"></div>' +
          '<div class="field" style="flex:1"><label class="field-label" for="ak-daily">Daily budget (USD)</label>' +
            '<input type="number" id="ak-daily" min="0" step="0.01" value="' + (k.daily_budget_usd || '') + '" placeholder="none"></div>' +
          '<div class="field" style="flex:1"><label class="field-label" for="ak-monthly">Monthly budget (USD)</label>' +
            '<input type="number" id="ak-monthly" min="0" step="0.01" value="' + (k.monthly_budget_usd || '') + '" placeholder="none"></div>' +
        '</div>' +
        '<div class="field"><label class="field-label" for="ak-allowlist">Model allowlist (comma-separated logical IDs)</label>' +
          '<input type="text" id="ak-allowlist" value="' + esc((k.model_allowlist || []).join(', ')) + '" placeholder="empty = all models"></div>' +
        '<label class="field-label" style="display:flex;align-items:center;gap:10px;cursor:pointer;margin:0">' +
          '<input type="checkbox" id="ak-enabled"' + (k.enabled === false ? '' : ' checked') + '> Enabled</label>' +
      '</div>' +
      '<div class="modal-actions">' +
        '<button class="secondary" data-cancel>Cancel</button>' +
        '<button class="primary" data-ok>' + (isEdit ? 'Save' : 'Create') + '</button></div>',
    onMount: (ov) => {
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
      ov.querySelector('[data-ok]').onclick = () => saveApiKey(existingId, ov);
    },
  });
}

function toggleKeyField() {
  const input = document.getElementById('ak-key');
  input.type = input.type === 'password' ? 'text' : 'password';
}

async function saveApiKey(existingId, overlay) {
  const id = document.getElementById('ak-id').value.trim();
  if (!id) { toast('Key ID is required', 'error'); return; }
  const payload = {
    id,
    label: document.getElementById('ak-label').value.trim(),
    key: document.getElementById('ak-key').value.trim(),
    rpm: document.getElementById('ak-rpm').value === '' ? null : parseInt(document.getElementById('ak-rpm').value, 10),
    daily_budget_usd: document.getElementById('ak-daily').value === '' ? null : parseFloat(document.getElementById('ak-daily').value),
    monthly_budget_usd: document.getElementById('ak-monthly').value === '' ? null : parseFloat(document.getElementById('ak-monthly').value),
    model_allowlist: document.getElementById('ak-allowlist').value.split(',').map(s => s.trim()).filter(Boolean),
    enabled: document.getElementById('ak-enabled').checked,
  };
  if (!payload.key && !existingId) { toast('Secret key is required for new keys', 'error'); return; }
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.api_keys = data.server.api_keys || [];
    const idx = data.server.api_keys.findIndex(k => k.id === existingId);
    if (idx >= 0) {
      const prev = data.server.api_keys[idx];
      data.server.api_keys[idx] = { ...prev, ...payload, key: payload.key || prev.key };
    } else if (data.server.api_keys.some(k => k.id === id)) {
      throw new Error('Key ID already exists');
    } else {
      data.server.api_keys.push(payload);
    }
  });
  if (ok) {
    toast(existingId ? 'Key updated' : 'Key created', 'success');
    closeModal(overlay);
    loadAccessKeys();
  }
}

async function revealApiKey(keyId) {
  try {
    const r = await fetch('/admin/config/server-key/' + encodeURIComponent(keyId) + '/reveal', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) { toast(d.error || 'Failed to reveal', 'error'); return; }
    await navigator.clipboard.writeText(d.key);
    toast('Key copied to clipboard', 'success');
  } catch(e) { toast('Could not copy', 'error'); }
}

async function toggleApiKeyEnabled(keyId) {
  const { ok } = await saveConfigSection(data => {
    const k = (data.server.api_keys || []).find(x => x.id === keyId);
    if (k) k.enabled = k.enabled === false;
  });
  if (ok) { toast('Key updated', 'success'); loadAccessKeys(); }
}

async function deleteApiKey(keyId) {
  const confirmed = await showConfirm('Delete API Key', 'Delete key "' + keyId + '"? Clients using it will lose access immediately.');
  if (!confirmed) return;
  const { ok } = await saveConfigSection(data => {
    data.server.api_keys = (data.server.api_keys || []).filter(k => k.id !== keyId);
  });
  if (ok) { toast('Key deleted', 'success'); loadAccessKeys(); }
}

function clearLegacyKey() {
  window.__lgClearLegacyKey__ = true;
  document.getElementById('settings-api-key').value = '';
  toast('Legacy key will be removed on Save', 'info');
}

// ---------- circuit breaker ----------
async function loadCircuitSettings() {
  const data = await getSettingsConfig();
  if (!data) { loadError('settings'); return; }
  document.getElementById('settings-circuit-enabled').checked = data.server?.circuit_breaker_enabled === true;
  document.getElementById('settings-circuit-threshold').value = data.server?.circuit_breaker_threshold || 3;
  const backoff = data.server?.circuit_breaker_backoff_s || 60;
  const sel = document.getElementById('settings-circuit-backoff');
  if (sel) sel.value = String(backoff);
  toggleCircuitSettings();
  refreshCircuitStatus();
}

function toggleCircuitSettings() {
  const enabled = document.getElementById('settings-circuit-enabled').checked;
  document.getElementById('circuit-options').classList.toggle('dimmed', !enabled);
}

async function saveCircuitSettings() {
  const enabled = document.getElementById('settings-circuit-enabled').checked;
  const threshold = parseInt(document.getElementById('settings-circuit-threshold').value, 10) || 3;
  const backoff = parseInt(document.getElementById('settings-circuit-backoff').value, 10) || 60;
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.circuit_breaker_enabled = enabled;
    data.server.circuit_breaker_threshold = threshold;
    data.server.circuit_breaker_backoff_s = backoff;
  });
  if (ok) toast('Circuit breaker settings saved', 'success');
  else toast('Failed to save circuit breaker settings', 'error');
}

async function refreshCircuitStatus() {
  const el = document.getElementById('circuit-status');
  try {
    const { ok, data } = await apiFetch('/admin/circuit', { silent: true });
    if (!ok) return;
    const open = Object.entries(data.circuits || {}).filter(([, c]) => c.open);
    if (!open.length) { el.textContent = 'No circuits open.'; return; }
    el.innerHTML = open.map(([k, c]) =>
      '<span class="badge badge-red">' + esc(k) + ' · ' + Math.ceil(c.open_remaining_s) + 's</span>'
    ).join(' ') + ' <button class="secondary btn-sm" data-action="resetAllCircuits()">close</button>';
  } catch(e) { /* silent */ }
}

async function resetAllCircuits() {
  const confirmed = await showConfirm('Close All Circuits', 'Reset every open circuit? Backends become eligible for routing immediately.');
  if (!confirmed) return;
  try {
    await fetch('/admin/circuit/reset', { method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' } });
    toast('All circuits closed', 'success');
    refreshCircuitStatus();
  } catch(e) { toast('Failed to reset circuits', 'error'); }
}

// ---------- warmth heatmap ----------
async function loadWarmthHeatmap() {
  const el = document.getElementById('warmth-heatmap');
  const summary = document.getElementById('warmth-summary');
  if (!el) return;
  try {
    const { ok, data } = await apiFetch('/admin/warmth', { silent: true });
    if (!ok) { el.innerHTML = '<p class="field-hint">Warmth registry unavailable.</p>'; return; }
    const warmth = data || {};
    const fps = Object.keys(warmth);
    if (!fps.length) {
      el.innerHTML = '<p class="field-hint">No warmth data yet — requests on cache-capable backends warm fingerprints automatically.</p>';
      summary.textContent = '';
      return;
    }
    // Columns = distinct backends (up to 8), rows = fingerprints (up to 10).
    const backendSet = new Set();
    fps.forEach(fp => Object.keys(warmth[fp]).forEach(bk => backendSet.add(bk)));
    const backends = [...backendSet].slice(0, 8);
    const rows = fps.slice(0, 10);
    el.style.gridTemplateColumns = '150px repeat(' + backends.length + ', 1fr)';
    let html = '<div></div>' + backends.map(b => '<div class="hm-label">' + esc(b.split(':')[0]) + '</div>').join('');
    let warmTotal = 0, entries = 0;
    rows.forEach(fp => {
      html += '<div class="hm-label" title="' + esc(fp) + '">' + esc(fp.slice(0, 14)) + '…</div>';
      backends.forEach(bk => {
        const e = warmth[fp][bk];
        if (!e) { html += '<div class="hm-cell cold"></div>'; return; }
        entries++;
        const rate = e.last_hit_rate || 0;
        if (rate >= 0.4) warmTotal++;
        const color = rate >= 0.8 ? 'var(--green)' : rate >= 0.4 ? 'var(--yellow)' : 'var(--blue)';
        html += '<div class="hm-cell" style="background:' + color + '" title="' + esc(fp.slice(0, 20)) + '… on ' + esc(bk) + ' — ' + Math.round(rate * 100) + '% hit · ' + e.hit_count + 'h/' + e.miss_count + 'm"></div>';
      });
    });
    el.innerHTML = html;
    summary.textContent = rows.length + ' fingerprints · ' + backends.length + ' backends · ' + (entries ? Math.round(warmTotal / entries * 100) : 0) + '% warm cells';
  } catch(e) { el.innerHTML = '<p class="field-hint">Warmth registry unavailable.</p>'; }
}

// ---------- alerts ----------
async function loadAlertSettings() {
  const data = await getSettingsConfig();
  if (!data) { loadError('settings'); return; }
  document.getElementById('settings-alert-url').value = data.server?.alert_webhook_url || '';
}

async function saveAlertSettings() {
  const url = document.getElementById('settings-alert-url').value.trim();
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.alert_webhook_url = url || null;
  });
  if (ok) toast('Alert settings saved', 'success');
  else toast('Failed to save alert settings', 'error');
}

async function sendTestAlert() {
  const { ok } = await saveConfigSection(data => {
    const url = document.getElementById('settings-alert-url').value.trim();
    data.server = data.server || {};
    data.server.alert_webhook_url = url || null;
  });
  if (!ok) { toast('Save the URL first', 'error'); return; }
  try {
    const r = await fetch('/admin/alerts/test', { method: 'POST' });
    const d = await r.json();
    toast(d.ok ? 'Test alert sent' : (d.error || 'Failed to send test alert'), d.ok ? 'success' : 'error');
  } catch(e) { toast('Failed to send test alert', 'error'); }
}

// ---------- response cache admin (C3) ----------
async function loadCacheAdmin() {
  try {
    const { ok, data } = await apiFetch('/admin/cache', { silent: true });
    if (!ok) return;
    const el = document.getElementById('cache-admin-status');
    if (el) {
      const on = data.enabled ? '<span class="badge badge-green">enabled</span>' : '<span class="badge badge-gray">disabled</span>';
      el.innerHTML = on + ' · ' + (data.entries || 0) + ' entries · ' + (data.total_hits || 0) + ' lifetime hits';
    }
    const sel = document.getElementById('settings-resp-cache-enabled');
    if (sel) sel.value = data.enabled ? 'true' : 'false';
  } catch(e) {}
}
async function saveCacheEnabledFlag() {
  const enabled = document.getElementById('settings-resp-cache-enabled').value === 'true';
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.response_cache_enabled = enabled;
  });
  if (ok) toast('Response cache ' + (enabled ? 'enabled' : 'disabled'), 'success');
  else toast('Failed to update response cache setting', 'error');
  loadCacheAdmin();
}
async function clearResponseCache() {
  if (!await confirm2('Clear all cached responses?')) return;
  const r = await fetch('/admin/cache', { method: 'DELETE' });
  if (r.ok) { toast('Cache cleared', 'success'); loadCacheAdmin(); }
  else toast('Failed to clear cache', 'error');
}

// ---------- config backups ----------
async function loadBackups() {
  const wrap = document.getElementById('backups-list');
  try {
    const { ok, data } = await apiFetch('/admin/backups', { silent: true });
    if (!ok) { wrap.innerHTML = '<p class="field-hint">Backups unavailable.</p>'; return; }
    const backups = data.backups || [];
    if (!backups.length) { wrap.innerHTML = '<p class="field-hint">No backups yet — they are created before each save.</p>'; return; }
    const fmtSize = n => n < 1024 ? n + ' B' : (n / 1024).toFixed(1) + ' KB';
    const fmtDate = ts => new Date(ts * 1000).toLocaleString();
    wrap.innerHTML = backups.map(b =>
      '<div class="key-row' + (b.current ? '' : ' dimmed') + '">' +
        '<div class="key-main">' +
          '<div class="hstack" style="gap:8px;align-items:center">' +
            '<span class="badge ' + (b.current ? 'badge-green' : 'badge-gray') + '">' + (b.current ? 'current' : 'backup ' + b.slot) + '</span>' +
            '<code style="font-size:0.76rem">' + esc(b.name) + '</code>' +
            '<span class="filter-hint">' + fmtSize(b.size) + ' · ' + fmtDate(b.mtime) + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="hstack" style="gap:8px">' +
          (b.current ? '' : '<button class="secondary btn-sm" data-slot="' + b.slot + '" data-action="restoreBackup(this.dataset.slot)">Restore</button>') +
        '</div>' +
      '</div>'
    ).join('');
  } catch(e) { wrap.innerHTML = '<p class="field-hint">Backups unavailable.</p>'; }
}

async function restoreBackup(slot) {
  const confirmed = await showConfirm('Restore Backup', 'Replace the current config with backup slot ' + slot + '? The current config is preserved as a new backup before the swap.');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/backups/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slot: parseInt(slot, 10) }) });
    const d = await r.json();
    if (r.ok) {
      // U7: refreshing only the backup list left every other section stale —
      // a later "Save" in any section would have silently overwritten the
      // restored values against phantom state. Reload the whole page.
      toast('Config restored from backup — reloading…', 'success');
      setTimeout(() => location.reload(), 700);
    } else toast(d.error || 'Failed to restore', 'error');
  } catch(e) { toast('Failed to restore', 'error'); }
}

// ---------- time-based routing ----------
let timeRoutingState = { modelId: null, tr: null };

async function loadTimeRouting() {
  const select = document.getElementById('tr-model-select');
  const editor = document.getElementById('tr-editor');
  try {
    const { data } = await fetchJSON('/admin/config');
    const selected = select.value;
    select.innerHTML = '<option value="">Select a model…</option>' +
      (data.models || []).map(m =>
        `<option value="${escAttr(m.id)}">${esc(m.display_name || m.id)}</option>`
      ).join('');
    if (selected) select.value = selected;
    const modelId = select.value;
    if (!modelId) {
      editor.classList.add('dimmed');
      timeRoutingState = { modelId: null, tr: null };
      return;
    }
    timeRoutingState.modelId = modelId;
    const res = await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-routing');
    timeRoutingState.tr = res.data || { enabled: false, timezone: 'UTC', slots: [] };
    renderTimeRoutingEditor();
  } catch(e) {
    toast('Failed to load time routing: ' + e.message, 'error');
  }
}

function renderTimeRoutingEditor() {
  const tr = timeRoutingState.tr;
  if (!tr) return;
  const editor = document.getElementById('tr-editor');
  editor.classList.remove('dimmed');
  document.getElementById('tr-enabled').checked = tr.enabled !== false;
  document.getElementById('tr-timezone').value = tr.timezone || 'UTC';
  toggleTimeRouting();
  renderTimeSlots(tr.slots || []);
}

function toggleTimeRouting() {
  const enabled = document.getElementById('tr-enabled').checked;
  document.getElementById('tr-options').classList.toggle('dimmed', !enabled);
}

function renderTimeSlots(slots) {
  const wrap = document.getElementById('tr-slots');
  if (!slots.length) {
    wrap.innerHTML = '<p class="field-hint">No time slots yet. Add one to restrict which backends serve this model during a period.</p>';
    return;
  }
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  wrap.innerHTML = slots.map(slot => {
    const dayStr = slot.days_of_week && slot.days_of_week.length
      ? slot.days_of_week.map(d => days[d]).join(', ')
      : 'Every day';
    const hr = String(slot.start_hour).padStart(2, '0') + ':00 – ' + String(slot.end_hour).padStart(2, '0') + ':00';
    const provs = slot.active_providers && slot.active_providers.length
      ? slot.active_providers.join(', ')
      : '(all backends)';
    return '<div class="tr-slot-row' + (slot.enabled === false ? ' dimmed' : '') + '">' +
      '<div class="tr-slot-main">' +
        '<div class="hstack" style="gap:8px;align-items:center">' +
          '<span class="badge ' + (slot.enabled === false ? 'badge-gray' : 'badge-green') + '">' + (slot.enabled === false ? 'off' : 'on') + '</span>' +
          '<strong>' + esc(slot.name || slot.id) + '</strong>' +
          '<span class="filter-hint">' + hr + '</span>' +
          '<span class="filter-hint">·</span>' +
          '<span class="filter-hint">' + esc(dayStr) + '</span>' +
        '</div>' +
        '<div class="filter-hint" style="margin-top:4px">Providers: <code>' + esc(provs) + '</code></div>' +
      '</div>' +
      '<div class="hstack" style="gap:8px">' +
        '<button class="secondary btn-sm" data-slot="' + escAttr(slot.id) + '" data-action="showEditSlotModal(this.dataset.slot)">Edit</button>' +
        '<button class="danger btn-sm" data-slot="' + escAttr(slot.id) + '" data-action="deleteTimeSlot(this.dataset.slot)">Delete</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

function _slotHoursOptions(selected) {
  return Array.from({ length: 24 }, (_, h) =>
    '<option value="' + h + '"' + (selected === h ? ' selected' : '') + '>' + String(h).padStart(2, '0') + ':00</option>'
  ).join('');
}

function _slotDaysCheckboxes(selectedDays) {
  const sel = new Set(selectedDays || []);
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  return days.map((d, i) =>
    '<label class="cap-item"><input type="checkbox" data-day="' + i + '"' + (sel.has(i) ? ' checked' : '') + '> ' + d + '</label>'
  ).join('');
}

async function _slotProviderOptions(selected) {
  const { data } = await fetchJSON('/admin/config');
  const model = (data.models || []).find(m => m.id === timeRoutingState.modelId);
  const backends = model && model.backends || [];
  const sel = new Set(selected || []);
  if (!backends.length) {
    return '<p class="field-hint">This model has no backends configured.</p>';
  }
  return backends.map(b => {
    const key = b.provider + ':' + b.model;
    return '<label class="cap-item"><input type="checkbox" data-provider="' + escAttr(key) + '"' + (sel.has(key) ? ' checked' : '') + '> <code>' + esc(key) + '</code></label>';
  }).join('');
}

function showAddSlotModal() {
  openTimeSlotModal(null);
}
function showEditSlotModal(slotId) {
  const slot = (timeRoutingState.tr.slots || []).find(s => s.id === slotId);
  if (!slot) return;
  openTimeSlotModal(slot);
}

async function openTimeSlotModal(existing) {
  const isEdit = !!existing;
  const slot = existing || { name: '', start_hour: 0, end_hour: 23, days_of_week: [], active_providers: [], enabled: true };
  const providerOptions = await _slotProviderOptions(slot.active_providers);
  const overlay = openModal({
    title: isEdit ? 'Edit Time Slot' : 'Add Time Slot',
    widthClass: 'modal-lg',
    bodyHtml:
      '<div class="field-group">' +
        '<div class="field"><label class="field-label" for="slot-name">Name</label>' +
          '<input type="text" id="slot-name" value="' + esc(slot.name || '') + '" placeholder="e.g. Peak hours"></div>' +
        '<div class="hstack"><div class="field" style="flex:1">' +
          '<label class="field-label" for="slot-start">Start</label>' +
          '<select id="slot-start">' + _slotHoursOptions(slot.start_hour) + '</select></div>' +
          '<div class="field" style="flex:1">' +
          '<label class="field-label" for="slot-end">End</label>' +
          '<select id="slot-end">' + _slotHoursOptions(slot.end_hour) + '</select></div></div>' +
        '<div class="field"><label class="field-label">Days of week (none = every day)</label>' +
          '<div class="cap-grid" id="slot-days">' + _slotDaysCheckboxes(slot.days_of_week) + '</div></div>' +
        '<div class="field"><label class="field-label">Active providers (none = all backends)</label>' +
          '<div class="cap-grid" id="slot-providers">' + providerOptions + '</div></div>' +
        '<label class="field-label" style="display:flex;align-items:center;gap:10px;cursor:pointer;margin:0">' +
          '<input type="checkbox" id="slot-enabled"' + (slot.enabled === false ? '' : ' checked') + '> Enabled</label>' +
      '</div>' +
      '<div class="modal-actions">' +
        '<button class="secondary" data-cancel>Cancel</button>' +
        '<button class="primary" data-ok>' + (isEdit ? 'Save' : 'Add') + '</button></div>',
    onMount: (ov) => {
      ov.querySelector('[data-cancel]').onclick = () => closeModal(ov);
      ov.querySelector('[data-ok]').onclick = () => {
        const payload = _collectSlotPayload(existing);
        if (!payload) return;
        if (isEdit) updateTimeSlot(existing.id, payload, ov);
        else createTimeSlot(payload, ov);
      };
    },
  });
}

function _collectSlotPayload(existing) {
  const name = document.getElementById('slot-name').value.trim();
  const start = parseInt(document.getElementById('slot-start').value, 10);
  const end = parseInt(document.getElementById('slot-end').value, 10);
  const days = [...document.querySelectorAll('#slot-days input[data-day]:checked')].map(cb => parseInt(cb.dataset.day, 10));
  const providers = [...document.querySelectorAll('#slot-providers input[data-provider]:checked')].map(cb => cb.dataset.provider);
  const enabled = document.getElementById('slot-enabled').checked;
  const payload = { name, start_hour: start, end_hour: end, days_of_week: days, active_providers: providers, enabled };
  if (existing && existing.id) payload.id = existing.id;
  return payload;
}

async function createTimeSlot(payload, overlay) {
  const modelId = timeRoutingState.modelId;
  try {
    const { ok, data } = await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-slots', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (!ok) { toast('Failed to create time slot', 'error'); return; }
    toast('Time slot added', 'success');
    closeModal(overlay);
    timeRoutingState.tr = (await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-routing')).data;
    renderTimeRoutingEditor();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function updateTimeSlot(slotId, payload, overlay) {
  const modelId = timeRoutingState.modelId;
  try {
    const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-slots/' + encodeURIComponent(slotId), {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    if (!ok) { toast('Failed to update time slot', 'error'); return; }
    toast('Time slot updated', 'success');
    closeModal(overlay);
    timeRoutingState.tr = (await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-routing')).data;
    renderTimeRoutingEditor();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function deleteTimeSlot(slotId) {
  const modelId = timeRoutingState.modelId;
  const confirmed = await showConfirm('Delete Time Slot', 'Delete this time slot?');
  if (!confirmed) return;
  try {
    const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-slots/' + encodeURIComponent(slotId), {
      method: 'DELETE',
    });
    if (!ok) { toast('Failed to delete time slot', 'error'); return; }
    toast('Time slot deleted', 'success');
    timeRoutingState.tr = (await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-routing')).data;
    renderTimeRoutingEditor();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function saveTimeRoutingConfig() {
  const modelId = timeRoutingState.modelId;
  if (!modelId) { toast('Select a model first', 'error'); return; }
  const tr = {
    enabled: document.getElementById('tr-enabled').checked,
    timezone: document.getElementById('tr-timezone').value.trim() || 'UTC',
    slots: (timeRoutingState.tr && timeRoutingState.tr.slots) || [],
  };
  try {
    const { ok } = await fetchJSON('/admin/models/' + encodeURIComponent(modelId) + '/time-routing', {
      method: 'PUT',
      body: JSON.stringify(tr),
    });
    if (ok) {
      toast('Time routing saved', 'success');
      timeRoutingState.tr = tr;
    } else {
      toast('Failed to save time routing', 'error');
    }
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', loadSettingsPage);