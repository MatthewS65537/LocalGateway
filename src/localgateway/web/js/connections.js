// connections.js — /connections deep page: sortable/filterable backend health
let connData = [];
let connFilter = 'all';
let connSort = { col: 'model', dir: 1 };

async function loadConnections() {
  const { ok, data } = await apiFetch('/admin/health', { silent: true });
  if (!ok) { loadError('connections'); return; }
  const stats = data.stats || {};
  const circuits = (data.circuit && data.circuit.circuits) || {};
  const warmth = {};
  try {
    const { data: w } = await apiFetch('/admin/warmth', { silent: true });
    if (w) {
      for (const entries of Object.values(w)) {
        for (const [bk, e] of Object.entries(entries)) {
          if (!warmth[bk]) warmth[bk] = { hit_rate: e.last_hit_rate, count: 1 };
          else { warmth[bk].count++; warmth[bk].hit_rate = Math.max(warmth[bk].hit_rate, e.last_hit_rate); }
        }
      }
    }
  } catch (_) {}

  connData = (data.backends || []).map(b => {
    const key = b.provider + ':' + b.backend_model;
    const st = stats[key] || {};
    const circ = circuits[key] || {};
    let state = 'available';
    if (!b.enabled || !b.provider_enabled) state = 'disabled';
    else if (circ.open) state = 'circuit';
    else if (b.cooldown_remaining === -1) state = 'snoozed';
    else if (b.cooldown_remaining > 0) state = 'snoozed';
    const w = warmth[key];
    return {
      model: b.model,
      provider: b.provider,
      provider_name: b.provider_name,
      backend_model: b.backend_model,
      backend_key: key,
      tier: b.priority,
      state,
      inflight: st.in_flight || 0,
      warm: w ? w.hit_rate : null,
      warm_count: w ? w.count : 0,
      latency: st.avg_latency_ms,
      errors: st.failures || 0,
      last_error: st.last_error || '',
      circuit_remaining: circ.open_remaining_s || 0,
      cooldown_remaining: b.cooldown_remaining || 0,
    };
  });
  renderConnections();
}

function stateLabel(s) {
  return { available: 'ready', circuit: 'failing', snoozed: 'paused', disabled: 'off' }[s] || s;
}
function showConnectionsHelp() {
  const rows = [
    ['Ready', 'Working normally and receiving traffic.'],
    ['Failing', 'Recently returned too many errors, so the gateway paused it automatically. It retries on its own after a short cooldown — click Reset to try again immediately.'],
    ['Paused', 'Manually put on hold (snoozed). No traffic is sent until you click Unsnooze. Useful to temporarily route around a flaky backend.'],
    ['Off', 'The provider or this backend is disabled in config. It never receives traffic until re-enabled.'],
  ];
  const body = rows.map(([t, d]) =>
    '<div style="margin-bottom:14px"><span class="badge badge-blue">' + esc(t) + '</span>'
    + '<div style="margin-top:5px;color:var(--text-dim);font-size:0.82rem">' + esc(d) + '</div></div>'
  ).join('');
  openModal({ title: 'What the connection states mean', bodyHtml: body });
}

