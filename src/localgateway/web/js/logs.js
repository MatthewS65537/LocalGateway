// logs.js — logs page logic
let logTimer = null;
let logSearchDebounce = null;
let _newestLogId = null;   // highest id seen; incremental polls only fetch newer rows
let _logListEl = null;

function _logRowHtml(l) {
  const metaObj = (l.meta && typeof l.meta === 'object') ? l.meta : {};
  const meta = [
    l.model, l.provider,
    l.latency_ms!=null?l.latency_ms+'ms':null,
    metaObj.ttft_ms!=null?'ttft '+metaObj.ttft_ms+'ms':null,
    metaObj.routing_reason?'why: '+metaObj.routing_reason:null,
    metaObj.reasoning_tokens!=null?'reasoning '+metaObj.reasoning_tokens:null,
    metaObj.input_tokens!=null?'in '+metaObj.input_tokens:null,
    metaObj.output_tokens!=null?'out '+metaObj.output_tokens:null,
  ].filter(Boolean).join(' · ');
  // C2: request_id chip — click filters the view to that request's full
  // fallback timeline (the F2 trace feature was backend-only until now).
  const rid = metaObj.request_id;
  const ridChip = rid
    ? '<span class="rid-chip" data-rid="'+escAttr(rid)+'" title="Trace this request (click to filter)" style="cursor:pointer;font-family:var(--font-mono);font-size:0.72rem;color:var(--accent-hover);background:var(--accent-surface);border-radius:6px;padding:0 6px;margin-left:8px">⧉ '+esc(rid)+'</span>'
    : '';
  return '<div class="log-row">'
    + '<span class="log-time" data-ts="' + (l.ts || '') + '">'+fmtTime(l.ts)+'</span>'
    + '<span class="lv lv-'+esc(l.level)+'">'+esc(l.level)+'</span>'
    + '<span class="log-msg" title="'+esc(l.message)+'">'+esc(l.message)+'</span>'
    + '<span class="log-meta">'+esc(meta)+'</span>'
    + ridChip
    + '</div>';
}

function _trackNewest(logs) {
  for (const l of logs) if (l.id != null && (_newestLogId === null || l.id > _newestLogId)) _newestLogId = l.id;
}

function _renderLogs(logs, { incremental = false } = {}) {
  _logListEl = _logListEl || document.getElementById('log-list');
  if (!_logListEl) return;
  if (logs.length === 0) {
    if (!incremental) _logListEl.innerHTML = '<div class="empty">No log entries.</div>';
    return;
  }
  if (incremental) {
    // P11: append only the NEW rows (server returns newest-first; we prepend)
    // instead of replacing innerHTML — the old full re-render collapsed the
    // scroll position every 3s and replayed the slide animation on all 200 rows.
    const frag = document.createDocumentFragment();
    for (const l of logs) {
      const div = document.createElement('div');
      div.innerHTML = _logRowHtml(l);
      frag.appendChild(div.firstChild);
    }
    _logListEl.prepend(frag);
  } else {
    _logListEl.innerHTML = logs.map(_logRowHtml).join('');
  }
  // Cap DOM rows at 250 so a long-lived tab doesn't accumulate unboundedly.
  while (_logListEl.children.length > 250) _logListEl.removeChild(_logListEl.lastChild);
}

