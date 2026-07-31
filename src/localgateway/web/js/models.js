// models.js — models catalog page logic
let modelsOverview = null;
let compareMode = false;
let compareSelection = new Set();
let catalogView = localStorage.getItem('lg-catalog-view') || 'cards';
let modalityFilter = '';

function setCatalogView(view, btn) {
  catalogView = view;
  localStorage.setItem('lg-catalog-view', view);
  document.querySelectorAll('#catalog-view-toggle button').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  else {
    const buttons = document.querySelectorAll('#catalog-view-toggle button');
    buttons.forEach(b => {
      if ((view === 'cards' && b.textContent.trim() === 'List') ||
          (view === 'table' && b.textContent.trim() === 'Table')) b.classList.add('active');
    });
  }
  renderModelCatalog();
}

function toggleCompareMode() {
  compareMode = !compareMode;
  compareSelection.clear();
  const btn = document.getElementById('compare-btn');
  btn.textContent = compareMode ? 'Cancel Compare' : 'Compare';
  btn.classList.toggle('active', compareMode);
  renderModelCatalog();
}

function toggleCompareSelection(id, ev) {
  if (ev) ev.stopPropagation();
  if (compareSelection.has(id)) compareSelection.delete(id);
  else if (compareSelection.size < 3) compareSelection.add(id);
  renderModelCatalog();
}

function showCompare() {
  if (compareSelection.size < 2) {
    toast('Select at least 2 models to compare', 'error');
    return;
  }
  const ids = Array.from(compareSelection).join(',');
  location.href = '/compare/' + ids;
}