function renderConnections() {
  // Update filter counts
  const counts = { all: connData.length, available: 0, circuit: 0, snoozed: 0, disabled: 0 };
  connData.forEach(r => { counts[r.state] = (counts[r.state] || 0) + 1; });
  for (const k of ['all','available','circuit','snoozed','disabled']) {
    const el = document.getElementById('cf-' + k);
    if (el) el.textContent = counts[k] || 0;
  }

  let rows = connData.filter(r => connFilter === 'all' || r.state === connFilter);
  const col = connSort.col;
  rows.sort((a, b) => {
    let va, vb;
    if (col === 'model') { va = a.model; vb = b.model; }
    else if (col === 'backend') { va = a.backend_key; vb = b.backend_key; }
    else if (col === 'tier') { va = a.tier; vb = b.tier; }
    else if (col === 'state') { va = a.state; vb = b.state; }
    else if (col === 'inflight') { va = a.inflight; vb = b.inflight; }
    else if (col === 'warm') { va = a.warm || -1; vb = b.warm || -1; }
    else if (col === 'latency') { va = a.latency || 0; vb = b.latency || 0; }
    else if (col === 'errors') { va = a.errors; vb = b.errors; }
    if (typeof va === 'string') return va.localeCompare(vb) * connSort.dir;
    return (va - vb) * connSort.dir;
  });

  const order = { circuit: 0, snoozed: 1, available: 2, disabled: 3 };
  if (col === 'state') rows.sort((a, b) => (order[a.state] - order[b.state]) * connSort.dir);

  const body = document.getElementById('conn-body');
  if (rows.length === 0) {
    body.innerHTML = '<tr><td colspan="9"><div class="empty">No backends match this filter.</div></td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const cls = r.state === 'circuit' ? ' class="row-circuit"' : r.state === 'snoozed' ? ' class="row-snoozed"' : '';
    const dotCls = { available: 'dot-ok', circuit: 'dot-circuit', snoozed: 'dot-snoozed', disabled: 'dot-disabled' }[r.state];
    const warmChip = r.warm != null
      ? '<span class="badge badge-green" title="' + r.warm_count + ' warm fingerprint(s)">warm ' + Math.round(r.warm * 100) + '%</span>'
      : '<span class="badge badge-gray">cold</span>';
    let actions = '';
    if (r.state === 'snoozed') actions = '<button class="secondary btn-sm" data-act="unsnooze" data-bk="' + escAttr(r.backend_key) + '">Unsnooze</button>';
    else if (r.state === 'circuit') actions = '<button class="secondary btn-sm" data-act="reset" data-bk="' + escAttr(r.backend_key) + '">Reset</button>';
    else if (r.state === 'available') actions = '<button class="secondary btn-sm" data-act="snooze" data-bk="' + escAttr(r.backend_key) + '">Snooze</button>';
    const errTip = r.last_error ? ' title="' + escAttr(r.last_error) + '"' : '';
    return '<tr' + cls + '>'
      + '<td><span style="font-weight:600">' + esc(r.model) + '</span></td>'
      + '<td class="mono">' + esc(r.backend_key) + '</td>'
      + '<td><span class="badge badge-purple">t' + r.tier + '</span></td>'
      + '<td><span class="state-dot ' + dotCls + '"></span>' + stateLabel(r.state) + (r.cooldown_remaining > 0 ? ' ' + Math.round(r.cooldown_remaining) + 's' : '') + '</td>'
      + '<td>' + r.inflight + '</td>'
      + '<td>' + warmChip + '</td>'
      + '<td class="mono">' + (r.latency != null ? Math.round(r.latency) + 'ms' : '—') + '</td>'
      + '<td class="mono"' + errTip + '>' + r.errors + '</td>'
      + '<td>' + actions + '</td>'
      + '</tr>';
  }).join('');

  // Sort indicators
  document.querySelectorAll('#conn-table th[data-sort]').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === connSort.col) th.classList.add(connSort.dir === 1 ? 'sort-asc' : 'sort-desc');
  });
}

async function unsnoozeBackend(bk) {
  const i = bk.indexOf(':');
  if (i < 0) return;
  const provider = bk.slice(0, i), model = bk.slice(i + 1);
  const { ok } = await fetchJSON('/admin/backends/unsnooze', {
    method: 'POST', body: JSON.stringify({ provider, model }),
  });
  if (ok) { toast('Unsnoozed ' + bk, 'success'); loadConnections(); }
  else toast('Failed to unsnooze', 'error');
}

