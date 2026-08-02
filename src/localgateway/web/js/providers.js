// providers.js — providers page logic
let editingProvider = null;
let discoveryOpen = null;
let discoveryCache = {};

async function loadProvidersPage() {
  await loadConfig();
}
async function loadConfig() {
  try {
    const { data } = await fetchJSON('/admin/config');
    currentConfig = data;
    renderProviders();
  } catch(e) { console.error(e); }
}
async function saveCurrentConfig() {
  try {
    const r = await fetch('/admin/config', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(currentConfig),
    });
    if (!r.ok) {
      const err = await r.json().catch(()=>null);
      toast('Error: ' + ((err&&err.error) || 'Validation failed'), 'error');
      return false;
    }
    renderProviders();
    return true;
  } catch(e) { toast('Save failed: ' + e.message, 'error'); return false; }
}

function toggleReveal(elOrId, btn) {
  const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
  if (!el) return;
  if (el.type === 'password') { el.type = 'text'; btn.textContent = 'hide'; }
  else { el.type = 'password'; btn.textContent = 'show'; }
}
function toggleAddProvider() {
  const form = document.getElementById('add-provider-form');
  form.classList.toggle('hidden');
  if (!form.classList.contains('hidden')) {
    ['new-prov-id','new-prov-name','new-prov-avatar','new-prov-url','new-prov-key'].forEach(id => document.getElementById(id).value='');
    document.getElementById('new-prov-timeout').value = '120';
    document.getElementById('new-prov-id').focus();
  }
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
  currentConfig.providers.push({ id, name: name || id, base_url: baseUrl, api_key: apiKey, headers: {}, timeout, avatar });
  if (await saveCurrentConfig()) { toggleAddProvider(); toast('Provider added', 'success'); }
}
function toggleEditProvider(id) {
  editingProvider = (editingProvider === id) ? null : id;
  renderProviders();
}
function cancelEditProvider(btn) {
  const section = btn.closest('.form-section');
  if (section) toggleEditProvider(section.dataset.providerId);
}
async function deleteProvider(id) {
  if (!await confirm2('Delete provider "'+id+'"?')) return;
  currentConfig.providers = currentConfig.providers.filter(p => p.id !== id);
  await saveCurrentConfig();
  toast('Provider deleted', 'info');
}
async function revealEditKey(btn) {
  const wrap = btn.closest('.api-key-wrap');
  const input = wrap.querySelector('.edit-key');
  const pid = input.dataset.provider;
  if (input.type === 'password') {
    try {
      const r = await fetch('/admin/config/api-key/' + encodeURIComponent(pid));
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
  const p = currentConfig.providers.find(x => x.id === id);
  if (!p) return;
  const val = cls => section.querySelector(cls).value.trim();
  const newId = val('.edit-id');
  const newName = val('.edit-name') || newId;
  p.name = newName;
  p.base_url = val('.edit-url');
  const keyInput = section.querySelector('.edit-key');
  const typedKey = keyInput ? keyInput.value.trim() : '';
  const keyChanged = typedKey !== '';
  if (keyChanged) p.api_key = typedKey;
  p.timeout = parseFloat(val('.edit-timeout')) || 120;
  p.avatar = val('.edit-avatar');

  // Slug rename: delegate to server-side cascade endpoint.
  if (newId && newId !== id) {
    if (currentConfig.providers.some(o => o.id === newId && o !== p)) {
      toast('Provider ID "'+newId+'" already exists', 'error');
      return;
    }
    if (!await confirm2('Rename provider "'+id+'" → "'+newId+'"? This updates all models, pricing, usage data, and snooze state keyed to it.')) {
      return;
    }
    // Save non-slug field changes first (still under old ID).
    if (!await saveCurrentConfig()) return;
    try {
      const r = await fetch('/admin/rename', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ type: 'provider', old_id: id, new_id: newId }),
      });
      const res = r.ok ? await r.json() : null;
      if (!r.ok) {
        toast('Rename failed: ' + (res && res.error || 'unknown'), 'error');
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
      return;
    }
    await loadConfig();
  } else {
    await saveCurrentConfig();
  }
  editingProvider = null;
  toast('Provider updated', 'success');
}
function renderProviders() {
  const list = document.getElementById('providers-list');
  if (!currentConfig || !currentConfig.providers || currentConfig.providers.length === 0) {
    list.innerHTML = '<div class="empty">No providers configured. Click "+ Add Provider" to get started.</div>';
    return;
  }
  list.innerHTML = currentConfig.providers.map(p => {
    if (editingProvider === p.id) {
      return '<div class="form-section" data-provider-id="'+escAttr(p.id)+'">'
        + '<div class="form-row"><label>ID (slug)</label><input type="text" class="edit-id" value="'+esc(p.id)+'" title="Changing the slug updates all backend references"></div>'
        + '<div class="form-row"><label>Name</label><input type="text" class="edit-name" value="'+esc(p.name||p.id)+'"></div>'
        + '<div class="form-row"><label>Avatar</label><input type="text" class="edit-avatar" value="'+esc(p.avatar||'')+'" placeholder="e.g. OG, GPT, ☁️" maxlength="8" title="Custom text for the avatar tile"></div>'
        + '<div class="form-row"><label>Base URL</label><input type="text" class="edit-url" value="'+esc(p.base_url)+'"></div>'
        + '<div class="form-row"><label>API Key</label><div class="api-key-wrap"><input type="password" class="edit-key" value="" data-provider="'+escAttr(p.id)+'" placeholder="'+esc(p.api_key ? '•••••• (unchanged)' : 'no key set')+'" autocomplete="new-password"><button class="reveal-btn" onclick="revealEditKey(this)">show</button></div><div class="form-hint">Key is stored securely; leave blank to keep the current key, or type a new one.</div></div>'
        + '<div class="form-row"><label>Timeout (s)</label><input type="number" class="edit-timeout" value="'+(p.timeout||120)+'"></div>'
        + '<div class="actions"><button onclick="saveProviderEdit(this)">Save Changes</button><button class="secondary" onclick="cancelEditProvider(this)">Cancel</button></div>'
        + '</div>';
    }
    const enabled = p.enabled !== false;
    const disc = discoveryOpen === p.id ? renderDiscovery(p.id) : '';
    return '<div class="provider-row'+(enabled ? '' : ' is-disabled')+'">'
      + '<div class="provider-info hstack">'+providerAvatar(p.id, 30, p.avatar)
      + '<span><span class="provider-name">'+esc(p.name||p.id)+'</span><code class="provider-id">'+esc(p.id)+'</code><div class="provider-url">'+esc(p.base_url)+'</div></span>'
      + '</div>'
      + '<button class="icon-btn" data-id="'+escAttr(p.id)+'" onclick="discoverProvider(this.dataset.id)" title="Fetch models served by this provider">Discover</button>'
      + '<button class="icon-btn" data-id="'+escAttr(p.id)+'" onclick="toggleEditProvider(this.dataset.id)">Edit</button>'
      + '<button class="icon-btn '+(enabled?'danger':'success')+'" data-id="'+escAttr(p.id)+'" onclick="toggleProviderEnabled(this.dataset.id)" title="'+(enabled?'Disable provider':'Enable provider')+'">'+(enabled?'Disable':'Enable')+'</button>'
      + '<button class="icon-btn danger" data-id="'+escAttr(p.id)+'" onclick="deleteProvider(this.dataset.id)">Delete</button>'
      + disc
      + '</div>';
  }).join('');
}

