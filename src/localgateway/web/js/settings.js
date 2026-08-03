// settings.js — settings page logic
function loadSettingsPage() {
  loadServerSettings();
  loadRoutingSettings();
  loadCacheAffinitySettings();
  loadProbeSettings();
  loadDisplaySettings();
}

async function loadServerSettings() {
  resetLoadError();
  const { ok, data } = await apiFetch('/admin/config', { silent: true });
  if (!ok) { loadError('settings'); return; }
  const port = data.server?.port || 3456;
  document.getElementById('settings-port').value = port;
  const keyInput = document.getElementById('settings-api-key');
  keyInput.value = data.server?.api_key || '';
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
    data.server.api_key = apiKey || null;
  });
  if (ok) toast('Server settings saved. Restart required for port changes.', 'success');
  else toast('Failed to save server settings', 'error');
}

async function loadRoutingSettings() {
  const { ok, data } = await apiFetch('/admin/config', { silent: true });
  if (!ok) { loadError('settings'); return; }
  const mode = data.server?.routing_mode || 'explore';
  const decay = data.server?.routing_decay || 0.4;
  document.getElementById('settings-routing-mode').value = mode;
  document.getElementById('settings-routing-decay').value = decay;
  updateDecayViz();
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
  const { ok } = await saveConfigSection(data => {
    data.server = data.server || {};
    data.server.routing_mode = mode;
    data.server.routing_decay = decay;
  });
  if (ok) toast('Routing settings saved', 'success');
  else toast('Failed to save routing settings', 'error');
}

async function loadCacheAffinitySettings() {
  const { ok, data } = await apiFetch('/admin/config', { silent: true });
  if (!ok) { loadError('settings'); return; }
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
  const { ok, data } = await apiFetch('/admin/config', { silent: true });
  if (!ok) { loadError('settings'); return; }
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
  const { ok, data } = await apiFetch('/admin/config', { silent: true });
  if (!ok) { loadError('settings'); return; }
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