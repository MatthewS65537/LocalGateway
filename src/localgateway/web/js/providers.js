// providers.js — providers page logic
let discoveryCache = {};
// Tracks the open "Import Models" modal so async fetch completion can update
// its body (and skip updating if the user closed it or opened another).
let _discoveryModal = null;
// C8: per-provider health summary from /admin/health (success rate, latency,
// circuit/snooze state) — the providers page previously showed zero health
// signal, which is what this page is really for.
let providerHealth = {};

async function loadProvidersPage() {
  await loadConfig();
  loadProviderHealth();
}
async function loadProviderHealth() {
  try {
    const { ok, data } = await apiFetch('/admin/health', { silent: true });
    if (ok) {
      providerHealth = data || {};
      renderProviders();
    }
  } catch(_) {}
}
async function loadConfig() {
  resetLoadError();
  const { ok, data } = await apiFetch('/admin/config');
  if (!ok) { loadError('providers'); return; }
  currentConfig = data;
  renderProviders();
}
// Save a providers-page mutation with optimistic-concurrency safety. The
// mutate callback applies the edit to a *fresh* GET copy; saveConfigSection
// retries on 409 (config changed since load) and invalidates the palette.
// On success we reload so currentConfig stays in sync with the server.
// Replaces the old direct-PUT saveCurrentConfig which raced on concurrent
// edits and skipped palette invalidation (B3/B4).
async function saveProvidersSection(mutate) {
  const res = await saveConfigSection(mutate);
  if (res.ok) await loadConfig();
  return res.ok;
}

function toggleReveal(elOrId, btn) {
  const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
  if (!el) return;
  if (el.type === 'password') { el.type = 'text'; btn.textContent = 'hide'; }
  else { el.type = 'password'; btn.textContent = 'show'; }
}

// ---- Add Provider modal ----
function addProviderFormHtml() {
  return '<div class="form-section" id="add-provider-form">'
    + '<div class="field"><label class="field-label" for="new-prov-id">ID</label><input type="text" id="new-prov-id" placeholder="openai, groq, openrouter"></div>'
    + '<div class="field"><label class="field-label" for="new-prov-name">Name</label><input type="text" id="new-prov-name" placeholder="Display name"></div>'
    + '<div class="field"><label class="field-label" for="new-prov-avatar">Avatar</label><input type="text" id="new-prov-avatar" placeholder="e.g. OG, GPT, ☁️" maxlength="8" title="Custom text for the avatar tile"><div class="field-hint">Custom text for the avatar tile.</div></div>'
    + '<div class="field"><label class="field-label" for="new-prov-url">Base URL</label><input type="text" id="new-prov-url" placeholder="https://api.openai.com/v1"></div>'
    + '<div class="field"><label class="field-label" for="new-prov-key">API Key</label><div class="api-key-wrap"><input type="password" id="new-prov-key" placeholder="sk-..."><button class="reveal-btn" data-action="toggleReveal(\'new-prov-key\', this)">show</button></div><div class="field-hint">Stored securely in config.json.</div></div>'
    + '<div class="field"><label class="field-label" for="new-prov-timeout">Timeout (s)</label><input type="number" id="new-prov-timeout" value="120"></div>'
    + '<div class="actions"><button data-action="addProvider()">Add</button><button class="secondary" data-action="closeModal()">Cancel</button></div>'
    + '</div>';
}
function openAddProvider() {
  openModal({
    title: 'Add Provider',
    bodyHtml: addProviderFormHtml(),
    widthClass: 'modal-lg',
    onMount: () => { const f = document.getElementById('new-prov-id'); if (f) f.focus(); },
  });
}
async function addProvider() {
  const id = document.getElementById('new-prov-id').value.trim();
  const name = document.getElementById('new-prov-name').value.trim();
  const avatar = document.getElementById('new-prov-avatar').value.trim();
  const baseUrl = document.getElementById('new-prov-url').value.trim();
  const apiKey = document.getElementById('new-prov-key').value.trim();
  const timeout = parseFloat(document.getElementById('new-prov-timeout').value) || 120;
  if (!id || !baseUrl) { toast('ID and Base URL are required', 'error'); return; }
  if (currentConfig.providers.some(p => p.id === id)) { toast('Provider ID already exists', 'error'); return; }
  const ok = await saveProvidersSection(data => {
    // Re-check against the fresh copy in case a concurrent edit added it.
    if (data.providers.some(p => p.id === id)) return;
    data.providers.push({ id, name: name || id, base_url: baseUrl, api_key: apiKey, headers: {}, timeout, avatar });
  });
  if (ok) { closeModal(); toast('Provider added', 'success'); }
}

