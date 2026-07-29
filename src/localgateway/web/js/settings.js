// settings.js — settings page logic
function loadSettingsPage() {
  loadServerSettings();
  loadRoutingSettings();
  loadProbeSettings();
  loadDisplaySettings();
}

async function loadServerSettings() {
  try {
    const { data } = await fetchJSON('/admin/config');
    const port = data.server?.port || 8080;
    document.getElementById('settings-port').value = port;
    const keyInput = document.getElementById('settings-api-key');
    keyInput.value = data.server?.api_key || '';
    keyInput.type = 'password';
    const showBtn = keyInput.nextElementSibling;
    if (showBtn) showBtn.textContent = 'Show';
  } catch(e) { console.error(e); }
}

function toggleApiKeyVisibility() {
  const input = document.getElementById('settings-api-key');
  const btn = input.nextElementSibling;
  if (input.type === 'password') {
    input.type = 'text';
    if (btn) btn.textContent = 'Hide';
  } else {
    input.type = 'password';
    if (btn) btn.textContent = 'Show';
  }
}

async function copyApiKey() {
  const val = document.getElementById('settings-api-key').value;
  if (!val) { toast('No API key set', 'info'); return; }
  try {
    await navigator.clipboard.writeText(val);
    toast('API key copied', 'success');
  } catch(e) {
    toast('Could not copy', 'error');
  }
}

async function saveServerSettings() {
  const port = parseInt(document.getElementById('settings-port').value, 10);
  if (!port || port < 1 || port > 65535) { toast('Port must be 1-65535', 'error'); return; }
  const apiKey = document.getElementById('settings-api-key').value.trim();
  try {
    const { data } = await fetchJSON('/admin/config');
    data.server = data.server || {};
    data.server.port = port;
    data.server.api_key = apiKey || null;
    const { ok } = await fetchJSON('/admin/config', { method: 'PUT', body: JSON.stringify(data) });
    if (ok) toast('Server settings saved. Restart required for port changes.', 'success');
    else toast('Failed to save server settings', 'error');
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function loadRoutingSettings() {
  try {
    const { data } = await fetchJSON('/admin/config');
    const mode = data.server?.routing_mode || 'explore';
    const decay = data.server?.routing_decay || 0.4;
    document.getElementById('settings-routing-mode').value = mode;
    document.getElementById('settings-routing-decay').value = decay;
    updateDecayViz();
  } catch(e) { console.error(e); }
}

function updateDecayViz() {
  const mode = document.getElementById('settings-routing-mode').value;
  const decay = parseFloat(document.getElementById('settings-routing-decay').value) || 0.4;
  document.getElementById('decay-value').textContent = decay.toFixed(2);
  const section = document.getElementById('decay-section');
  section.style.display = mode === 'explore' ? 'block' : 'none';

  const viz = document.getElementById('decay-viz');
  viz.innerHTML = '';
  for (let tiers = 2; tiers <= 5; tiers++) {
    const weights = [];
    for (let i = 0; i < tiers; i++) weights.push(Math.pow(decay, i));
    const sum = weights.reduce((a, b) => a + b, 0);
    const norm = weights.map(w => (w / sum * 100));

    const bar = document.createElement('div');
    bar.style.cssText = 'display:grid;grid-template-columns:70px 1fr;gap:10px;align-items:center';
    bar.innerHTML = `<span style="font-size:0.72rem;color:var(--text-dim)">${tiers} tiers</span>
      <div style="display:flex;gap:2px;height:28px;border-radius:6px;overflow:hidden;background:var(--surface-3)">
        ${norm.map((p, i) => `<div style="width:${p}%;background:${['var(--accent)','var(--green)','var(--yellow)','var(--red)','var(--text-dim)'][i]};display:flex;align-items:center;justify-content:center;font-size:0.68rem;font-weight:600;color:rgba(0,0,0,0.7);overflow:hidden" title="Tier ${i+1}: ${p.toFixed(1)}%">${p >= 8 ? p.toFixed(0)+'%' : ''}</div>`).join('')}
      </div>`;
    viz.appendChild(bar);
  }
}

async function saveRoutingSettings() {
  const mode = document.getElementById('settings-routing-mode').value;
  const decay = parseFloat(document.getElementById('settings-routing-decay').value) || 0.4;
  try {
    const { data } = await fetchJSON('/admin/config');
    data.server = data.server || {};
    data.server.routing_mode = mode;
    data.server.routing_decay = decay;
    const { ok } = await fetchJSON('/admin/config', { method: 'PUT', body: JSON.stringify(data) });
    if (ok) toast('Routing settings saved', 'success');
    else toast('Failed to save routing settings', 'error');
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function loadProbeSettings() {
  try {
    const { data } = await fetchJSON('/admin/config');
    const enabled = data.server?.probe_enabled !== false;
    const interval = data.server?.probe_interval_s || 3600;
    const maxTokens = data.server?.probe_max_tokens || 64;
    document.getElementById('settings-probe-enabled').checked = enabled;
    document.getElementById('settings-probe-interval').value = interval;
    document.getElementById('settings-probe-max-tokens').value = maxTokens;
    toggleProbeSettings();
  } catch(e) { console.error(e); }
}

function toggleProbeSettings() {
  const enabled = document.getElementById('settings-probe-enabled').checked;
  document.getElementById('probe-options').style.opacity = enabled ? '1' : '0.5';
  document.getElementById('probe-options').style.pointerEvents = enabled ? 'auto' : 'none';
}

async function saveProbeSettings() {
  const enabled = document.getElementById('settings-probe-enabled').checked;
  const interval = parseInt(document.getElementById('settings-probe-interval').value, 10);
  const maxTokens = parseInt(document.getElementById('settings-probe-max-tokens').value, 10);
  try {
    const { data } = await fetchJSON('/admin/config');
    data.server = data.server || {};
    data.server.probe_enabled = enabled;
    data.server.probe_interval_s = interval;
    data.server.probe_max_tokens = maxTokens;
    const { ok } = await fetchJSON('/admin/config', { method: 'PUT', body: JSON.stringify(data) });
    if (ok) toast('Probe settings saved', 'success');
    else toast('Failed to save probe settings', 'error');
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function loadDisplaySettings() {
  try {
    const { data } = await fetchJSON('/admin/config');
    const enabled = data.server?.chart_enabled !== false;
    document.getElementById('settings-chart-enabled').checked = enabled;
  } catch(e) { console.error(e); }
}

async function saveDisplaySettings() {
  try {
    const { data } = await fetchJSON('/admin/config');
    data.server = data.server || {};
    data.server.chart_enabled = document.getElementById('settings-chart-enabled').checked;
    const r = await fetch('/admin/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
    });
    if (r.ok) toast('Display settings saved', 'success');
    else toast('Failed to save display settings', 'error');
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function clearUsageData() {
  const confirmed = await showConfirm('Clear Usage Data', 'This will permanently delete all token usage history. This action cannot be undone.');
  if (!confirmed) return;
  try {
    const r = await fetch('/admin/usage', { method: 'DELETE' });
    if (r.ok) {
      toast('Usage data cleared', 'success');
    } else {
      const d = await r.json();
      toast(d.error || 'Failed to clear usage data', 'error');
    }
  } catch(e) {
    toast('Error: ' + e.message, 'error');
  }
}

document.addEventListener('DOMContentLoaded', loadSettingsPage);