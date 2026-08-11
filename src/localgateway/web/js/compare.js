// compare.js — side-by-side model comparison
(async function initCompare() {
  const ids = decodeURIComponent(location.pathname.replace(/^\/compare\//, ''));
  const loading = document.getElementById('compare-loading');
  const errEl = document.getElementById('compare-error');
  const wrap = document.getElementById('compare-wrap');

  if (!ids) {
    loading.classList.add('hidden');
    errEl.classList.remove('hidden');
    errEl.innerHTML = '<p>No models selected.</p><a href="/models">Back to catalog</a>';
    return;
  }

  try {
    const { ok, data } = await fetchJSON('/admin/models/compare?ids=' + encodeURIComponent(ids));
    loading.classList.add('hidden');
    if (!ok || data.error) {
      errEl.classList.remove('hidden');
      errEl.innerHTML = '<p>' + esc(data.error || 'Failed to load comparison') + '</p><a href="/models">Back to catalog</a>';
      return;
    }
    const models = data.models || [];
    if (models.length < 2) {
      errEl.classList.remove('hidden');
      errEl.innerHTML = '<p>Need at least 2 models to compare.</p><a href="/models">Back to catalog</a>';
      return;
    }

    const rows = [
      ['ID', m => `<code><a href="/models/${encodeURIComponent(m.id)}">${esc(m.id)}</a></code>`],
      ['Display Name', m => esc(m.display_name || m.id)],
      ['Description', m => esc(m.description || '—')],
      ['Modality', m => esc(m.modality || '—')],
      ['Context', m => {
        const lo = m.context_min, hi = m.context_max;
        if (lo != null && hi != null) return lo === hi ? fmtTokens(lo) : fmtTokens(lo) + '–' + fmtTokens(hi);
        return m.context_length ? fmtTokens(m.context_length) : '—';
      }],
      ['Max Output', m => m.max_output_tokens ? fmtTokens(m.max_output_tokens) : '—'],
      ['Tags', m => esc((m.tags || []).join(', ') || '—')],
      ['Aliases', m => esc((m.aliases || []).join(', ') || '—')],
      ['Capabilities', m => {
        const caps = Object.entries(m.capabilities || {}).filter(([, v]) => v).map(([k]) => k);
        return esc(caps.join(', ') || '—');
      }],
      ['Backends', m => String(m.backend_count ?? 0)],
      ['Requests (7d)', m => fmt(m.requests || 0, 0)],
      ['Success Rate', m => m.success_rate != null ? m.success_rate + '%' : '—'],
      ['Tokens (7d)', m => fmtTokens(m.tokens || 0)],
      ['TPS P50', m => m.tps_p50 != null ? Number(m.tps_p50).toFixed(1) : '—'],
      ['Input $/M', m => fmtPrice(m.input_price)],
      ['Output $/M', m => fmtPrice(m.output_price)],
      ['Enabled', m => m.enabled ? 'Yes' : 'No'],
    ];

    let html = '<div class="table-wrap"><table class="compare-table"><thead><tr><th class="compare-label"></th>';
    models.forEach(m => {
      const avatar = modelAvatar(m.id, m.display_name, 24, m.avatar);
      const enabledBadge = m.enabled === false ? ' <span class="badge badge-gray">off</span>' : '';
      html += `<th class="compare-th">
        <div class="hstack" style="justify-content:flex-start">
          ${avatar}
          <span>${esc(m.display_name || m.id)}</span>
          <button class="copy-btn" data-id="${escAttr(m.id)}" data-stop data-action="copyId(this.dataset.id,this)" title="Copy model ID">⧉</button>
          ${enabledBadge}
        </div>
        <div class="filter-hint" style="margin-top:2px"><code>${esc(m.id)}</code></div>
      </th>`;
    });
    html += '</tr></thead><tbody>';
    rows.forEach(([label, fn]) => {
      html += `<tr><td class="compare-label">${esc(label)}</td>`;
      models.forEach(m => { html += `<td>${fn(m)}</td>`; });
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    wrap.innerHTML = html;
    wrap.classList.remove('hidden');
  } catch (e) {
    loading.classList.add('hidden');
    errEl.classList.remove('hidden');
    errEl.innerHTML = '<p>' + esc(e.message) + '</p><a href="/models">Back to catalog</a>';
  }
})();