// ---- Edit Provider modal ----
function editProviderFormHtml(p) {
  return '<div class="form-section" data-provider-id="'+escAttr(p.id)+'">'
    + '<div class="form-row"><label>ID (slug)</label><input type="text" class="edit-id" value="'+esc(p.id)+'" title="Changing the slug updates all backend references"></div>'
    + '<div class="form-row"><label>Name</label><input type="text" class="edit-name" value="'+esc(p.name||p.id)+'"></div>'
    + '<div class="form-row"><label>Avatar</label><input type="text" class="edit-avatar" value="'+esc(p.avatar||'')+'" placeholder="e.g. OG, GPT, ☁️" maxlength="8" title="Custom text for the avatar tile"></div>'
    + '<div class="form-row"><label>Base URL</label><input type="text" class="edit-url" value="'+esc(p.base_url)+'"></div>'
    + '<div class="form-row"><label>API Key</label><div class="api-key-wrap"><input type="password" class="edit-key" value="" data-provider="'+escAttr(p.id)+'" placeholder="'+esc(p.api_key ? '•••••• (unchanged)' : 'no key set')+'" autocomplete="new-password"><button class="reveal-btn" data-action="revealEditKey(this)">show</button></div></div>'
    + '<div class="form-row"><label>Timeout (s)</label><input type="number" class="edit-timeout" value="'+(p.timeout||120)+'"></div>'
    + '<div class="actions" data-edit-actions><button data-action="saveProviderEdit(this)">Save Changes</button><button class="secondary" data-action="cancelEditProvider(this)">Cancel</button></div>'
    + '</div>';
}
function editProvider(id) {
  const p = currentConfig.providers.find(x => x.id === id);
  if (!p) return;
  openModal({
    title: 'Edit provider: ' + (p.name || p.id),
    bodyHtml: editProviderFormHtml(p),
    widthClass: 'modal-lg',
  });
}
function cancelEditProvider(btn) {
  closeModal();
}
async function deleteProvider(id) {
  if (!await confirm2('Delete provider "'+id+'"?')) return;
  const ok = await saveProvidersSection(data => {
    data.providers = data.providers.filter(p => p.id !== id);
  });
  if (ok) toast('Provider deleted', 'info');
}
async function revealEditKey(btn) {
  const wrap = btn.closest('.api-key-wrap');
  const input = wrap.querySelector('.edit-key');
  const pid = input.dataset.provider;
  if (input.type === 'password') {
    try {
      const r = await fetch('/admin/config/api-key/' + encodeURIComponent(pid) + '/reveal', { method: 'POST' });
      const d = await r.json();
      if (r.ok) { input.value = d.api_key || ''; input.type = 'text'; btn.textContent = 'hide'; }
      else toast('Cannot reveal: ' + (d.error || r.status), 'error');
    } catch(e) { toast('Error: ' + e.message, 'error'); }
  } else {
    input.type = 'password'; btn.textContent = 'show';
  }
}