function populateProviderFilter() {
  const select = document.getElementById('filter-provider');
  if (!select || !currentConfig) return;
  const providers = currentConfig.providers || [];
  select.innerHTML = '<option value="">All providers</option>' +
    providers.map(p => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join('');
}

async function loadModelsPage() {
  const loading = document.getElementById('models-loading');
  if (loading) loading.classList.remove('hidden');
  if (!currentConfig) { try { currentConfig = (await fetchJSON('/admin/config')).data; } catch(e) {} }
  populateProviderFilter();
  try {
    const { ok, data } = await fetchJSON('/admin/models/overview?hours=168');
    modelsOverview = ok ? data.models : null;
  } catch(e) { modelsOverview = null; }
  renderModalityTabs();
  const params = new URLSearchParams(location.search);
  const q = params.get('q');
  if (q) {
    const searchInput = document.getElementById('models-search');
    if (searchInput) searchInput.value = q;
  }
  setCatalogView(catalogView);
  if (loading) loading.classList.add('hidden');
}

function _modalityCounts() {
  const cfgModels = (currentConfig && currentConfig.models) || [];
  const base = modelsOverview || cfgModels.map(m => ({ id: m.id, modality: m.modality }));
  const counts = { '': 0, 'text': 0, 'text+vision': 0, 'multimodal': 0 };
  base.forEach(m => {
    const mod = m.modality || '';
    counts[''] = (counts[''] || 0) + 1;
    if (mod && counts[mod] != null) counts[mod] += 1;
    else if (mod) counts[mod] = (counts[mod] || 0) + 1;
  });
  return counts;
}

function renderModalityTabs() {
  const el = document.getElementById('modality-tabs');
  if (!el) return;
  const counts = _modalityCounts();
  const tabs = [
    { val: '', label: 'All' },
    { val: 'text', label: 'Text' },
    { val: 'text+vision', label: 'Text + Vision' },
    { val: 'multimodal', label: 'Multimodal' },
  ];
  el.innerHTML = tabs.map(t => {
    const cnt = counts[t.val] || 0;
    if (!t.val && cnt === 0) return '';
    return '<button class="modality-tab'+(modalityFilter === t.val ? ' active' : '')+'" onclick="setModalityFilter(\''+t.val+'\')">'
      + esc(t.label) + ' <span class="tab-count">'+cnt+'</span></button>';
  }).join('');
}

function setModalityFilter(val) {
  modalityFilter = val;
  renderModalityTabs();
  renderModelCatalog();
}

function _filteredModels() {
  const cfgModels = (currentConfig && currentConfig.models) || [];
  let models;
  if (modelsOverview) {
    models = modelsOverview.slice();
  } else {
    models = cfgModels.map(m => {
      const active = (m.backends || []).filter(b => b.enabled !== false);
      const ctxs = active.map(b => b.context_length).filter(v => v);
      return {
      id: m.id, description: m.description || null,
      context_min: ctxs.length ? Math.min(...ctxs) : null,
      context_max: ctxs.length ? Math.max(...ctxs) : null,
      display_name: m.display_name || null,
      enabled: m.enabled !== false, backend_count: active.length,
      requests: 0, success_rate: null, tokens: 0, cost: 0, tps_p50: null,
      input_price: null, output_price: null,
    }});
  }

  models = models.map(m => {
    const cfg = cfgModels.find(c => c.id === m.id);
    return { ...m, ...cfg, ...m };
  });

  const q = (document.getElementById('models-search').value || '').toLowerCase();
  if (q) {
    models = models.filter(m =>
      m.id.toLowerCase().includes(q) ||
      (m.description || '').toLowerCase().includes(q) ||
      (m.display_name || '').toLowerCase().includes(q) ||
      (m.aliases || []).some(a => a.toLowerCase().includes(q))
    );
  }

  const providerFilter = document.getElementById('filter-provider').value;
  if (providerFilter) models = models.filter(m => (m.backends || []).some(b => b.provider === providerFilter));

  if (modalityFilter) models = models.filter(m => m.modality === modalityFilter);

  const capabilityFilter = document.getElementById('filter-capability').value;
  if (capabilityFilter) models = models.filter(m => m.capabilities && m.capabilities[capabilityFilter]);

  const minCtx = parseInt(document.getElementById('filter-min-ctx').value);
  if (minCtx) models = models.filter(m => (m.context_length || 0) >= minCtx);

  const maxPrice = parseFloat(document.getElementById('filter-max-price').value);
  if (maxPrice) models = models.filter(m => m.input_price != null && m.input_price <= maxPrice / 1e6);

  const sort = document.getElementById('models-sort').value;
  models.sort((a, b) => {
    if (sort === 'name') return a.id.localeCompare(b.id);
    if (sort === 'tps') return (b.tps_p50 || 0) - (a.tps_p50 || 0);
    if (sort === 'price') return (a.input_price || Infinity) - (b.input_price || Infinity);
    if (sort === 'context') return (b.context_length || 0) - (a.context_length || 0);
    return (b.tokens || 0) - (a.tokens || 0);
  });
  return models;
}

function _statChip(label, val, accent) {
  return '<div class="stat-chip"><div class="val'+(accent?' accent':'')+'">'+val+'</div><div class="lbl">'+label+'</div></div>';
}

function renderModelCatalog() {
  const cardsEl = document.getElementById('models-cards');
  const tableEl = document.getElementById('models-table');
  const models = _filteredModels();

  if (!models.length) {
    cardsEl.classList.remove('hidden');
    tableEl.classList.add('hidden');
    cardsEl.innerHTML = '<div class="empty-state"><p>No models match your filters.</p><button class="primary" onclick="showAddModelModal()">Add a model</button></div>';
    tableEl.innerHTML = '';
    return;
  }

  if (catalogView === 'table' && !compareMode) {
    cardsEl.classList.add('hidden');
    tableEl.classList.remove('hidden');
    tableEl.innerHTML = '<table class="catalog-table"><thead><tr>' +
      '<th>Model</th><th>Modality</th><th>Context</th><th>TPS P50</th><th>Tokens 7d</th><th>Spend 7d</th><th>In / Out</th><th>Uptime</th><th>Backends</th>' +
      '</tr></thead><tbody>' +
      models.map(m => {
        const disabled = m.enabled === false;
        return `<tr onclick="location.href='/models/${encodeURIComponent(m.id)}'">` +
          `<td>${m.display_name ? esc(m.display_name)+' <code style="font-size:0.7rem;color:var(--text-dim)">'+esc(m.id)+'</code>' : '<code>'+esc(m.id)+'</code>'}` +
          (disabled ? ' <span class="badge badge-gray">off</span>' : '') + `</td>` +
          `<td>${m.modality ? '<span class="badge badge-purple">'+esc(m.modality)+'</span>' : '—'}</td>` +
          `<td>${m.context_length ? fmtTokens(m.context_length) : '—'}</td>` +
          `<td>${m.tps_p50 != null ? fmt(m.tps_p50, 0) : '—'}</td>` +
          `<td>${fmtTokens(m.tokens)}</td>` +
          `<td>${fmtCost(m.cost)}</td>` +
          `<td>${m.input_price != null ? fmtPrice(m.input_price)+' / '+fmtPrice(m.output_price) : '—'}</td>` +
          `<td>${m.success_rate != null ? m.success_rate+'%' : '—'}</td>` +
          `<td>${m.backend_count || 0}</td></tr>`;
      }).join('') +
      '</tbody></table>';
    return;
  }

  cardsEl.classList.remove('hidden');
  tableEl.classList.add('hidden');
  tableEl.innerHTML = '';

  cardsEl.innerHTML = models.map(m => {
    const disabled = m.enabled === false;
    const modalityBadge = m.modality ? `<span class="badge badge-purple">${esc(m.modality)}</span>` : '';
    const tagsHtml = (m.tags || []).slice(0, 3).map(t => `<span class="badge badge-gray">${esc(t)}</span>`).join('');
    const primaryProvider = (m.backends || [])[0];
    const avatarHtml = modelAvatar(m.id, m.display_name, 30, m.avatar);
    const ctxStr = (m.context_min != null && m.context_max != null) ? (m.context_min === m.context_max ? fmtTokens(m.context_min) : fmtTokens(m.context_min)+'-'+fmtTokens(m.context_max)) : (m.context_length ? fmtTokens(m.context_length) : '—');
    const priceStr = m.input_price != null ? fmtPrice(m.input_price)+' / '+fmtPrice(m.output_price) : '—';

    if (compareMode) {
      const selected = compareSelection.has(m.id);
      const canSelect = selected || compareSelection.size < 3;
      return '<div class="row-card" style="opacity:'+(canSelect?'1':'0.4')+'" onclick="toggleCompareSelection(\''+esc(m.id)+'\')">'
        + '<input type="checkbox" '+(selected?'checked':'')+(!canSelect?' disabled':'')+' onclick="toggleCompareSelection(\''+esc(m.id)+'\', event)" style="width:18px;height:18px;flex-shrink:0">'
        + '<div class="rc-left">'
        +   avatarHtml
        +   '<div>'
        +     '<div class="rc-title"><span class="rc-name">'+(m.display_name ? esc(m.display_name) : esc(m.id))+'</span><span class="rc-slug">'+esc(m.id)+'</span>'+modalityBadge+tagsHtml+'</div>'
        +     '<div class="rc-desc">'+esc(m.description || 'No description')+'</div>'
        +   '</div>'
        + '</div>'
        + '<div class="rc-right">'
        +   '<div class="rc-stat al-r pl9 pr9"><div class="v">'+esc(ctxStr)+'</div><div class="l">Context</div></div>'
        +   '<div class="rc-stat al-r pl9 pr9"><div class="v">'+esc(priceStr)+'</div><div class="l">In / Out</div></div>'
        + '</div></div>';
    }

    return '<div class="row-card" onclick="location.href=\'/models/'+encodeURIComponent(m.id)+'\'">'
      + '<div class="rc-left">'
      +   avatarHtml
      +   '<div>'
      +     '<div class="rc-title">'
      +       '<span class="rc-name">'+(m.display_name ? esc(m.display_name) : esc(m.id))+'</span>'
      +       '<span class="rc-slug">'+esc(m.id)+'</span>'
      +       '<button class="copy-btn" onclick="event.stopPropagation();copyId(\''+esc(m.id)+'\',this)" title="Copy model ID">⧉</button>'
      +       modalityBadge + tagsHtml
      +       (disabled ? '<span class="badge badge-gray">disabled</span>' : '<span class="badge badge-blue">'+(m.backend_count||0)+' backend'+((m.backend_count||0)===1?'':'s')+'</span>')
      +     '</div>'
      +     '<div class="rc-desc">'+esc(m.description || 'No description')+'</div>'
      +   '</div>'
      + '</div>'
      + '<div class="rc-right">'
      +   '<div class="rc-stat al-r pl9 pr9"><div class="v">'+esc(ctxStr)+'</div><div class="l">Context</div></div>'
      +   '<div class="rc-stat al-r pl9 pr16"><div class="v">'+esc(priceStr)+'</div><div class="l">In / Out</div></div>'
      +   '<div class="rc-divider"></div>'
      +   '<div class="rc-stat al-l pl16 pr9"><div class="v accent">'+(m.tps_p50 != null ? fmt(m.tps_p50, 0) : '—')+'</div><div class="l">TPS P50</div></div>'
      +   '<div class="rc-stat al-r pl9 pr16"><div class="v">'+(m.success_rate != null ? m.success_rate+'%' : '—')+'</div><div class="l">Uptime</div></div>'
      +   '<div class="rc-divider"></div>'
      +   '<div class="rc-stat al-l pl16 pr9"><div class="v">'+fmtTokens(m.tokens)+'</div><div class="l">Tokens 7d</div></div>'
      +   '<div class="rc-stat al-r pl9 pr9"><div class="v">'+fmtCost(m.cost)+'</div><div class="l">Spend 7d</div></div>'
      + '</div></div>';
  }).join('');

  if (compareMode && compareSelection.size >= 2) {
    cardsEl.innerHTML += '<div style="position:fixed;bottom:20px;right:20px;z-index:10"><button class="primary" onclick="showCompare()">Compare ' + compareSelection.size + ' models</button></div>';
  }
  alignStatColumns();
}

function alignStatColumns() {
  const rows = [...document.querySelectorAll('.row-card')].filter(r => r.querySelectorAll('.rc-stat').length);
  if (!rows.length) return;
  const groups = {};
  rows.forEach(r => { const n = r.querySelectorAll('.rc-stat').length; (groups[n] = groups[n] || []).push(r); });
  Object.values(groups).forEach(grp => {
    if (grp.length < 2) return;
    const n = grp[0].querySelectorAll('.rc-stat').length;
    const widths = new Array(n).fill(0);
    grp.forEach(r => { const s = r.querySelectorAll('.rc-stat'); for (let i = 0; i < n; i++) widths[i] = Math.max(widths[i], s[i].offsetWidth); });
    grp.forEach(r => { const s = r.querySelectorAll('.rc-stat'); for (let i = 0; i < n; i++) s[i].style.width = widths[i] + 'px'; });
  });
}
window.addEventListener('load', alignStatColumns);

function showAddModelModal() {
  const providers = (currentConfig && currentConfig.providers) || [];
  const providerOpts = providers.map(p => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join('');
  openModal({
    title: 'Add New Model',
    widthClass: 'modal-lg',
    bodyHtml: `
      <div class="seg" id="add-model-tabs" style="margin-bottom:14px">
        <button type="button" class="active" data-tab="manual" onclick="switchAddModelTab('manual', this)">Manual</button>
        <button type="button" data-tab="catalog" onclick="switchAddModelTab('catalog', this)">From provider</button>
      </div>
      <div id="add-manual">
        <div class="field"><label class="field-label">Model ID *</label><input type="text" id="new-model-id" placeholder="e.g. gpt-4o"></div>
        <div class="field"><label class="field-label">Display Name</label><input type="text" id="new-model-name" placeholder="e.g. GPT-4o"></div>
        <div class="field"><label class="field-label">Description</label><input type="text" id="new-model-desc" placeholder="Optional description"></div>
      </div>
      <div id="add-catalog" class="hidden">
        <div class="field"><label class="field-label">Provider</label>
          <select id="discover-provider"><option value="">Select provider…</option>${providerOpts}</select>
        </div>
        <div class="field"><label class="field-label">Search</label>
          <input type="text" id="discover-search" placeholder="Filter upstream models…" oninput="filterDiscoverList()">
        </div>
        <div id="discover-status" style="font-size:0.78rem;color:var(--text-dim);margin-bottom:8px"></div>
        <div id="discover-list" style="max-height:220px;overflow-y:auto;border:1px solid var(--border);border-radius:8px"></div>
        <input type="hidden" id="picked-backend-model" value="">
        <input type="hidden" id="picked-provider" value="">
        <div class="field" style="margin-top:12px"><label class="field-label">Gateway Model ID *</label><input type="text" id="new-model-id-cat" placeholder="Logical id for LocalGateway"></div>
        <div class="field"><label class="field-label">Display Name</label><input type="text" id="new-model-name-cat"></div>
      </div>
      <div class="modal-actions">
        <button class="secondary" onclick="closeModal()">Cancel</button>
        <button class="primary" id="add-model-create" onclick="createModel(this)">Create</button>
      </div>`,
    onMount: (overlay) => {
      const sel = overlay.querySelector('#discover-provider');
      if (sel) sel.addEventListener('change', () => discoverForAdd(sel.value));
    },
  });
}

function switchAddModelTab(tab, btn) {
  document.querySelectorAll('#add-model-tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('add-manual').classList.toggle('hidden', tab !== 'manual');
  document.getElementById('add-catalog').classList.toggle('hidden', tab !== 'catalog');
}

let _discoverModels = [];

async function discoverForAdd(providerId) {
  const status = document.getElementById('discover-status');
  const list = document.getElementById('discover-list');
  _discoverModels = [];
  list.innerHTML = '';
  if (!providerId) { status.textContent = ''; return; }
  status.textContent = 'Loading catalog…';
  try {
    const { ok, data } = await fetchJSON('/admin/providers/' + encodeURIComponent(providerId) + '/models');
    if (!ok) {
      status.textContent = data.error || 'Failed to discover models';
      return;
    }
    _discoverModels = data.models || [];
    status.textContent = _discoverModels.length + ' models found';
    filterDiscoverList();
  } catch (e) {
    status.textContent = e.message;
  }
}

function filterDiscoverList() {
  const list = document.getElementById('discover-list');
  const q = (document.getElementById('discover-search').value || '').toLowerCase();
  const filtered = _discoverModels.filter(m => {
    const id = typeof m === 'string' ? m : (m.id || m);
    return !q || String(id).toLowerCase().includes(q);
  });
  if (!filtered.length) {
    list.innerHTML = '<div class="empty-state" style="padding:16px">No models</div>';
    return;
  }
  list.innerHTML = filtered.slice(0, 100).map(m => {
    const id = typeof m === 'string' ? m : (m.id || '');
    return `<div style="padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer;font-family:var(--font-mono);font-size:0.82rem" onclick="pickDiscovered('${esc(id)}')">${esc(id)}</div>`;
  }).join('');
}

function pickDiscovered(backendModel) {
  const providerId = document.getElementById('discover-provider').value;
  document.getElementById('picked-backend-model').value = backendModel;
  document.getElementById('picked-provider').value = providerId;
  const slug = backendModel.replace(/[/:]/g, '-');
  document.getElementById('new-model-id-cat').value = slug;
  document.getElementById('new-model-name-cat').value = backendModel;
  document.querySelectorAll('#discover-list > div').forEach(el => {
    el.style.background = el.textContent === backendModel ? 'var(--surface-2)' : '';
  });
}

async function createModel(btn) {
  const catalogTab = !document.getElementById('add-catalog').classList.contains('hidden');
  let id, display_name, description, backends = [];

  if (catalogTab) {
    id = document.getElementById('new-model-id-cat').value.trim();
    display_name = document.getElementById('new-model-name-cat').value.trim() || null;
    description = null;
    const provider = document.getElementById('picked-provider').value;
    const backendModel = document.getElementById('picked-backend-model').value;
    if (!provider || !backendModel) { toast('Pick a provider model first', 'error'); return; }
    backends = [{ provider, model: backendModel, priority: 1, enabled: true }];
  } else {
    id = document.getElementById('new-model-id').value.trim();
    display_name = document.getElementById('new-model-name').value.trim() || null;
    description = document.getElementById('new-model-desc').value.trim() || null;
  }
  if (!id) { toast('Model ID is required', 'error'); return; }

  btn.disabled = true;
  btn.textContent = 'Creating…';
  try {
    const r = await fetch('/admin/models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id, display_name, description, enabled: true, backends })
    });
    if (r.ok) {
      toast('Model created', 'success');
      closeModal();
      currentConfig = (await fetchJSON('/admin/config')).data;
      loadModelsPage();
    } else {
      const d = await r.json();
      toast(d.error || 'Failed to create model', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create';
  }
}

document.addEventListener('DOMContentLoaded', loadModelsPage);