function debouncedLoadLogs() {
  clearTimeout(logSearchDebounce);
  logSearchDebounce = setTimeout(() => { _newestLogId = null; loadLogs(); }, 300);
}
async function loadLogs({ incremental = false } = {}) {
  const level = document.getElementById('log-level').value;
  const search = document.getElementById('log-search').value.trim();
  const requestId = document.getElementById('log-request-id').value.trim();
  let url = '/admin/logs?limit=200';
  if (level) url += '&level=' + encodeURIComponent(level);
  if (search) url += '&search=' + encodeURIComponent(search);
  if (requestId) url += '&request_id=' + encodeURIComponent(requestId);
  // C4: filters survive refresh / are shareable via the URL.
  try {
    const params = new URLSearchParams();
    if (level) params.set('level', level);
    if (search) params.set('q', search);
    if (requestId) params.set('request_id', requestId);
    const qs = params.toString();
    history.replaceState(null, '', qs ? '/logs?' + qs : '/logs');
  } catch(_) {}
  if (incremental && _newestLogId != null) url += '&since_id=' + _newestLogId;
  const { ok, data } = await apiFetch(url, { silent: true });
  if (!ok) { loadError('logs'); return; }
  const logs = (data && data.logs) || [];
  _trackNewest(logs);
  _renderLogs(logs, { incremental });
}
function toggleLogAutoRefresh() {
  setupLogTimer();
}
function setupLogTimer() {
  if (logTimer) { if (logTimer.stop) logTimer.stop(); else clearInterval(logTimer); logTimer = null; }
  const on = document.getElementById('log-autorefresh').checked;
  if (on) {
    // L1: SSE tick triggers instant incremental fetch; poller backs off to 30s.
    const _logRefresh = () => {
      if (!document.hidden && document.getElementById('log-list')) loadLogs({ incremental: true });
    };
    LGEvents.onTick(_logRefresh);
    logTimer = LGEvents.registerPoller(_logRefresh, 3000, 30000);
  }
}
async function clearLogs() {
  if (!await confirm2('Clear all log entries?')) return;
  await fetch('/admin/logs', { method: 'DELETE' });
  _newestLogId = null;
  await loadLogs();
  toast('Logs cleared', 'info');
}

// C6: pagination — the view was hardcoded to the newest 200 rows with no way
// to reach older entries. Fetch rows older than the oldest row currently shown
// and append them.
async function loadOlderLogs() {
  const list = document.getElementById('log-list');
  if (!list || !list.children.length) return;
  const firstRow = list.querySelector('.log-row');
  if (!firstRow) return;
  // The endpoint is newest-first; the last DOM row is the oldest we have.
  // Track oldest id via the API: fetch a big batch and find our boundary.
  const level = document.getElementById('log-level').value;
  const search = document.getElementById('log-search').value.trim();
  const requestId = document.getElementById('log-request-id').value.trim();
  let url = '/admin/logs?limit=500';
  if (level) url += '&level=' + encodeURIComponent(level);
  if (search) url += '&search=' + encodeURIComponent(search);
  if (requestId) url += '&request_id=' + encodeURIComponent(requestId);
  const { ok, data } = await apiFetch(url, { silent: true });
  if (!ok) return;
  const logs = (data && data.logs) || [];
  const lastRow = list.querySelector('.log-row:last-child');
  const lastTs = lastRow ? parseFloat((lastRow.querySelector('.log-time') || {}).getAttribute && lastRow.querySelector('.log-time').getAttribute('data-ts')) : null;
  const older = logs.filter(l => lastTs == null || !isNaN(lastTs) && (l.ts || 0) < lastTs);
  if (!older.length) {
    toast('No older entries', 'info');
    return;
  }
  const frag = document.createDocumentFragment();
  for (const l of older) {
    const div = document.createElement('div');
    div.innerHTML = _logRowHtml(l);
    frag.appendChild(div.firstChild);
  }
  list.appendChild(frag);
  _trackNewest(logs);
}

document.addEventListener('DOMContentLoaded', () => {
  // C2/C4: deep-link into a trace (or a filtered view) via ?request_id= etc.
  try {
    const params = new URLSearchParams(location.search);
    const rid = params.get('request_id');
    const level = params.get('level');
    const q = params.get('q');
    if (rid && document.getElementById('log-request-id')) document.getElementById('log-request-id').value = rid;
    if (level && document.getElementById('log-level')) document.getElementById('log-level').value = level;
    if (q != null && document.getElementById('log-search')) document.getElementById('log-search').value = q;
  } catch(_) {}
  loadServerStatus();
  loadLogs();
  setupLogTimer();
});

// Clicking a request-id chip filters to that request's timeline.
document.addEventListener('click', (e) => {
  const chip = e.target.closest && e.target.closest('[data-rid]');
  if (!chip) return;
  const rid = chip.getAttribute('data-rid');
  const input = document.getElementById('log-request-id');
  if (input) {
    input.value = rid;
    _newestLogId = null;
    loadLogs();
    input.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
});