async function saveProviderEdit(btn) {
  const section = btn.closest('.form-section');
  const id = section.dataset.providerId;
  const val = cls => section.querySelector(cls).value.trim();
  const newId = val('.edit-id');
  const newName = val('.edit-name') || newId;
  const newUrl = val('.edit-url');
  const keyInput = section.querySelector('.edit-key');
  const typedKey = keyInput ? keyInput.value.trim() : '';
  const keyChanged = typedKey !== '';
  const newTimeout = parseFloat(val('.edit-timeout')) || 120;
  const newAvatar = val('.edit-avatar');

  // Apply the non-slug field edits to a config copy. Used for both the
  // plain-save and the rename paths below.
  const applyEdits = data => {
    const p = data.providers.find(x => x.id === id);
    if (!p) return;
    p.name = newName;
    p.base_url = newUrl;
    if (keyChanged) p.api_key = typedKey;
    p.timeout = newTimeout;
    p.avatar = newAvatar;
  };

  if (newId && newId !== id) {
    // Slug rename: confirm inline (inside this modal so the form values
    // survive a "Back"). We avoid confirm2() here because openModal() closes
    // the current modal first, which would discard the edit form.
    if (currentConfig.providers.some(o => o.id === newId)) {
      toast('Provider ID "'+newId+'" already exists', 'error');
      return;
    }
    const actions = section.querySelector('[data-edit-actions]');
    if (!actions) return;
    const savedActions = actions.innerHTML;
    actions.innerHTML =
      '<div class="modal-sub" style="flex:1;color:var(--text)">Rename provider "'+esc(id)+'" → "'+esc(newId)+'"? This updates all models, pricing, usage data, and snooze state keyed to it.</div>'
      + '<button data-act="back">Back</button>'
      + '<button class="danger" data-act="confirm">Confirm Rename</button>';
    actions.querySelector('[data-act="back"]').onclick = () => { actions.innerHTML = savedActions; };
    actions.querySelector('[data-act="confirm"]').onclick = async () => {
      // Save non-slug field changes first (still under old id). Use
      // saveConfigSection directly so the edit form stays intact on failure.
      const saved = await saveConfigSection(applyEdits);
      if (!saved.ok) { actions.innerHTML = savedActions; return; }
      try {
        const r = await fetch('/admin/rename', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ type: 'provider', old_id: id, new_id: newId }),
        });
        const res = r.ok ? await r.json() : null;
        if (!r.ok) {
          toast('Rename failed: ' + (res && res.error || 'unknown'), 'error');
          actions.innerHTML = savedActions;
          return;
        }
        const parts = [];
        if (res.backend_refs_updated) parts.push(res.backend_refs_updated + ' backend refs');
        if (res.pricing_keys_moved) parts.push(res.pricing_keys_moved + ' pricing keys');
        if (res.usage_rows_updated) parts.push(res.usage_rows_updated + ' usage rows');
        if (res.snooze_keys_moved) parts.push(res.snooze_keys_moved + ' snooze keys');
        if (parts.length) toast('Migrated ' + parts.join(', '), 'info');
      } catch(e) {
        toast('Rename error: ' + e.message, 'error');
        actions.innerHTML = savedActions;
        return;
      }
      closeModal();
      await loadConfig();
      toast('Provider updated', 'success');
    };
  } else {
    // No slug change: save all field edits. On failure the form stays intact
    // so the user can retry without re-typing.
    const res = await saveConfigSection(applyEdits);
    if (!res.ok) return;
    closeModal();
    await loadConfig();
    toast('Provider updated', 'success');
  }
}
function renderProviders() {
  const list = document.getElementById('providers-list');
  if (!currentConfig || !currentConfig.providers || currentConfig.providers.length === 0) {
    list.innerHTML = '<div class="empty">No providers configured. Click "+ Add" to get started.</div>';
    return;
  }
  list.innerHTML = currentConfig.providers.map(p => {
    const enabled = p.enabled !== false;
    const health = _providerHealthDot(p.id);
    return '<div class="provider-row'+(enabled ? '' : ' is-disabled')+'">'
      + '<div class="provider-info hstack">'+providerAvatar(p.id, 30, p.avatar)
      + '<span><span class="provider-name">'+esc(p.name||p.id)+'</span><code class="provider-id">'+esc(p.id)+'</code><div class="provider-url">'+esc(p.base_url)+'</div></span>'
      + health
      + '</div>'
      + '<button class="icon-btn" data-id="'+escAttr(p.id)+'" data-action="discoverProvider(this.dataset.id)" title="Fetch models served by this provider and import them">Import Models</button>'
      + '<button class="icon-btn" data-id="'+escAttr(p.id)+'" data-action="editProvider(this.dataset.id)">Edit</button>'
      + '<button class="icon-btn '+(enabled?'danger':'success')+'" data-id="'+escAttr(p.id)+'" data-action="toggleProviderEnabled(this.dataset.id)" title="'+(enabled?'Disable provider':'Enable provider')+'">'+(enabled?'Disable':'Enable')+'</button>'
      + '<button class="icon-btn danger" data-id="'+escAttr(p.id)+'" data-action="deleteProvider(this.dataset.id)">Delete</button>'
      + '</div>';
  }).join('');
}

// C8: aggregate /admin/health backend stats into a per-provider status dot.
function _providerHealthDot(providerId) {
  if (!providerHealth || !providerHealth.backends) return '';
  const bks = (providerHealth.backends || []).filter(b => b.provider === providerId);
  if (!bks.length) return '';
  const stats = providerHealth.stats || {};
  const perBackend = bks.map(b => stats[b.provider + ':' + b.backend_model] || {});
  const requests = perBackend.reduce((s, x) => s + (x.requests || 0), 0);
  const failures = perBackend.reduce((s, x) => s + (x.failures || 0), 0);
  const latencies = perBackend.map(x => x.avg_latency_ms).filter(v => v != null);
  const avgLat = latencies.length ? Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length) : null;
  const openCircuits = bks.filter(b => {
    const c = ((providerHealth.circuit || {}).circuits || {});
    return c[b.provider + ':' + b.backend_model] && c[b.provider + ':' + b.backend_model].open;
  }).length;
  const snoozed = bks.filter(b => (b.cooldown_remaining || 0) > 0).length;
  let cls = 'ok', label;
  if (openCircuits > 0) { cls = 'fail'; label = openCircuits + ' circuit' + (openCircuits > 1 ? 's' : '') + ' open'; }
  else if (requests > 0 && failures / requests > 0.5) { cls = 'warn'; label = Math.round(failures / requests * 100) + '% failing'; }
  else { label = requests > 0 ? requests + ' reqs' : 'no traffic'; }
  if (snoozed > 0) label += ' · ' + snoozed + ' paused';
  if (avgLat != null) label += ' · ' + avgLat + 'ms';
  return '<span class="status-dot ' + cls + '" title="' + esc(label) + '" style="margin-left:10px;align-self:center"></span>';
}

