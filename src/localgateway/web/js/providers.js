// providers.js — providers page logic
let editingProvider = null;
let costEditorOpen = null;
let discoveryOpen = null;
let discoveryCache = {};
let testResults = {};
let editingBackend = null;
let expandedModels = {};

async function loadProvidersPage() {
  await loadConfig();
}
async function loadConfig() {
  try {
    const { data } = await fetchJSON('/admin/config');
    currentConfig = data;
    renderProviders();
    renderModels();
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
    renderModels();
    return true;
  } catch(e) { toast('Save failed: ' + e.message, 'error'); return false; }
}

function toggleReveal(inputId, btn) {
  const el = document.getElementById(inputId);
  if (el.type === 'password') { el.type = 'text'; btn.textContent = 'hide'; }
  else { el.type = 'password'; btn.textContent = 'show'; }
}
function toggleAddProvider() {
  const form = document.getElementById('add-provider-form');
  form.classList.toggle('hidden');
  if (!form.classList.contains('hidden')) {
    ['new-prov-id','new-prov-name','new-prov-url','new-prov-key'].forEach(id => document.getElementById(id).value='');
    document.getElementById('new-prov-timeout').value = '120';
    document.getElementById('new-prov-id').focus();
  }
}
async function addProvider() {
  const id = document.getElementById('new-prov-id').value.trim();
  const name = document.getElementById('new-prov-name').value.trim();
  const baseUrl = document.getElementById('new-prov-url').value.trim();
  const apiKey = document.getElementById('new-prov-key').value.trim();
  const timeout = parseFloat(document.getElementById('new-prov-timeout').value) || 120;
  if (!id || !baseUrl) { toast('ID and Base URL are required', 'error'); return; }
  if (currentConfig.providers.some(p => p.id === id)) { toast('Provider ID already exists', 'error'); return; }
  currentConfig.providers.push({ id, name: name || id, base_url: baseUrl, api_key: apiKey, headers: {}, timeout });
  if (await saveCurrentConfig()) { toggleAddProvider(); toast('Provider added', 'success'); }
}
function toggleEditProvider(id) {
  editingProvider = (editingProvider === id) ? null : id;
  renderProviders();
}
async function deleteProvider(id) {
  if (!await confirm2('Delete provider "'+id+'"?')) return;
  currentConfig.providers = currentConfig.providers.filter(p => p.id !== id);
  await saveCurrentConfig();
  toast('Provider deleted', 'info');
}
async function saveProviderEdit(id) {
  const p = currentConfig.providers.find(p => p.id === id);
  if (!p) return;
  p.name = document.getElementById('edit-name-'+id).value.trim() || p.id;
  p.base_url = document.getElementById('edit-url-'+id).value.trim();
  p.api_key = document.getElementById('edit-key-'+id).value.trim();
  p.timeout = parseFloat(document.getElementById('edit-timeout-'+id).value) || 120;
  editingProvider = null;
  await saveCurrentConfig();
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
      return '<div class="form-section">'
        + '<h2 style="margin-bottom:14px">Edit: '+esc(p.id)+'</h2>'
        + '<div class="form-row"><label>Name</label><input type="text" id="edit-name-'+p.id+'" value="'+esc(p.name||p.id)+'"></div>'
        + '<div class="form-row"><label>Base URL</label><input type="text" id="edit-url-'+p.id+'" value="'+esc(p.base_url)+'"></div>'
        + '<div class="form-row"><label>API Key</label><div class="api-key-wrap"><input type="password" id="edit-key-'+p.id+'" value="'+esc(p.api_key||'')+'"><button class="reveal-btn" onclick="toggleReveal(\'edit-key-'+p.id+'\', this)">show</button></div></div>'
        + '<div class="form-row"><label>Timeout (s)</label><input type="number" id="edit-timeout-'+p.id+'" value="'+(p.timeout||120)+'"></div>'
        + '<div class="actions"><button onclick="saveProviderEdit(\''+p.id+'\')">Save Changes</button><button class="secondary" onclick="toggleEditProvider(\''+p.id+'\')">Cancel</button></div>'
        + '</div>';
    }
    const enabled = p.enabled !== false;
    const mode = p.reasoning_mode || 'auto';
    const disc = discoveryOpen === p.id ? renderDiscovery(p.id) : '';
    return '<div class="provider-row" style="flex-wrap:wrap' + (enabled ? '' : ';opacity:0.55') + '">'
      + '<div class="provider-info"><span class="provider-name">'+esc(p.name||p.id)+'</span><code class="provider-id">'+esc(p.id)+'</code><div class="provider-url">'+esc(p.base_url)+'</div></div>'
      + '<select style="width:auto;padding:5px 8px;font-size:0.78rem" title="Reasoning normalization" onchange="setReasoningMode(\''+esc(p.id)+'\',this.value)">'
      +   '<option value="auto"'+(mode==='auto'?' selected':'')+'>reasoning: dual-emit</option>'
      +   '<option value="passthrough"'+(mode==='passthrough'?' selected':'')+'>reasoning: passthrough</option>'
      + '</select>'
      + '<button class="icon-btn" onclick="discoverProvider(\''+esc(p.id)+'\')" title="Fetch models served by this provider">Discover</button>'
      + '<button class="icon-btn" onclick="toggleEditProvider(\''+p.id+'\')">Edit</button>'
      + '<button class="icon-btn '+(enabled?'danger':'success')+'" style="padding:5px 10px" onclick="toggleProviderEnabled(\''+esc(p.id)+'\')" title="'+(enabled?'Disable provider':'Enable provider')+'">'+(enabled?'Disable':'Enable')+'</button>'
      + '<button class="icon-btn danger" onclick="deleteProvider(\''+esc(p.id)+'\')">Delete</button>'
      + disc
      + '</div>';
  }).join('');
}

