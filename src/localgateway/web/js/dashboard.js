// dashboard.js — dashboard page logic
let usageRangeHours = 24;

async function loadDashboard() {
  loadServerStatus();
  loadDashUsage();
  loadHealth();
}
async function loadDashUsage() {
  try {
    const { data } = await fetchJSON('/admin/usage?hours=24');
    const t = data.total;
    document.getElementById('dash-stats').innerHTML =
      '<div class="stat-cell"><div class="stat-num">'+fmt(t.requests,0)+'</div><div class="stat-cap">Requests (24h)</div></div>'
      + '<div class="stat-cell"><div class="stat-num">'+fmt((t.input_tokens||0)+(t.output_tokens||0),0)+'</div><div class="stat-cap">Tokens</div></div>'
      + '<div class="stat-cell"><div class="stat-num accent">'+fmtCost(t.cost)+'</div><div class="stat-cap">Est. Cost</div></div>'
      + '<div class="stat-cell"><div class="stat-num">'+(t.requests?fmt(t.successes/t.requests*100,1)+'%':'—')+'</div><div class="stat-cap">Success Rate</div></div>';
  } catch(e) { console.error(e); }
}
async function loadHealth() {
  const healthEl = document.getElementById('health-list');
  const rlEl = document.getElementById('ratelimit-list');
  try {
    const { ok, data } = await fetchJSON('/admin/health');
    if (!ok) {
      healthEl.innerHTML = '<div class="notice">Gateway is stopped — start it to see live health.</div>';
      rlEl.innerHTML = '<div class="empty">—</div>';
      return;
    }
    const stats = data.stats || {};
    const rows = (data.backends||[]).map(b => {
      const key = b.provider + ':' + b.backend_model;
      const st = stats[key] || {};
      let badge;
      if (!b.enabled || !b.provider_enabled) badge = '<span class="badge badge-gray">disabled</span>';
      else if (b.cooldown_remaining === -1) badge = '<span class="badge badge-gray">snoozed</span>';
      else if (b.cooldown_remaining > 0) badge = '<span class="badge badge-yellow">rate-limited '+b.cooldown_remaining.toFixed(0)+'s</span>';
      else badge = '<span class="badge badge-green">available</span>';
      const lat = st.avg_latency_ms != null ? fmt(st.avg_latency_ms,0)+'ms' : '—';
      const ttft = st.avg_ttft_ms != null ? fmt(st.avg_ttft_ms,0)+'ms' : '—';
      const rate = st.success_rate != null ? st.success_rate+'%' : '—';
      const errTip = st.last_error ? ' title="'+esc(st.last_error)+'"' : '';
      return '<div class="provider-row"><div class="provider-info">'
        + '<span class="provider-name"><code>'+esc(b.model)+'</code> <span style="color:var(--text-dim)">via</span> '+esc(b.provider_name)+':'+esc(b.backend_model)+'</span>'
        + '<div class="provider-url">tier #'+b.priority+' · lat '+lat+' · ttft '+ttft+' · ok '+rate+'</div>'
        + '</div><span'+errTip+'>'+badge+'</span></div>';
    }).join('');
    healthEl.innerHTML = rows || '<div class="empty">No providers configured</div>';
    const rl = Object.entries(data.rate_limits||{}).map(([k,v]) =>
      '<div class="provider-row"><div class="provider-info"><code>'+esc(k)+'</code></div><span class="badge '+(v < 0 ? 'badge-gray">snoozed' : 'badge-yellow">'+v.toFixed(0)+'s left')+'</span></div>').join('');
    rlEl.innerHTML = rl || '<div class="empty">No active rate limits</div>';
  } catch(e) {
    healthEl.innerHTML = '<div class="notice">Gateway is stopped.</div>';
  }
}

async function savePortFromDash() {
  const port = parseInt(document.getElementById('dash-port-input').value, 10);
  if (!port || port < 1 || port > 65535) { toast('Port must be 1-65535', 'error'); return; }
  try {
    const { data } = await fetchJSON('/admin/config');
    data.server = data.server || {};
    data.server.port = port;
    const { ok } = await fetchJSON('/admin/config', { method: 'PUT', body: JSON.stringify(data) });
    if (ok) toast('Port saved. Restart required.', 'success');
    else toast('Failed to save port', 'error');
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', loadDashboard);