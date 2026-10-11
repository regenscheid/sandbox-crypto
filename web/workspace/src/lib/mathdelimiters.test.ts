// SPDX-License-Identifier: MIT
// LaTeX's own delimiters beside dollar math. The tests under "normalization" are adapted
// from cscience-crypto's test/unit/web-math.test.mjs and test/unit/markdown-math.test.mjs,
// contributed under the MIT License by its author. The original rewrote the text into its
// dollar form; here each case asserts that the parser reads the text as it reads that
// dollar form, in this workspace's dialect.
import { describe, expect, it } from 'vitest';
import markdownit from 'markdown-it';
import type { Token } from 'markdown-it';
import { dollarMath, MAX_BLOCK_LINES } from './dollarmath';
import dollarFixtures from './dollarmath.fixtures.json';
import fixtures from './mathdelimiters.fixtures.json';
import { MATH_RULES, parser } from './markdown';
import type { MathMeta } from './mathdelimiters';

const MATH = new Set(['math_inline', 'math_inline_double', 'math_block', 'math_block_label']);

function tokens(markdown: string, using = parser): Token[] {
  const found: Token[] = [];
  const visit = (list: Token[]) => {
    for (const token of list) {
      if (MATH.has(token.type)) found.push(token);
      if (token.children) visit(token.children);
    }
  };
  visit(using.parse(markdown, {}));
  return found;
}

/** Each formula as [type, content, markup, close]. */
function spans(markdown: string): [string, string, string, string][] {
  return tokens(markdown).map((token) => [
    token.type,
    token.content,
    token.markup,
    (token.meta as MathMeta | null)?.close ?? token.markup,
  ]);
}

/** Each formula as the renderer sees it: whether it is displayed, and its LaTeX. */
function math(markdown: string): [boolean, string][] {
  return tokens(markdown).map((token) => [token.type !== 'math_inline', token.content.trim()]);
}

describe('math spans', () => {
  for (const { name, markdown, math: expected } of fixtures.cases) {
    it(name, () => {
      expect(spans(markdown)).toEqual(expected);
    });
  }
});