function renderDiscovery(providerId) {
  const cache = discoveryCache[providerId];
  if (!cache) return '<div style="width:100%" class="form-section"><div class="empty">Loading…</div></div>';
  if (cache.error) return '<div style="width:100%" class="form-section"><div class="empty" style="color:var(--red)">'+esc(cache.error)+'</div></div>';
  if (cache.loading) return '<div style="width:100%" class="form-section"><div class="empty">Fetching models from '+esc(providerId)+'…</div></div>';
  const models = cache.models || [];
  const rows = models.map(m => {
    const ctx = m.context_length ? ' · '+fmt(m.context_length,0)+' ctx' : '';
    return '<div class="provider-row" style="padding:7px 10px">'
      + '<div class="provider-info"><code>'+esc(m.id)+'</code><span class="provider-url" style="display:inline;margin-left:8px">'+esc(ctx)+'</span></div>'
      + '<button class="icon-btn" onclick="addDiscoveredModel(\''+esc(providerId)+'\',\''+esc(m.id).replace(/'/g,"\\'")+'\')">+ Add</button>'
      + '</div>';
  }).join('');
  return '<div style="width:100%" class="form-section">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><h2>Models on '+esc(providerId)+' ('+models.length+')</h2>'
    + '<button class="secondary" style="padding:5px 12px;font-size:0.78rem" onclick="discoveryOpen=null;renderProviders()">Close</button></div>'
    + '<div style="font-size:0.78rem;color:var(--text-dim);margin-bottom:10px">Adding creates a gateway model with the same ID (if missing) and attaches this provider at the next priority tier.</div>'
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
  const p = currentConfig.providers.find(p => p.id === id);
  if (!p) return;
  p.enabled = p.enabled === false ? true : false;
  await saveCurrentConfig();
  toast('Provider '+(p.enabled?'enabled':'disabled'), 'info');
}

async function setReasoningMode(id, mode) {
  const p = currentConfig.providers.find(p => p.id === id);
  if (!p) return;
  p.reasoning_mode = mode;
  await saveCurrentConfig();
}

// ---------- gateway models ----------
function toggleAddModel() {
  const form = document.getElementById('add-model-form');
  form.classList.toggle('hidden');
  if (!form.classList.contains('hidden')) {
    document.getElementById('new-model-id').value = '';
    document.getElementById('new-model-id').focus();
  }
}
async function addModel() {
  const id = document.getElementById('new-model-id').value.trim();
  if (!id) { toast('Model ID is required', 'error'); return; }
  if (currentConfig.models.some(m => m.id === id)) { toast('Model ID already exists', 'error'); return; }
  currentConfig.models.push({ id, backends: [] });
  if (await saveCurrentConfig()) { toggleAddModel(); toast('Model added', 'success'); }
}
async function deleteModel(id) {
  if (!await confirm2('Delete model "'+id+'"?')) return;
  currentConfig.models = currentConfig.models.filter(m => m.id !== id);
  await saveCurrentConfig();
  toast('Model deleted', 'info');
}
async function deleteBackend(modelId, index) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  model.backends.splice(index, 1);
  await saveCurrentConfig();
}
async function moveBackend(modelId, index, delta) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  const to = index + delta;
  if (to < 0 || to >= model.backends.length) return;
  const [item] = model.backends.splice(index, 1);
  model.backends.splice(to, 0, item);
  model.backends.forEach((b, i) => { b.priority = i + 1; });
  await saveCurrentConfig();
}

