import { describe, it, expect } from 'vitest';
import {
  fmt, fmtCost, fmtPricePrecise, esc, escAttr, fuzzyScore,
  parseDuration, fmtSnoozeDuration, fmtSnoozeDurationRounded,
} from '../../src/localgateway/web/js/pure-helpers.js';

describe('fmt', () => {
  it('formats numbers with locale grouping', () => {
    expect(fmt(1234.567, 2)).toMatch(/1,234\.57/);
  });
  it('treats null/undefined as 0', () => {
    expect(fmt(null)).toBe('0');
    expect(fmt(undefined, 0)).toBe('0');
  });
});

describe('fmtCost', () => {
  it('prepends $ and uses 4 decimal places', () => {
    expect(fmtCost(0.0001)).toMatch(/\$0\.0001/);
  });
});

describe('fmtPricePrecise', () => {
  it('returns — for null/NaN', () => {
    expect(fmtPricePrecise(null)).toBe('—');
    expect(fmtPricePrecise(NaN)).toBe('—');
  });
  it('returns $0 for zero', () => {
    expect(fmtPricePrecise(0)).toBe('$0');
  });
  it('uses 6 decimals for very small prices', () => {
    expect(fmtPricePrecise(0.000001)).toBe('$0.000001');
  });
  it('uses 2 decimals for prices >= 1', () => {
    expect(fmtPricePrecise(5.5)).toBe('$5.50');
  });
});

describe('esc', () => {
  it('escapes HTML special chars', () => {
    expect(esc('<script>alert("x")</script>')).toBe(
      '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;'
    );
  });
  it('escapes ampersands', () => {
    expect(esc('a & b')).toBe('a &amp; b');
  });
  it('handles null/undefined as empty string', () => {
    expect(esc(null)).toBe('');
    expect(esc(undefined)).toBe('');
  });
});

describe('escAttr', () => {
  it('escapes single quotes (esc does not)', () => {
    expect(escAttr("it's")).toBe("it&#39;s");
  });
  it('still escapes double quotes', () => {
    expect(escAttr('say "hi"')).toBe('say &quot;hi&quot;');
  });
});

describe('fuzzyScore', () => {
  it('returns 0 for empty query (matches everything)', () => {
    expect(fuzzyScore('', 'anything')).toBe(0);
  });
  it('returns 0 for exact match', () => {
    expect(fuzzyScore('abc', 'abc')).toBe(0);
  });
  it('returns >= 0 for subsequence match', () => {
    expect(fuzzyScore('gpt', 'gpt-4o')).toBeGreaterThanOrEqual(0);
  });
  it('returns -1 for non-match', () => {
    expect(fuzzyScore('xyz', 'abc')).toBe(-1);
  });
  it('ranks contiguous matches better than scattered', () => {
    const contiguous = fuzzyScore('gpt', 'gpt-4o');
    const scattered = fuzzyScore('gpt', 'g__p___t');
    expect(contiguous).toBeLessThanOrEqual(scattered);
  });
  it('is case-insensitive', () => {
    expect(fuzzyScore('GPT', 'gpt-4o')).toBeGreaterThanOrEqual(0);
    expect(fuzzyScore('gpt', 'GPT-4O')).toBeGreaterThanOrEqual(0);
  });
  it('ranks word-start matches higher', () => {
    const wordStart = fuzzyScore('md', 'model-detail');
    const midWord = fuzzyScore('md', 'amendment');
    expect(wordStart).toBeLessThanOrEqual(midWord);
  });
});

describe('parseDuration', () => {
  it('parses bare numbers as seconds', () => {
    expect(parseDuration('3600')).toBe(3600);
  });
  it('parses minutes', () => {
    expect(parseDuration('30m')).toBe(1800);
  });
  it('parses hours', () => {
    expect(parseDuration('2h')).toBe(7200);
  });
  it('parses days', () => {
    expect(parseDuration('1d')).toBe(86400);
  });
  it('parses weeks', () => {
    expect(parseDuration('1w')).toBe(604800);
  });
  it('parses decimals', () => {
    expect(parseDuration('1.5h')).toBe(5400);
  });
  it('returns null for invalid input', () => {
    expect(parseDuration('')).toBeNull();
    expect(parseDuration('abc')).toBeNull();
  });
  it('is case-insensitive', () => {
    expect(parseDuration('5M')).toBe(300);
    expect(parseDuration('5H')).toBe(18000);
  });
});

describe('fmtSnoozeDuration', () => {
  it('formats seconds', () => {
    expect(fmtSnoozeDuration(30)).toBe('30s');
  });
  it('formats minutes', () => {
    expect(fmtSnoozeDuration(180)).toBe('3m');
  });
  it('formats hours', () => {
    expect(fmtSnoozeDuration(7200)).toBe('2h');
  });
  it('formats days', () => {
    expect(fmtSnoozeDuration(86400)).toBe('1d');
  });
  it('handles permanent (-1)', () => {
    expect(fmtSnoozeDuration(-1)).toBe('permanent');
  });
  it('handles null', () => {
    expect(fmtSnoozeDuration(null)).toBe('—');
  });
});

describe('fmtSnoozeDurationRounded', () => {
  it('rounds small durations to <1m', () => {
    expect(fmtSnoozeDurationRounded(30)).toBe('<1m');
  });
  it('rounds hours', () => {
    expect(fmtSnoozeDurationRounded(7200)).toBe('2h');
  });
  it('handles permanent', () => {
    expect(fmtSnoozeDurationRounded(-1)).toBe('permanent');
  });
});
