// logs.js — logs page logic
let logTimer = null;
let logSearchDebounce = null;

function debouncedLoadLogs() {
  clearTimeout(logSearchDebounce);
  logSearchDebounce = setTimeout(loadLogs, 300);
}
async function loadLogs() {
  const level = document.getElementById('log-level').value;
  const search = document.getElementById('log-search').value.trim();
  let url = '/admin/logs?limit=200';
  if (level) url += '&level=' + encodeURIComponent(level);
  if (search) url += '&search=' + encodeURIComponent(search);
  try {
    const { data } = await fetchJSON(url);
    const logs = (data && data.logs) || [];
    const el = document.getElementById('log-list');
    if (logs.length === 0) {
      el.innerHTML = '<div class="empty">No log entries.</div>';
      return;
    }
    el.innerHTML = logs.map(l => {
      const metaObj = (l.meta && typeof l.meta === 'object') ? l.meta : {};
      const meta = [
        l.model, l.provider,
        l.latency_ms!=null?l.latency_ms+'ms':null,
        metaObj.ttft_ms!=null?'ttft '+metaObj.ttft_ms+'ms':null,
        metaObj.reasoning_tokens!=null?'reasoning '+metaObj.reasoning_tokens:null,
        metaObj.input_tokens!=null?'in '+metaObj.input_tokens:null,
        metaObj.output_tokens!=null?'out '+metaObj.output_tokens:null,
      ].filter(Boolean).join(' · ');
      return '<div class="log-row">'
        + '<span class="log-time">'+fmtTime(l.ts)+'</span>'
        + '<span class="lv lv-'+esc(l.level)+'">'+esc(l.level)+'</span>'
        + '<span class="log-msg" title="'+esc(l.message)+'">'+esc(l.message)+'</span>'
        + '<span class="log-meta">'+esc(meta)+'</span>'
        + '</div>';
    }).join('');
  } catch(e) { console.error(e); }
}
function toggleLogAutoRefresh() {
  setupLogTimer();
}
function setupLogTimer() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  const on = document.getElementById('log-autorefresh').checked;
  if (on) logTimer = setInterval(() => {
    if (document.getElementById('log-list')) loadLogs();
  }, 3000);
}
async function clearLogs() {
  if (!await confirm2('Clear all log entries?')) return;
  await fetch('/admin/logs', { method: 'DELETE' });
  await loadLogs();
  toast('Logs cleared', 'info');
}

document.addEventListener('DOMContentLoaded', () => {
  loadServerStatus();
  loadLogs();
  setupLogTimer();
});