// cost editor
function toggleCost(modelId, index) {
  const key = modelId + ':' + index;
  costEditorOpen = (costEditorOpen === key) ? null : key;
  renderModels();
}
async function updateCost(provider, backendModel, field, value) {
  const key = provider + ':' + backendModel;
  if (!currentConfig.pricing) currentConfig.pricing = {};
  if (!currentConfig.pricing[key]) currentConfig.pricing[key] = { input: 0, output: 0 };
  const v = parseFloat(value) || 0;
  if (field === 'input' || field === 'output') {
    currentConfig.pricing[key][field] = v / 1e6;
  } else {
    currentConfig.pricing[key][field] = v > 0 ? v / 1e6 : null;
  }
  await saveCurrentConfig();
}
function getPricing(provider, model) {
  const p = (currentConfig.pricing || {})[provider + ':' + model];
  return {
    input: p ? (p.input||0) : 0,
    output: p ? (p.output||0) : 0,
    cache_read: p ? (p.cache_read || 0) : 0,
    cache_write: p ? (p.cache_write || 0) : 0,
  };
}

function renderModels() {
  const list = document.getElementById('models-list');
  if (!currentConfig || !currentConfig.models || currentConfig.models.length === 0) {
    list.innerHTML = '<div class="empty">No models configured. Click "+ Add" to get started.</div>';
    return;
  }
  const providerOptions = (currentConfig.providers || [])
    .map(p => '<option value="'+esc(p.id)+'">'+esc(p.id)+'</option>').join('');

  list.innerHTML = currentConfig.models.map(m => {
    m.backends.sort((a, b) => (a.priority||0) - (b.priority||0));
    const n = m.backends.length;
    const modelEnabled = m.enabled !== false;
    const expanded = expandedModels[m.id];
    const tierCount = new Set(m.backends.map(b => b.priority || 1)).size;

    const summary = '<div class="model-summary" onclick="toggleModelExpand(\''+esc(m.id)+'\')" style="display:flex;align-items:center;gap:12px;cursor:pointer;padding:12px 14px;border-radius:8px;transition:background 0.15s" onmouseover="this.style.background=\'var(--surface-2)\'" onmouseout="this.style.background=\'\'">'
      + '<span style="color:var(--text-dim);font-size:0.7rem;transition:transform 0.15s;'+(expanded?'transform:rotate(90deg)':'')+'">▶</span>'
      + '<code style="font-weight:600;color:var(--text-bright);font-size:0.92rem">'+esc(m.id)+'</code>'
      + (m.description ? '<span style="color:var(--text-dim);font-size:0.8rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(m.description)+'</span>' : '<span style="flex:1"></span>')
      + '<span style="font-size:0.72rem;color:var(--text-dim)">'+n+' provider'+(n!==1?'s':'')+' · '+tierCount+' tier'+(tierCount!==1?'s':'')+'</span>'
      + '<span class="badge '+(modelEnabled?'badge-green':'badge-red')+'" style="font-size:0.7rem">'+(modelEnabled?'on':'off')+'</span>'
      + '</div>';

    if (!expanded) {
      return '<div class="model-row" style="'+(modelEnabled?'':'opacity:0.7')+';padding:4px 0;border-bottom:1px solid var(--border-light)">'+summary+'</div>';
    }

    const backendsHtml = m.backends.map((b, i) => {
      const isPrimary = i === 0;
      const enabled = b.enabled !== false;
      const pricing = getPricing(b.provider, b.model);
      const costOpen = costEditorOpen === (m.id + ':' + i);
      const editOpen = editingBackend === (m.id + ':' + i);
      const tr = testResults[m.id + ':' + i];
      const costEditor = costOpen
        ? '<div class="cost-editor">'
          + '<label>Input $/1M</label><input type="number" step="any" value="'+(pricing.input*1e6)+'" onchange="updateCost(\''+esc(b.provider)+'\',\''+esc(b.model)+'\',\'input\',this.value)">'
          + '<label>Output $/1M</label><input type="number" step="any" value="'+(pricing.output*1e6)+'" onchange="updateCost(\''+esc(b.provider)+'\',\''+esc(b.model)+'\',\'output\',this.value)">'
          + '<label>Cache read $/1M</label><input type="number" step="any" placeholder="same as input" value="'+(pricing.cache_read*1e6)+'" onchange="updateCost(\''+esc(b.provider)+'\',\''+esc(b.model)+'\',\'cache_read\',this.value)">'
          + '<label>Cache write $/1M</label><input type="number" step="any" placeholder="same as input" value="'+(pricing.cache_write*1e6)+'" onchange="updateCost(\''+esc(b.provider)+'\',\''+esc(b.model)+'\',\'cache_write\',this.value)">'
          + '<span class="cost-hint">per 1M tokens</span>'
          + '</div>'
        : '';
      const nameHtml = editOpen
        ? '<span class="backend-name" style="flex-wrap:wrap;gap:6px"><span class="prov">'+esc(b.provider)+':</span>'
          + '<input type="text" id="edit-backend-'+esc(m.id)+'-'+i+'" value="'+esc(b.model)+'" style="width:190px;padding:4px 8px;font-size:0.82rem" '
          + 'onkeydown="if(event.key===\'Enter\')saveBackendModel(\''+esc(m.id)+'\','+i+')" title="Backend model ID">'
          + '<input type="number" id="edit-backend-ctx-'+esc(m.id)+'-'+i+'" value="'+(b.context_length||'')+'" placeholder="ctx" style="width:86px;padding:4px 8px;font-size:0.82rem" title="Context length">'
          + '<input type="number" id="edit-backend-maxout-'+esc(m.id)+'-'+i+'" value="'+(b.max_output_tokens||'')+'" placeholder="max out" style="width:92px;padding:4px 8px;font-size:0.82rem" title="Max output tokens">'
          + '</span>'
          + '<button class="icon-btn" style="padding:3px 9px" onclick="saveBackendModel(\''+esc(m.id)+'\','+i+')">Save</button>'
        : '<span class="backend-name"><span class="prov">'+esc(b.provider)+':</span>'+esc(b.model)+'</span>';
      const testBadge = tr
        ? (tr.ok
            ? '<span class="badge badge-green" title="Probe succeeded">ok '+tr.latency_ms+'ms'+(tr.ttft_ms!=null?' · ttft '+tr.ttft_ms+'ms':'')+'</span>'
            : '<span class="badge badge-red" title="'+esc(tr.error||'')+'">fail'+(tr.status_code?' '+tr.status_code:'')+'</span>')
        : '';
      return '<div style="'+(enabled?'':'opacity:0.55')+'">'
        + '<div class="backend-item">'
        +   '<span class="rank-badge '+(isPrimary?'primary':'')+'" title="'+(isPrimary?'Tier 1 — load-balanced with other #1s':'Fallback tier #'+(i+1))+'">#'+(i+1)+'</span>'
        +   nameHtml
        +   testBadge
        +   '<span class="rank-btns">'
        +     '<button '+(i===0?'disabled':'')+' onclick="moveBackend(\''+esc(m.id)+'\','+i+',-1)" title="Move up">▲</button>'
        +     '<button '+(i===n-1?'disabled':'')+' onclick="moveBackend(\''+esc(m.id)+'\','+i+',1)" title="Move down">▼</button>'
        +   '</span>'
        +   '<button class="icon-btn" style="padding:3px 9px" onclick="testBackend(\''+esc(m.id)+'\','+i+')" title="Send a 1-token probe">Test</button>'
        +   '<button class="icon-btn" style="padding:3px 9px" onclick="toggleEditBackend(\''+esc(m.id)+'\','+i+')" title="Edit provider model ID">✎</button>'
        +   '<button class="cost-toggle '+(costOpen?'open':'')+'" onclick="toggleCost(\''+esc(m.id)+'\','+i+')" title="Set cost">$</button>'
        +   '<button class="icon-btn '+(enabled?'danger':'success')+'" style="padding:3px 9px" onclick="toggleBackendEnabled(\''+esc(m.id)+'\','+i+')" title="'+(enabled?'Disable backend':'Enable backend')+'">'+(enabled?'Disable':'Enable')+'</button>'
        +   '<button class="icon-btn danger" style="padding:3px 9px" onclick="deleteBackend(\''+esc(m.id)+'\','+i+')" title="Remove backend">×</button>'
        + '</div>'
        + costEditor
        + '</div>';
    }).join('');

    const addBackendHtml = currentConfig.providers.length > 0
      ? '<div class="backend-edit-row">'
        + '<div><label>Provider</label><select id="backend-provider-'+esc(m.id)+'">'+providerOptions+'</select></div>'
        + '<div><label>Backend Model</label><input type="text" id="backend-model-'+esc(m.id)+'" placeholder="e.g. gpt-4o"></div>'
        + '<div></div>'
        + '<div><button class="secondary" style="padding:7px 14px;font-size:0.8rem" onclick="addBackendOld(\''+esc(m.id)+'\')">+ Add</button></div>'
        + '</div>'
      : '<div class="empty" style="padding:8px">Add a provider first to configure providers.</div>';

    return '<div class="model-row" style="'+(modelEnabled?'':'opacity:0.7')+';padding:8px 0;border-bottom:1px solid var(--border-light)">'
      + summary
      + '<div style="padding:8px 14px 4px">'
      + '<div style="display:flex;justify-content:flex-end;gap:6px;margin-bottom:10px">'
      +   '<button class="icon-btn '+(modelEnabled?'danger':'success')+'" onclick="toggleModelEnabled(\''+esc(m.id)+'\')" title="'+(modelEnabled?'Disable model':'Enable model')+'">'+(modelEnabled?'Disable':'Enable')+'</button>'
      +   '<button class="icon-btn danger" onclick="deleteModel(\''+esc(m.id)+'\')">Delete Model</button>'
      + '</div>'
      + '<div style="display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap">'
      +   '<input type="text" placeholder="Description (shown in /v1/models)" value="'+esc(m.description||'')+'" style="flex:1;min-width:200px;padding:6px 10px;font-size:0.8rem" onchange="setModelMeta(\''+esc(m.id)+'\',\'description\',this.value)">'
      +   '<input type="number" placeholder="Context length" value="'+(m.context_length||'')+'" style="width:150px;padding:6px 10px;font-size:0.8rem" onchange="setModelMeta(\''+esc(m.id)+'\',\'context_length\',this.value)">'
      + '</div>'
      + '<div class="backend-list">'+(backendsHtml || '<div class="empty" style="padding:8px">No backends configured.</div>')+'</div>'
      + addBackendHtml
      + '</div>'
      + '</div>';
  }).join('');
}

function toggleModelExpand(modelId) {
  expandedModels[modelId] = !expandedModels[modelId];
  renderModels();
}

async function setModelMeta(modelId, field, value) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  if (field === 'context_length') model.context_length = value ? parseInt(value, 10) : null;
  else model[field] = value;
  await saveCurrentConfig();
}

