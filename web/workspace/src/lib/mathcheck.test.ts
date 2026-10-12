// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { checkFormula } from './mathcheck';
import { KATEX_OPTIONS } from './markdown';

describe('isolated math check shares the GUI renderer', () => {
  it('accepts math under the effective bounded macros', () => {
    const result = checkFormula({ id: 'm1', tex: '\\F_{17}', display: false, macros: { '\\F': '\\mathbb{F}' } });
    expect(result).toMatchObject({ status: 'accepted', error: null, remedy: null, engine: '0.18.9', options: KATEX_OPTIONS });
    expect(result).not.toHaveProperty('html');
  });

  it('returns a renderer-tested repair without changing the input', () => {
    const formula = { id: 'm1', tex: '\\frac{a}{', display: false, macros: {} };
    expect(checkFormula(formula)).toMatchObject({ status: 'rejected', remedy: '\\frac{a}{}' });
    expect(formula.tex).toBe('\\frac{a}{');
  });

  it('does not mistake renderer skips for accepted formulas', () => {
    for (const tex of ['x'.repeat(5_001), '\\def\\foo{a}\\foo']) {
      expect(checkFormula({ id: 'm1', tex, display: false, macros: {} })).toMatchObject({ status: 'unavailable', remedy: null });
    }
  });

  it('does not leak a preceding formula\'s macros into another check', () => {
    checkFormula({ id: 'm1', tex: '\\myword', display: false, macros: { '\\myword': 'a' } });
    expect(checkFormula({ id: 'm2', tex: '\\myword', display: false, macros: {} }).status).toBe('rejected');
  });
});
