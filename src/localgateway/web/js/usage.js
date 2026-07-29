// usage.js — usage page logic
let usageRangeHours = 24;

function setUsageRange(hours, btn) {
  usageRangeHours = hours;
  document.querySelectorAll('#usage-range button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadUsage();
}
async function loadUsage() {
  try {
    const { data } = await fetchJSON('/admin/usage?hours=' + usageRangeHours);
    const t = data.total;
    document.getElementById('usage-stats').innerHTML =
      '<div class="stat-cell"><div class="stat-num">'+fmt(t.requests,0)+'</div><div class="stat-cap">Requests</div></div>'
      + '<div class="stat-cell"><div class="stat-num">'+fmt(t.input_tokens||0,0)+'</div><div class="stat-cap">Input Tokens</div></div>'
      + '<div class="stat-cell"><div class="stat-num">'+fmt(t.output_tokens||0,0)+'</div><div class="stat-cap">Output Tokens</div></div>'
      + '<div class="stat-cell"><div class="stat-num">'+fmt(t.cached_tokens||0,0)+'</div><div class="stat-cap">Cached Tokens</div></div>'
      + '<div class="stat-cell"><div class="stat-num">'+fmt(t.reasoning_tokens||0,0)+'</div><div class="stat-cap">Reasoning Tokens</div></div>'
      + '<div class="stat-cell"><div class="stat-num accent">'+fmtCost(t.cost)+'</div><div class="stat-cap">Est. Cost</div></div>';
    const pp = Object.entries(data.by_provider||{}).map(([k,v]) =>
      '<tr><td>'+esc(k)+'</td><td>'+fmt(v.requests,0)+'</td><td>'+fmt((v.input_tokens||0)+(v.output_tokens||0),0)+'</td><td>'+fmt(v.cached_tokens||0,0)+'</td><td>'+fmtCost(v.cost)+'</td></tr>').join('');
    document.getElementById('usage-provider').innerHTML = pp || '<tr><td colspan="5" class="empty">No data</td></tr>';
    const mp = Object.entries(data.by_model||{}).map(([k,v]) =>
      '<tr><td>'+esc(k)+'</td><td>'+fmt(v.requests,0)+'</td><td>'+fmt((v.input_tokens||0)+(v.output_tokens||0),0)+'</td><td>'+fmt(v.cached_tokens||0,0)+'</td><td>'+fmtCost(v.cost)+'</td></tr>').join('');
    document.getElementById('usage-model').innerHTML = mp || '<tr><td colspan="5" class="empty">No data</td></tr>';
    const bp = Object.entries(data.by_backend||{}).map(([k,v]) =>
      '<tr><td><code>'+esc(k)+'</code></td><td>'+fmt(v.requests,0)+'</td>'
      + '<td>'+(v.requests?fmt(v.successes/v.requests*100,0)+'%':'—')+'</td>'
      + '<td>'+fmt(v.input_tokens||0,0)+'</td>'
      + '<td>'+fmt(v.output_tokens||0,0)+'</td>'
      + '<td>'+fmt(v.cached_tokens||0,0)+'</td>'
      + '<td>'+fmt(v.cache_write_tokens||0,0)+'</td>'
      + '<td>'+fmt((v.input_tokens||0)+(v.output_tokens||0),0)+'</td>'
      + '<td>'+fmtCost(v.cost)+'</td></tr>').join('');
    document.getElementById('usage-backend').innerHTML = bp || '<tr><td colspan="9" class="empty">No data</td></tr>';
  } catch(e) { console.error(e); }
}

document.addEventListener('DOMContentLoaded', loadUsage);