function toggleEditBackend(modelId, index) {
  const key = modelId + ':' + index;
  editingBackend = (editingBackend === key) ? null : key;
  renderModels();
  if (editingBackend) {
    const el = document.getElementById('edit-backend-'+modelId+'-'+index);
    if (el) { el.focus(); el.select(); }
  }
}

async function saveBackendModel(modelId, index) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  const el = document.getElementById('edit-backend-'+modelId+'-'+index);
  const newModel = (el ? el.value : '').trim();
  if (!newModel) { toast('Provider model ID cannot be empty', 'error'); return; }
  const b = model.backends[index];
  b.model = newModel;
  const ctxEl = document.getElementById('edit-backend-ctx-'+modelId+'-'+index);
  const maxEl = document.getElementById('edit-backend-maxout-'+modelId+'-'+index);
  b.context_length = ctxEl && ctxEl.value ? parseInt(ctxEl.value, 10) : null;
  b.max_output_tokens = maxEl && maxEl.value ? parseInt(maxEl.value, 10) : null;
  editingBackend = null;
  await saveCurrentConfig();
  toast('Provider updated', 'success');
}

async function toggleModelEnabled(modelId) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  model.enabled = model.enabled === false ? true : false;
  await saveCurrentConfig();
  toast('Model '+(model.enabled?'enabled':'disabled'), 'info');
}