describe('normalization', () => {
  it('reads backslash delimiters as the dollar forms', () => {
    const input = 'Inline \\(\\lambda = 2^{128}\\) and display:\n\\[\n\\Pr[X] \\approx q^2\n\\]\nafter';
    const dollars = 'Inline $\\lambda = 2^{128}$ and display:\n\n$$\n\\Pr[X] \\approx q^2\n$$\nafter';
    expect(math(input)).toEqual([
      [false, '\\lambda = 2^{128}'],
      [true, '\\Pr[X] \\approx q^2'],
    ]);
    expect(math(input)).toEqual(math(dollars));
    expect(parser.render(input)).toContain('<p>after</p>');
  });

  it('reads bare display environments as displayed math', () => {
    const input = 'Result:\n\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}\ndone';
    const dollars = 'Result:\n\n$$\n\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}\n$$\ndone';
    expect(math(input)).toEqual([[true, '\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}']]);
    expect(math(input)).toEqual(math(dollars));
  });

  it('leaves code fences, code spans, escaped delimiters, and dollar math alone', () => {
    const input = [
      'Use `\\(x\\)` literally.',
      '```latex',
      '\\[ a \\]',
      '```',
      'Escaped \\\\[ bracket and $$ \\(kept\\) $$ math.',
    ].join('\n');
    expect(math(input)).toEqual([[true, '\\(kept\\)']]);
    const all = parser.parse(input, {});
    expect(all.find((token) => token.type === 'fence')?.content).toBe('\\[ a \\]\n');
    const inline = all.flatMap((token) => token.children ?? []);
    expect(inline.find((token) => token.type === 'code_inline')?.content).toBe('\\(x\\)');
  });

  it('finds no math in plain text, and dollar math as before', () => {
    expect(math('plain text')).toEqual([]);
    expect(math('plain $x$ text')).toEqual([[false, 'x']]);
  });

  it('reads single-dollar inline math and leaves prices alone', () => {
    // The original's cases for Pandoc's rule. Two differ in this dialect, mdit-py-plugins'
    // (D65): a digit may follow a closing dollar, and inline math may span lines.
    const cases: [string, [boolean, string][]][] = [
      ['with $h = gf^{-1}$ here', [[false, 'h = gf^{-1}']]],
      ['both $x$ and $y$.', [[false, 'x'], [false, 'y']]],
      ['costs $5 and $10 today', []],
      ['between $5/$6 per unit', [[false, '5/']]], // Pandoc: no math
      ['a lone $ sign', []],
      ['open $ x$ with a space', []],
      ['closing $x $ with a space', []],
      ['escaped \\$x\\$ stays', []],
      ['code `$x$` stays', []],
      ['across $x\ny$ lines', [[false, 'x\ny']]], // Pandoc: no math
      ['already $$x$$ double', [[true, 'x']]],
      ['block\n$$\na $b$ c\n$$\nafter $d$', [[true, 'a $b$ c'], [false, 'd']]],
      ['escaped \\{ $\\{x\\}$ set', [[false, '\\{x\\}']]],
      ['```\n$x$\n```', []],
    ];
    for (const [input, expected] of cases) expect(math(input), input).toEqual(expected);
  });

  it('reads the portable single-dollar form and its exceptions', () => {
    const input = 'Inline \\(h = gf^{-1}\\) and display:\n\\[\nfG - gF = q\n\\]\nprice $5 and $x$ kept';
    expect(math(input)).toEqual([
      [false, 'h = gf^{-1}'],
      [true, 'fG - gF = q'],
      [false, 'x'],
    ]);
    expect(math('\\(x\\)5 units')).toEqual([[false, 'x']]);
    expect(math('\\(a \\$ b\\)')).toEqual([[false, 'a \\$ b']]);
    expect(math('\\( \\)')).toEqual([]);
  });

  // From test/unit/markdown-math.test.mjs: saved Markdown, before and after conversion.
  const BRACKETS = 'Key: \\(h = gf^{-1}\\).\n\\[\nfG - gF = q\n\\]\n';
  const DOLLARS = 'Key: $h = gf^{-1}$.\n\n$$\nfG - gF = q\n$$\n';

  it('reads the bracket form as its dollar form', () => {
    expect(spans(BRACKETS).map(([type, content]) => [type, content])).toEqual(
      spans(DOLLARS).map(([type, content]) => [type, content]),
    );
  });

  it('reads the dollar form as it was', () => {
    const dollarsOnly = markdownit('default', { html: false });
    dollarMath(dollarsOnly, MATH_RULES);
    expect(parser.parse(DOLLARS, {})).toEqual(dollarsOnly.parse(DOLLARS, {}));
  });
});

describe('with dollar math', () => {
  const dollarsOnly = markdownit('default', { html: false, linkify: false, typographer: false });
  dollarMath(dollarsOnly, MATH_RULES);

  it('leaves every dollar case as the dollar rules read it', () => {
    for (const { markdown } of dollarFixtures.cases) {
      expect(parser.parse(markdown, {}), markdown).toEqual(dollarsOnly.parse(markdown, {}));
    }
  });

  it('ends a block within the same number of lines as a $$ block', () => {
    const inside = (count: number) => 'x\n'.repeat(count);
    for (const count of [MAX_BLOCK_LINES - 1, MAX_BLOCK_LINES]) {
      const backslash = tokens(`\\[\n${inside(count)}\\]`).map((token) => token.type);
      const dollars = tokens(`$$\n${inside(count)}$$`).map((token) => token.type);
      expect(backslash, `${count} lines`).toEqual(dollars);
    }
    // Within the limit a block; beyond it, displayed math within a paragraph, as for $$.
    expect(tokens(`\\[\n${inside(MAX_BLOCK_LINES - 1)}\\]`)[0]?.type).toBe('math_block');
    expect(tokens(`\\[\n${inside(MAX_BLOCK_LINES)}\\]`)[0]?.type).toBe('math_inline_double');
  });

  it('pairs no delimiter with the other kind', () => {
    expect(math('$a \\) b$ and \\(c $ d\\)')).toEqual([
      [false, 'a \\) b'],
      [false, 'c $ d'],
    ]);
    expect(math('\\[ a $$ b \\]')).toEqual([[true, 'a $$ b']]);
    expect(math('$$ \\[ $$')).toEqual([[true, '\\[']]);
  });
});
