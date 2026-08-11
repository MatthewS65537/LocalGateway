// usage.js — usage page logic
let usageRangeHours = 24;
let usageSeriesCache = null;
let usageMetric = 'cost';
let usageKeyColors = {};

const USAGE_COLORS = ['#6366f1', '#34d399', '#fbbf24', '#f87171', '#a78bfa', '#60a5fa', '#34d399', '#f472b6'];

function setUsageRange(hours, btn) {
  usageRangeHours = hours;
  document.querySelectorAll('#usage-range button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _syncUsageUrl();
  loadUsage();
}

function setUsageMetric(metric, btn) {
  usageMetric = metric;
  document.querySelectorAll('.usage-metric-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (usageSeriesCache) renderUsageChart(usageSeriesCache);
}

// C4: the range/metric are memory-only today — sync them to the URL so
// views survive refresh and are shareable.
function _syncUsageUrl() {
  try {
    const params = new URLSearchParams();
    if (usageRangeHours !== 24) params.set('hours', String(usageRangeHours));
    if (usageMetric !== 'cost') params.set('metric', usageMetric);
    const qs = params.toString();
    history.replaceState(null, '', qs ? '/usage?' + qs : '/usage');
  } catch(_) {}
}
function _initUsageUrl() {
  try {
    const params = new URLSearchParams(location.search);
    const hours = parseInt(params.get('hours'), 10);
    if (hours && [24, 168, 720].includes(hours)) usageRangeHours = hours;
    const metric = params.get('metric');
    if (metric === 'tokens' || metric === 'requests') usageMetric = metric;
  } catch(_) {}
  // Apply to the buttons on first load (the DOM handlers run after this).
  setTimeout(() => {
    document.querySelectorAll('#usage-range button').forEach(b => {
      if (parseInt(b.dataset.hours || b.dataset.value || b.textContent, 10) === usageRangeHours) {
        document.querySelectorAll('#usage-range button').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
      }
    });
    document.querySelectorAll('.usage-metric-btn').forEach(b => {
      if ((b.dataset.metric || '') === usageMetric) {
        document.querySelectorAll('.usage-metric-btn').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
      }
    });
  }, 0);
}

async function loadUsage() {
  const loading = document.getElementById('usage-loading');
  if (loading) loading.classList.remove('hidden');
  const [usageR, seriesR] = await Promise.all([
    apiFetch('/admin/usage?hours=' + usageRangeHours, { silent: true }),
    apiFetch('/admin/usage/series?hours=' + usageRangeHours, { silent: true }),
  ]);
  if (loading) loading.classList.add('hidden');
  if (!usageR.ok) { loadError('usage data'); return; }
  const data = usageR.data;
  if (seriesR.ok) usageSeriesCache = seriesR.data;
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
    '<tr><td><a href="/models/'+encodeURIComponent(k)+'">'+esc(k)+'</a></td><td>'+fmt(v.requests,0)+'</td><td>'+fmt((v.input_tokens||0)+(v.output_tokens||0),0)+'</td><td>'+fmt(v.cached_tokens||0,0)+'</td><td>'+fmtCost(v.cost)+'</td></tr>').join('');
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

  // Per-key / per-user tables (F4/F5 attribution).
  const kp = Object.entries(data.by_key||{}).map(([k,v]) =>
    '<tr><td><code>'+esc(k)+'</code></td><td>'+fmt(v.requests,0)+'</td><td>'+fmt((v.input_tokens||0)+(v.output_tokens||0),0)+'</td><td>'+fmtCost(v.cost)+'</td></tr>').join('');
  const keysEl = document.getElementById('usage-keys');
  if (keysEl) keysEl.innerHTML = kp || '<tr><td colspan="4" class="empty">No key-attributed requests in this window</td></tr>';

  const up = Object.entries(data.by_user||{}).map(([k,v]) =>
    '<tr><td><code>'+esc(k)+'</code></td><td>'+fmt(v.requests,0)+'</td><td>'+fmt((v.input_tokens||0)+(v.output_tokens||0),0)+'</td><td>'+fmtCost(v.cost)+'</td></tr>').join('');
  const usersEl = document.getElementById('usage-users');
  if (usersEl) usersEl.innerHTML = up || '<tr><td colspan="4" class="empty">No per-user requests in this window</td></tr>';

  if (usageSeriesCache) renderUsageChart(usageSeriesCache);
}

// ---------- cost/tokens over time (stacked, per provider) ----------
function renderUsageChart(data) {
  const wrap = document.getElementById('usage-chart');
  const series = data.series || {};
  const providers = Object.keys(series);
  if (!providers.length) { wrap.innerHTML = '<div class="empty">No usage in this window yet.</div>'; return; }
  // Merge all buckets across providers.
  const buckets = new Map();
  providers.forEach(p => (series[p] || []).forEach(pt => {
    if (!buckets.has(pt.t)) buckets.set(pt.t, { t: pt.t, providers: {} });
    buckets.get(pt.t).providers[p] = pt;
  }));
  const times = [...buckets.keys()].sort((a, b) => a - b);
  if (!times.length) { wrap.innerHTML = '<div class="empty">No usage in this window yet.</div>'; return; }

  const W = 900, H = 220, PL = 52, PR = 12, PT = 12, PB = 26;
  const isCost = usageMetric === 'cost';
  let max = 0;
  const totals = new Map();
  providers.forEach(p => { totals.set(p, 0); });
  times.forEach(t => {
    const pt = buckets.get(t);
    let sum = 0;
    providers.forEach(p => {
      const v = pt.providers[p] ? (isCost ? pt.providers[p].cost : pt.providers[p].tokens) : 0;
      sum += v; totals.set(p, (totals.get(p) || 0) + v);
    });
    if (sum > max) max = sum;
  });
  max *= 1.12;
  if (max <= 0) { wrap.innerHTML = '<div class="empty">No usage in this window yet.</div>'; return; }
  const x = t => PL + (times.length === 1 ? 0.5 : (t - times[0]) / (times[times.length - 1] - times[0])) * (W - PL - PR);
  const y = v => PT + (1 - v / max) * (H - PT - PB);

  let svg = '';
  for (let g = 0; g <= 4; g++) {
    const v = max * g / 4, yy = y(v).toFixed(1);
    svg += '<line x1="'+PL+'" y1="'+yy+'" x2="'+(W-PR)+'" y2="'+yy+'" stroke="var(--border)" stroke-width="1"/>'
      + '<text x="'+(PL-7)+'" y="'+(+yy+3)+'" text-anchor="end" font-size="9" fill="var(--text-dim)">'+(isCost?'$':'')+Math.round(v)+'</text>';
  }
  // x labels: up to 6 ticks
  const step = Math.max(1, Math.floor(times.length / 6));
  const fmtTick = t => {
    const d = new Date(t * 1000);
    if (usageRangeHours <= 48) return (d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':00';
    return (d.getMonth()+1)+'/'+d.getDate();
  };
  times.forEach((t, i) => {
    if (i % step === 0 || i === times.length - 1) {
      svg += '<text x="'+x(t).toFixed(1)+'" y="'+(H-8)+'" text-anchor="middle" font-size="9" fill="var(--text-dim)">'+fmtTick(t)+'</text>';
    }
  });

  // Stacked bars.
  providers.forEach((p, pi) => {
    const color = USAGE_COLORS[pi % USAGE_COLORS.length];
    usageKeyColors[p] = color;
    times.forEach(t => {
      const pt = buckets.get(t);
      if (!pt.providers[p]) return;
      const v = isCost ? pt.providers[p].cost : pt.providers[p].tokens;
      if (v <= 0) return;
      // Accumulate the providers below this one for the stack.
      let acc = 0;
      for (let j = 0; j < pi; j++) {
        const q = buckets.get(t).providers[providers[j]];
        if (q) acc += isCost ? q.cost : q.tokens;
      }
      const h = Math.max(1, (H - PT - PB) * v / max);
      const y0 = y(acc + v);
      svg += '<rect x="'+(x(t) - 4)+'" y="'+y0+'" width="8" height="'+h+'" fill="'+color+'" rx="1">'
        + '<title>'+esc(p)+' · '+fmt(v, isCost ? 4 : 0)+'</title></rect>';
    });
  });
  const legend = providers.sort((a, b) => (totals.get(b) || 0) - (totals.get(a) || 0)).map(p =>
    '<span class="lg-item"><span class="lg-swatch" style="background:'+usageKeyColors[p]+'"></span><code>'+esc(p)+'</code></span>'
  ).join('');
  wrap.innerHTML = '<div class="chart-wrap"><svg viewBox="0 0 '+W+' '+H+'">'+svg+'</svg></div><div class="chart-legend">'+legend+'</div>';
}

async function exportUsage(format) {
  try {
    // C7: the export API supports include_probes (H3) but the UI couldn't
    // pass it — add the toggle.
    const includeProbes = (document.getElementById('usage-export-probes') || {}).checked;
    const url = '/admin/usage/export?hours=' + usageRangeHours + '&format=' + format
      + (includeProbes ? '&include_probes=true' : '');
    if (format === 'csv') {
      const r = await fetch(url);
      if (!r.ok) { toast('Export failed', 'error'); return; }
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'localgateway-usage-' + usageRangeHours + 'h.csv';
      a.click();
      URL.revokeObjectURL(a.href);
    } else {
      window.open(url, '_blank');
    }
  } catch(e) { toast('Export failed', 'error'); }
}

document.addEventListener('DOMContentLoaded', () => { _initUsageUrl(); loadUsage(); });
