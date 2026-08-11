// pure-helpers.js — pure functions with no DOM/fetch dependencies.
// Extracted from shared.js and model_detail.js so they can be unit-tested
// under Node/vitest. The original files re-assign these as globals on load.

export function fmt(n, d = 2) {
  return (n || 0).toLocaleString(undefined, { maximumFractionDigits: d });
}

export function fmtCost(n) {
  return '$' + fmt(n, 4);
}

export function fmtPricePrecise(p) {
  if (p == null || isNaN(p)) return '—';
  if (p === 0) return '$0';
  if (p < 0.001) return '$' + p.toFixed(6);
  if (p < 1) return '$' + p.toFixed(4);
  return '$' + p.toFixed(2);
}

export function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function escAttr(s) {
  return esc(s).replace(/'/g, '&#39;');
}

// Subsequence fuzzy scorer: returns a score >= 0 if every char of `q` appears
// in `text` in order, else -1. Lower score = better match; contiguous runs
// (and matches at word starts) rank higher.
export function fuzzyScore(q, text) {
  const t = String(text || '').toLowerCase();
  q = String(q || '').toLowerCase();
  if (!q) return 0;
  if (t === q) return 0;
  let score = 0, qi = 0, last = -2;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) {
      score += (i === last + 1) ? 0 : (i === 0 || t[i - 1] === ' ' || t[i - 1] === '-' || t[i - 1] === '_' ? 0 : 3);
      last = i;
      qi++;
    }
  }
  if (qi < q.length) return -1;
  return score + t.length - q.length;
}

// Parse a human duration string like "30m", "2h", "1d", "3600" into seconds.
export function parseDuration(str) {
  if (!str) return null;
  str = String(str).trim().toLowerCase();
  const m = str.match(/^(\d+(?:\.\d+)?)\s*(s|m|h|d|w)?$/);
  if (!m) return null;
  const n = parseFloat(m[1]);
  const unit = m[2] || 's';
  const mult = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 }[unit];
  return Math.round(n * mult);
}

// Format a duration in seconds as a human-readable string.
export function fmtSnoozeDuration(s) {
  if (s == null) return '—';
  if (s < 0) return 'permanent';
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600 * 10) / 10 + 'h';
  return Math.round(s / 86400 * 10) / 10 + 'd';
}

export function fmtSnoozeDurationRounded(s) {
  if (s == null) return '—';
  if (s < 0) return 'permanent';
  if (s < 60) return '<1m';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}