async function toggleBackendEnabled(modelId, index) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  const b = model.backends[index];
  b.enabled = b.enabled === false ? true : false;
  await saveCurrentConfig();
}

async function testBackend(modelId, index) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  const b = model.backends[index];
  const key = modelId + ':' + index;
  testResults[key] = null;
  renderModels();
  try {
    const r = await fetch('/admin/backends/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider: b.provider, model: b.model, stream: true }),
    });
    const data = await r.json();
    testResults[key] = data;
  } catch(e) {
    testResults[key] = { ok: false, error: e.message };
  }
  renderModels();
}

// addBackend for the inline provider-list form (uses inline select/input)
async function addBackendOld(modelId) {
  const model = currentConfig.models.find(m => m.id === modelId);
  if (!model) return;
  const provider = document.getElementById('backend-provider-'+modelId).value;
  const backendModel = document.getElementById('backend-model-'+modelId).value.trim();
  if (!provider || !backendModel) { toast('Provider and model are required', 'error'); return; }
  const nextPriority = model.backends.reduce((mx, b) => Math.max(mx, b.priority || 0), 0) + 1;
  model.backends.push({ provider, model: backendModel, priority: nextPriority });
  await saveCurrentConfig();
  toast('Provider added', 'success');
}

document.addEventListener('DOMContentLoaded', loadProvidersPage);