function renderDiscovery(providerId) {
  const cache = discoveryCache[providerId];
  if (!cache) return '<div class="form-section discovery-panel"><div class="empty">Loading…</div></div>';
  if (cache.error) return '<div class="form-section discovery-panel"><div class="empty" style="color:var(--red)">'+esc(cache.error)+'</div></div>';
  if (cache.loading) return '<div class="form-section discovery-panel"><div class="empty">Fetching models from '+esc(providerId)+'…</div></div>';
  const models = cache.models || [];
  const rows = models.map(m => {
    const ctx = m.context_length ? ' · '+fmt(m.context_length,0)+' ctx' : '';
    return '<div class="provider-row discovery-row">'
      + '<div class="provider-info"><code>'+esc(m.id)+'</code><span class="provider-url" style="display:inline;margin-left:8px">'+esc(ctx)+'</span></div>'
      + '<button class="icon-btn" data-provider="'+escAttr(providerId)+'" data-model="'+escAttr(m.id)+'" onclick="addDiscoveredModel(this.dataset.provider, this.dataset.model)">+ Add</button>'
      + '</div>';
  }).join('');
  return '<div class="form-section discovery-panel">'
    + '<div class="hstack" style="justify-content:space-between;margin-bottom:10px"><h2>Models on '+esc(providerId)+' ('+models.length+')</h2>'
    + '<button class="secondary btn-sm" onclick="discoveryOpen=null;renderProviders()">Close</button></div>'
    + '<div class="filter-hint" style="margin-bottom:10px">Adding creates a gateway model with the same ID (if missing) and attaches this provider at the next priority tier.</div>'
    + (rows || '<div class="empty">No models returned.</div>')
    + '</div>';
}

async function discoverProvider(id) {
  if (discoveryOpen === id) { discoveryOpen = null; renderProviders(); return; }
  discoveryOpen = id;
  discoveryCache[id] = { loading: true };
  renderProviders();
  try {
    const r = await fetch('/admin/providers/'+encodeURIComponent(id)+'/models');
    const data = await r.json();
    if (!r.ok) { discoveryCache[id] = { error: data.error || ('HTTP '+r.status) }; }
    else { discoveryCache[id] = { models: data.models || [] }; }
  } catch(e) { discoveryCache[id] = { error: e.message }; }
  renderProviders();
}

async function addDiscoveredModel(providerId, backendModel) {
  let model = currentConfig.models.find(m => m.id === backendModel);
  if (!model) {
    model = { id: backendModel, backends: [] };
    currentConfig.models.push(model);
  }
  if (model.backends.some(b => b.provider === providerId && b.model === backendModel)) {
    toast('Provider already attached to '+backendModel, 'info');
    return;
  }
  const nextPriority = model.backends.reduce((mx, b) => Math.max(mx, b.priority || 0), 0) + 1;
  model.backends.push({ provider: providerId, model: backendModel, priority: nextPriority, enabled: true });
  if (await saveCurrentConfig()) toast('Added '+providerId+':'+backendModel+' → '+backendModel, 'success');
}

async function toggleProviderEnabled(id) {
  const p = currentConfig.providers.find(x => x.id === id);
  if (!p) return;
  p.enabled = p.enabled === false ? true : false;
  await saveCurrentConfig();
  toast('Provider '+(p.enabled?'enabled':'disabled'), 'info');
}

document.addEventListener('DOMContentLoaded', loadProvidersPage);