async function snoozeBackend(bk) {
  const i = bk.indexOf(':');
  if (i < 0) return;
  const provider = bk.slice(0, i), model = bk.slice(i + 1);
  const { ok } = await fetchJSON('/admin/backends/snooze', {
    method: 'POST', body: JSON.stringify({ provider, model, permanent: true }),
  });
  if (ok) { toast('Snoozed ' + bk, 'info'); loadConnections(); }
  else toast('Failed to snooze', 'error');
}

async function resetCircuit(bk) {
  const i = bk.indexOf(':');
  if (i < 0) return;
  const provider = bk.slice(0, i), model = bk.slice(i + 1);
  const { ok } = await fetchJSON('/admin/circuit/reset', {
    method: 'POST', body: JSON.stringify({ provider, model }),
  });
  if (ok) { toast('Circuit reset for ' + bk, 'success'); loadConnections(); }
  else toast('Failed to reset circuit', 'error');
}

async function bulkUnsnooze() {
  const confirmed = await showConfirm('Unsnooze All', 'Remove every snooze and rate-limit cooldown across all backends.');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/backends/unsnooze-all', { method: 'POST' });
    const d = await r.json();
    if (r.ok) { toast('Cleared ' + (d.cleared || 0) + ' snooze(s)', 'success'); loadConnections(); }
    else toast(d.error || 'Failed', 'error');
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

async function bulkResetCircuits() {
  const confirmed = await showConfirm('Reset All Circuits', 'Close every open circuit breaker across all backends.');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/circuit/reset', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
    const d = await r.json();
    if (r.ok) { toast('Reset ' + (d.cleared || 0) + ' circuit(s)', 'success'); loadConnections(); }
    else toast(d.error || 'Failed', 'error');
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

function refreshConnections() { loadConnections(); }

// C4: filter/sort survive refresh via URL params.
function _syncConnUrl() {
  try {
    const params = new URLSearchParams();
    if (connFilter !== 'all') params.set('filter', connFilter);
    if (connSort.col !== 'model' || connSort.dir !== 1) {
      params.set('sort', connSort.col + (connSort.dir === 1 ? '' : ':desc'));
    }
    const qs = params.toString();
    history.replaceState(null, '', qs ? '/connections?' + qs : '/connections');
  } catch(_) {}
}
function _initConnUrl() {
  try {
    const params = new URLSearchParams(location.search);
    const f = params.get('filter');
    if (f && ['all', 'available', 'circuit', 'snoozed', 'disabled'].includes(f)) connFilter = f;
    const s = params.get('sort');
    if (s) {
      const [col, dir] = s.split(':');
      if (col) connSort.col = col;
      if (dir === 'desc') connSort.dir = -1;
    }
  } catch(_) {}
  // Reflect into the filter buttons after DOM is wired.
  document.querySelectorAll('[data-cf]').forEach(x => x.classList.toggle('on', x.dataset.cf === connFilter));
}

document.addEventListener('DOMContentLoaded', () => {
  loadServerStatus();
  _initConnUrl();
  loadConnections();
  // C8 + L1: 5s poller (hidden-tab gated) with SSE tick for instant refresh.
  // When the event bus is live, the poller backs off to 30s as a safety net.
  const _connRefresh = () => { if (!document.hidden) loadConnections(); };
  LGEvents.onTick(_connRefresh);
  LGEvents.registerPoller(_connRefresh, 5000, 30000);
  document.querySelectorAll('[data-cf]').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('[data-cf]').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      connFilter = b.dataset.cf;
      _syncConnUrl();
      renderConnections();
    });
  });
  document.querySelectorAll('#conn-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const c = th.dataset.col;
      if (connSort.col === c) connSort.dir *= -1;
      else { connSort.col = c; connSort.dir = 1; }
      _syncConnUrl();
      renderConnections();
    });
  });
  document.getElementById('conn-body').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act, bk = btn.dataset.bk;
    if (act === 'unsnooze') unsnoozeBackend(bk);
    else if (act === 'snooze') snoozeBackend(bk);
    else if (act === 'reset') resetCircuit(bk);
  });});