function discoveryBodyHtml(providerId) {
  const cache = discoveryCache[providerId];
  if (!cache || cache.loading) return '<div class="empty">Fetching models from '+esc(providerId)+'…</div>';
  if (cache.error) return '<div class="empty" style="color:var(--red)">'+esc(cache.error)+'</div>';
  const models = cache.models || [];
  const rows = models.map(m => {
    const ctx = m.context_length ? ' · '+fmt(m.context_length,0)+' ctx' : '';
    return '<div class="provider-row discovery-row">'
      + '<div class="provider-info"><code>'+esc(m.id)+'</code><span class="provider-url" style="display:inline;margin-left:8px">'+esc(ctx)+'</span></div>'
      + '<button class="icon-btn" data-provider="'+escAttr(providerId)+'" data-model="'+escAttr(m.id)+'" data-action="addDiscoveredModel(this.dataset.provider, this.dataset.model)">+ Add</button>'
      + '</div>';
  }).join('');
  return '<div class="filter-hint" style="margin-bottom:10px">'+models.length+' model'+(models.length === 1 ? '' : 's')+' found. Adding creates a gateway model with the same ID (if missing) and attaches this provider at the next priority tier.</div>'
    + (rows || '<div class="empty">No models returned.</div>')
    + '<div class="modal-actions"><button class="secondary" data-action="closeModal()">Close</button></div>';
}

function _updateDiscoveryModal(providerId) {
  if (_discoveryModal && _discoveryModal.providerId === providerId && _discoveryModal.overlay.isConnected) {
    _discoveryModal.overlay.querySelector('.modal-body').innerHTML = discoveryBodyHtml(providerId);
  }
}

async function discoverProvider(id) {
  const overlay = openModal({
    title: 'Import models from ' + id,
    bodyHtml: discoveryBodyHtml(id),
    widthClass: 'modal-lg',
  });
  _discoveryModal = { providerId: id, overlay };
  if (discoveryCache[id] && !discoveryCache[id].error) return; // cached result already rendered
  discoveryCache[id] = { loading: true };
  _updateDiscoveryModal(id);
  try {
    const r = await fetch('/admin/providers/'+encodeURIComponent(id)+'/models');
    const data = await r.json();
    if (!r.ok) { discoveryCache[id] = { error: data.error || ('HTTP '+r.status) }; }
    else { discoveryCache[id] = { models: data.models || [] }; }
  } catch(e) { discoveryCache[id] = { error: e.message }; }
  _updateDiscoveryModal(id);
}

async function addDiscoveredModel(providerId, backendModel) {
  // Pre-check against the local cache for fast feedback; saveProvidersSection
  // re-checks against a fresh GET copy to handle concurrent edits.
  const local = currentConfig.models.find(m => m.id === backendModel);
  if (local && local.backends.some(b => b.provider === providerId && b.model === backendModel)) {
    toast('Provider already attached to '+backendModel, 'info');
    return;
  }
  const ok = await saveProvidersSection(data => {
    let m = data.models.find(x => x.id === backendModel);
    if (!m) { m = { id: backendModel, backends: [] }; data.models.push(m); }
    if (m.backends.some(b => b.provider === providerId && b.model === backendModel)) return;
    const nextPriority = m.backends.reduce((mx, b) => Math.max(mx, b.priority || 0), 0) + 1;
    m.backends.push({ provider: providerId, model: backendModel, priority: nextPriority, enabled: true });
  });
  if (ok) toast('Added '+providerId+':'+backendModel+' → '+backendModel, 'success');
}

async function toggleProviderEnabled(id) {
  const ok = await saveProvidersSection(data => {
    const p = data.providers.find(x => x.id === id);
    if (p) p.enabled = p.enabled === false ? true : false;
  });
  if (ok) {
    const p = currentConfig.providers.find(x => x.id === id);
    toast('Provider '+(p && p.enabled ? 'enabled' : 'disabled'), 'info');
  }
}

document.addEventListener('DOMContentLoaded', loadProvidersPage);
