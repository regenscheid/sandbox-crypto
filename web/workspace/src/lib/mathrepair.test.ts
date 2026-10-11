// SPDX-License-Identifier: MIT
// The deterministic repairs, the Unicode approximation and the recovery ladder. Adapted
// from cscience-crypto's test/unit/web-math.test.mjs, contributed under the MIT License by
// its author; the tests after the original's are this project's own.
import { describe, expect, it } from 'vitest';
import { ApiError, type MathRepairAnswer, type MathRepairRequest } from './api';
import {
  definesMacros,
  describeError,
  MAX_MODEL_TEX,
  ModelRepairs,
  recoveredHtml,
  recoverMath,
  repairKey,
  repairTex,
  safeRepair,
  texToUnicode,
  type MathRenderer,
} from './mathrepair';

interface Call {
  tex: string;
  options: Record<string, unknown>;
}

function fakeKatex(rejects: (tex: string) => boolean): MathRenderer & { calls: Call[] } {
  const calls: Call[] = [];
  return {
    calls,
    renderToString(tex, options) {
      calls.push({ tex, options });
      if (rejects(tex)) throw new Error(`KaTeX parse error: rejected ${tex}`);
      return (
        '<span class="katex"><span class="katex-mathml"><math><semantics>' +
        `<annotation encoding="application/x-tex">${tex}</annotation></semantics></math></span>` +
        '<span class="katex-html">rendered</span></span>'
      );
    },
  };
}

/** A transport whose answers the test gives, one request at a time. */
function heldTransport() {
  const requests: MathRepairRequest[] = [];
  const answers: ((answer: MathRepairAnswer) => void)[] = [];
  const transport = (request: MathRepairRequest) => {
    requests.push(request);
    return new Promise<MathRepairAnswer>((resolve) => answers.push(resolve));
  };
  return { transport, requests, answers };
}

describe('deterministic LaTeX repairs', () => {
  it('maps unsupported macros and strips labels', () => {
    const [candidate] = repairTex(
      '\\bm{x} \\label{eq:1} + \\mbox{text}',
      'Undefined control sequence: \\bm',
    );
    expect(candidate).toBe('\\boldsymbol{x}  + \\text{text}');
  });

  it('balances braces and delimiters for truncated input', () => {
    expect(repairTex('\\frac{a}{b', "Expected '}', got 'EOF'")).toContain('\\frac{a}{b}');
    const delimiters = repairTex('\\left( x', 'Missing \\right');
    expect(delimiters.some((candidate) => candidate.includes('\\right.'))).toBe(true);
  });

  it('converts alignment environments for inline math', () => {
    const candidates = repairTex(
      '\\begin{align} a &= b \\end{align}',
      '{align} can be used only in display mode',
      false,
    );
    expect(candidates[0]).toBe('\\begin{aligned} a &= b \\end{aligned}');
  });

  it('wraps bare alignment markers and escapes bare characters', () => {
    const candidates = repairTex('a &= b \\\\ c &= d', "Expected 'EOF', got '&'", true);
    expect(candidates.some((candidate) => candidate.startsWith('\\begin{aligned}'))).toBe(true);
    expect(repairTex('100% of #1', "Unexpected character: '#'")).toContain('100\\% of \\#1');
  });

  it('nests double scripts and names unknown control sequences', () => {
    expect(repairTex('x_i_j', 'Double subscript')).toContain('x_{i_j}');
    expect(repairTex('\\Tr(A)', 'Undefined control sequence: \\Tr')).toContain(
      '\\operatorname{Tr}(A)',
    );
  });

  it('returns nothing for input it cannot change', () => {
    expect(repairTex('x^2', 'anything')).toEqual([]);
  });

  it("repairs what KaTeX's own errors point to", () => {
    // The messages of the KaTeX this workspace uses (0.18.9).
    expect(
      repairTex(
        '\\frac{a}{b',
        "Unexpected end of input in a macro argument, expected '}' at end of input: \\frac{a}{b",
      ),
    ).toContain('\\frac{a}{b}');
    expect(repairTex('\\left( x', "Expected '\\right', got 'EOF' at end of input: \\left( x")[0]).toBe(
      '\\left( x \\right.',
    );
    expect(repairTex('x^', "Expected group after '^' at position 2: x^̲")[0]).toBe('x');
  });
});

describe('commands the repairs never make', () => {
  it('leave an escaped backslash alone: \\\\R is a line break and an R', () => {
    const candidates = repairTex('a \\\\R b \\\\label{x} c \\R', 'Undefined control sequence: \\foo');
    expect(candidates[0]).toBe('a \\\\R b \\\\label{x} c \\mathbb{R}');
  });

  it('never join pieces into a command by removing what lies between them', () => {
    for (const [tex, gated] of [
      ['\\hr\\label{a}ef{javascript:alert(1)}{x}\\foo', 'href'],
      ['\\inc\\vspace{2pt}ludegraphics{https://evil.example/x.png}\\foo', 'includegraphics'],
      ['\\ur\\footnote{z}l{javascript:alert(1)}\\foo', 'url'],
      ['\\htm\\label{}lClass{math-mark}{x}\\foo', 'htmlClass'],
      ['\\d\\nonumber ef\\a{x}\\a\\foo', 'def'],
    ] as const) {
      for (const candidate of repairTex(tex, 'Undefined control sequence: \\foo')) {
        expect(candidate, tex).not.toContain(`\\${gated}`);
      }
    }
    expect(repairTex('\\d\\label{x}ef', "Expected 'EOF'")).toContain('\\d ef');
  });

  it('refuse a repair that brings in a gated command or a definition', () => {
    expect(safeRepair('x^', 'x^{2}')).toBe(true);
    expect(safeRepair('\\href{a}{b}', '\\href{a}{b}')).toBe(true);
    expect(safeRepair('x^', '\\href{javascript:alert(1)}{x}')).toBe(false);
    expect(safeRepair('x^', '\\def\\a{x}\\a')).toBe(false);
    expect(definesMacros('\\gdef\\a{x}')).toBe('gdef');
    expect(definesMacros('a \\\\def b')).toBeNull();
    expect(definesMacros('\\newcommand*{\\R}{\\mathbb{R}}')).toBe('newcommand');
  });

  it('refuse a model answer that brings in a gated command', async () => {
    const katex = fakeKatex((tex) => tex.includes('broken'));
    const repairs = new ModelRepairs(async () => ({
      enabled: true,
      tex: '\\href{javascript:alert(1)}{x}',
      model: 'rev',
    }));
    const options = { displayMode: false, error: 'e', settings: {}, repairs };
    recoverMath(katex, '\\broken', options);
    await repairs.settled();
    expect(repairs.get(repairKey('\\broken', false))).toMatchObject({ tex: null });
    repairs.set(repairKey('\\broken2', false), { tex: '\\url{javascript:x}', model: 'rev' });
    expect(recoverMath(katex, '\\broken2', options).status).toBe('fallback');
  });

  it('always render with trust off', () => {
    const katex = fakeKatex((tex) => tex.includes('\\bm'));
    recoverMath(katex, '\\bm{x}', { displayMode: false, error: 'e', settings: { trust: true } });
    expect(katex.calls.every((call) => call.options.trust === false)).toBe(true);
  });
});

describe('Unicode transliteration', () => {
  it('handles Greek letters, operators, scripts, and alphabets', () => {
    expect(texToUnicode('\\alpha_i^2 + \\beta \\le \\infty')).toBe('αᵢ² + β ≤ ∞');
    expect(texToUnicode('\\mathbb{F}_{2^8}')).toBe('𝔽_(2⁸)');
    expect(texToUnicode('\\mathbf{A}x')).toBe('𝐀x');
  });

  it('flattens fractions, roots, binomials, and text', () => {
    expect(texToUnicode('\\frac{q^2}{2^{n+1}}')).toBe('q²/2ⁿ⁺¹');
    expect(texToUnicode('\\sqrt[3]{x+1} \\text{ over } \\gcd(a,n)')).toBe('³√(x+1) over gcd(a,n)');
    expect(texToUnicode('\\binom{n}{k}')).toBe('C(n, k)');
  });

  it('flattens matrices and cases', () => {
    expect(texToUnicode('\\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}')).toBe('(1 2; 3 4)');
    expect(
      texToUnicode('\\begin{cases} 1 & x > 0 \\\\ 0 & \\text{otherwise} \\end{cases}'),
    ).toBe('{ 1 if x > 0; 0 if otherwise }');
  });

  it('keeps unknown macros readable', () => {
    expect(texToUnicode('\\Tr(A) \\quad \\hat{x} \\to y')).toBe('Tr(A) x̂ → y');
  });

  it('reads names that objects inherit as names', () => {
    // The original looked names up with `in`, which finds Object.prototype's members.
    expect(texToUnicode('\\constructor + \\toString + \\hasOwnProperty{x}')).toBe(
      'constructor + toString + hasOwnPropertyx',
    );
    expect(texToUnicode('\\sqrt[{3}]{x}')).toBe('³√x');
  });

  it('stays quick, and gives the source back when nesting is too deep', () => {
    const started = performance.now();
    texToUnicode('\\begin{a}'.repeat(500) + '\\begin{b}x\\end{b}'.repeat(50));
    texToUnicode('\u2003'.repeat(5_000) + 'x;' + ' '.repeat(5_000));
    expect(performance.now() - started).toBeLessThan(1_000);
    const deep = '{'.repeat(100_000);
    expect(texToUnicode(deep)).toBe(deep);
  });
});

describe('recovery ladder', () => {
  it('renders the first deterministic repair that KaTeX accepts', () => {
    const katex = fakeKatex((tex) => tex.includes('\\bm'));
    const result = recoverMath(katex, '\\bm{x}', {
      displayMode: false,
      error: new Error('KaTeX parse error: Undefined control sequence: \\bm'),
      settings: { output: 'htmlAndMathml' },
    });
    expect(result).toMatchObject({ status: 'repaired', how: 'heuristic', tex: '\\boldsymbol{x}' });
    const html = recoveredHtml(result, 'span', false, '$\\bm{x}$');
    expect(html).toMatch(/^<span class="math math-inline math-repaired" data-repair="heuristic">/);
    expect(html).toContain('<code class="math-source">$\\bm{x}$</code>');
    expect(katex.calls[0]!.options.output).toBe('htmlAndMathml');
    expect(katex.calls[0]!.options.throwOnError).toBe(true);
  });

  it('falls back to Unicode while preserving the source and requesting a repair once', async () => {
    const katex = fakeKatex(() => true);
    const requests: MathRepairRequest[] = [];
    const repairs = new ModelRepairs(async (request) => {
      requests.push(request);
      return { enabled: true, tex: '\\alpha + 1', model: 'reviewer' };
    });
    const first = recoverMath(katex, '\\alpha + \\undefinedmacro', {
      displayMode: true,
      error: new Error('KaTeX parse error: Undefined control sequence: \\undefinedmacro'),
      settings: {},
      repairs,
    });
    expect(first.status).toBe('fallback');
    const html = recoveredHtml(first, 'div', true, '$$\\alpha + \\undefinedmacro$$');
    expect(html).toMatch(/^<div class="math math-display math-approximated"/);
    expect(html).toContain('<code class="math-source">$$\\alpha + \\undefinedmacro$$</code>');
    expect(html).toContain('<span class="math-unicode">α + undefinedmacro</span>');
    expect(html).toContain('data-state="pending"');
    recoverMath(katex, '\\alpha + \\undefinedmacro', {
      displayMode: true,
      error: 'x',
      settings: {},
      repairs,
    });
    await repairs.settled();
    expect(requests).toEqual([
      {
        tex: '\\alpha + \\undefinedmacro',
        display: true,
        error: 'Undefined control sequence: \\undefinedmacro',
      },
    ]);
    // The repair still fails with this KaTeX, so it is not kept as usable.
    expect(repairs.get(repairKey('\\alpha + \\undefinedmacro', true))?.tex).toBeNull();
  });

  it('renders a cached reviewer-model repair', () => {
    const katex = fakeKatex((tex) => tex.includes('\\broken'));
    const repairs = new ModelRepairs(null);
    repairs.set(repairKey('\\broken', false), { tex: 'x^2', model: 'test' });
    const result = recoverMath(katex, '\\broken', {
      displayMode: false,
      error: 'unsupported',
      settings: {},
      repairs,
    });
    expect(result).toMatchObject({ status: 'repaired', how: 'model', tex: 'x^2', model: 'test' });
    const html = recoveredHtml(result, 'span', false, '$\\broken$');
    expect(html).toContain('data-repair="model"');
    expect(html).toContain('Repaired by the reviewer model (test).');
  });

  it('stops asking once the service reports it is disabled', async () => {
    const katex = fakeKatex(() => true);
    let calls = 0;
    const repairs = new ModelRepairs(async () => {
      calls += 1;
      return { enabled: false, reason: 'no reviewer model configured' };
    });
    const options = { displayMode: false, error: 'e', settings: {}, repairs };
    recoverMath(katex, '\\one', options);
    await repairs.settled();
    recoverMath(katex, '\\two', options);
    await repairs.settled();
    expect(calls).toBe(1);
    const result = recoverMath(katex, '\\three', options);
    const html = recoveredHtml(result, 'span', false, '$\\three$');
    expect(html).toContain('data-state="unrepairable"');
    expect(html).toContain('No repair: no reviewer model configured.');
  });

  it('describes errors compactly', () => {
    expect(
      describeError(new Error("KaTeX parse error: Expected group after '^' at position 3")),
    ).toBe("Expected group after '^' at position 3");
    expect(describeError(undefined)).toBe('unknown error');
    expect(describeError('a'.repeat(299) + '😀')).toBe('a'.repeat(299)); // no half pair
  });
});

describe('model repairs', () => {
  it('renders the repair once it arrives, and tells the listeners', async () => {
    const katex = fakeKatex((tex) => tex.includes('broken'));
    const held = heldTransport();
    const repairs = new ModelRepairs(held.transport);
    const heard: string[] = [];
    repairs.subscribe((keys) => heard.push(...keys));
    const options = {
      displayMode: false,
      error: 'Undefined control sequence: \\broken',
      settings: {},
      repairs,
    };
    expect(recoverMath(katex, '\\broken', options)).toMatchObject({ status: 'fallback', state: 'pending' });
    await Promise.resolve();
    held.answers[0]!({ enabled: true, tex: ' x^2 ', model: 'reviewer' });
    await repairs.settled();
    expect(heard).toEqual([repairKey('\\broken', false)]);
    expect(repairs.get(repairKey('\\broken', false))).toEqual({ tex: 'x^2', model: 'reviewer' });
    expect(recoverMath(katex, '\\broken', options)).toMatchObject({
      status: 'repaired',
      how: 'model',
      tex: 'x^2',
    });
  });

  it('asks for one repair until the server says it has a model, then two at a time', async () => {
    const held = heldTransport();
    const repairs = new ModelRepairs(held.transport);
    const check = () => undefined;
    for (const tex of ['a', 'b', 'c', 'd', 'e']) {
      repairs.request(tex, { tex, display: false, error: 'e' }, check);
    }
    await Promise.resolve();
    expect(held.requests.map((request) => request.tex)).toEqual(['a']);
    held.answers[0]!({ enabled: true, tex: null, reason: 'no usable repair' });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(held.requests.map((request) => request.tex)).toEqual(['a', 'b', 'c']);
    held.answers[1]!({ enabled: true, tex: 'b2' });
    held.answers[2]!({ enabled: true, tex: 'c2' });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(held.requests.map((request) => request.tex)).toEqual(['a', 'b', 'c', 'd', 'e']);
    held.answers[3]!({ enabled: true, tex: 'd2' });
    held.answers[4]!({ enabled: true, tex: 'e2' });
    await repairs.settled();
    expect(repairs.get('a')).toEqual({ tex: null, reason: 'no usable repair' });
    expect(repairs.get('e')).toEqual({ tex: 'e2', model: null });
  });

  it('with no model on the server, sends one request, drops the rest, and tells once', async () => {
    // The review's case: 300 failing formulas, then the first answer turns repairs off.
    let calls = 0;
    const repairs = new ModelRepairs(async () => {
      calls += 1;
      return { enabled: false, reason: 'no reviewer model configured' };
    });
    const told: number[] = [];
    repairs.subscribe((keys) => told.push(keys.size));
    for (let index = 0; index < 300; index += 1) {
      repairs.request(`k${index}`, { tex: `x${index}`, display: false, error: 'e' }, () => undefined);
    }
    expect(repairs.isPending('k299')).toBe(true);
    await repairs.settled();
    expect(calls).toBe(1);
    expect(told).toEqual([300]);
    expect(repairs.isPending('k299')).toBe(false);
    expect(repairs.unavailable).toBe('no reviewer model configured');
  });

  it('tells its listeners once a frame, however many answers arrive in it', async () => {
    const frames: (() => void)[] = [];
    const repairs = new ModelRepairs(async (request) => ({ enabled: true, tex: `${request.tex}2` }), {
      maxActive: 8,
      nextFrame: (callback) => frames.push(callback),
    });
    const told: number[] = [];
    repairs.subscribe((keys) => told.push(keys.size));
    repairs.request('first', { tex: 'first', display: false, error: 'e' }, () => undefined);
    for (let turn = 0; turn < 10 && frames.length === 0; turn += 1) await Promise.resolve();
    frames.splice(0).forEach((callback) => callback());
    for (const tex of ['a', 'b', 'c', 'd', 'e']) {
      repairs.request(tex, { tex, display: false, error: 'e' }, () => undefined);
    }
    for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();
    expect(frames).toHaveLength(1);
    frames.splice(0).forEach((callback) => callback());
    expect(told).toEqual([1, 5]);
  });

  it('asks again later when the server is busy, and keeps no "busy"', async () => {
    let calls = 0;
    const waits: number[] = [];
    const repairs = new ModelRepairs(
      async () => {
        calls += 1;
        return calls < 3
          ? { enabled: true, tex: null, model: 'rev', reason: 'busy' }
          : { enabled: true, tex: 'x^{2}', model: 'rev' };
      },
      {
        later: (callback, ms) => {
          waits.push(ms);
          setTimeout(callback, 0);
          return () => undefined;
        },
      },
    );
    repairs.request('k', { tex: 'x^^2', display: false, error: 'e' }, () => undefined);
    await repairs.settled();
    expect(calls).toBe(3);
    expect(waits).toEqual([2_000, 4_000]);
    expect(repairs.get('k')).toEqual({ tex: 'x^{2}', model: 'rev' });
  });

  it('gives up for the page after the server stays busy', async () => {
    const repairs = new ModelRepairs(async () => ({ enabled: true, tex: null, reason: 'busy' }), {
      busyRetries: [1, 1],
      later: (callback) => {
        setTimeout(callback, 0);
        return () => undefined;
      },
    });
    repairs.request('k', { tex: 'x^^2', display: false, error: 'e' }, () => undefined);
    await repairs.settled();
    expect(repairs.get('k')).toEqual({ tex: null, reason: 'the server stayed busy' });
  });

  it("stops asking when the session ends or reaches the server's limit", async () => {
    for (const status of [401, 429]) {
      let calls = 0;
      const repairs = new ModelRepairs(async () => {
        calls += 1;
        throw new ApiError(status, 'policy_denied', `refused with ${status}`);
      });
      repairs.request('a', { tex: 'a', display: false, error: 'e' }, () => undefined);
      await repairs.settled();
      repairs.request('b', { tex: 'b', display: false, error: 'e' }, () => undefined);
      await repairs.settled();
      expect(calls).toBe(1);
      expect(repairs.unavailable).toBe(`refused with ${status}`);
    }
  });

  it('keeps other failures to their own expression', async () => {
    let calls = 0;
    const repairs = new ModelRepairs(async () => {
      calls += 1;
      throw new ApiError(500, 'internal', 'internal workspace error');
    });
    repairs.request('a', { tex: 'a', display: false, error: 'e' }, () => undefined);
    repairs.request('b', { tex: 'b', display: false, error: 'e' }, () => undefined);
    await repairs.settled();
    expect(calls).toBe(2);
    expect(repairs.get('a')).toEqual({ tex: null, reason: 'internal workspace error' });
    expect(repairs.unavailable).toBeNull();
  });

  it('does not ask for expressions longer than the server takes', async () => {
    let calls = 0;
    const repairs = new ModelRepairs(async () => {
      calls += 1;
      return { enabled: true, tex: 'x' };
    });
    const tex = 'x'.repeat(MAX_MODEL_TEX + 1);
    repairs.request(tex, { tex, display: false, error: 'e' }, () => undefined);
    await repairs.settled();
    expect(calls).toBe(0);
    expect(repairs.get(tex)?.tex).toBeNull();
  });
});

describe('the marks', () => {
  it('escape everything that comes from the text, the model or the server', () => {
    const repaired = recoveredHtml(
      {
        status: 'repaired',
        how: 'model',
        html: '<span class="katex">ok</span>',
        tex: 'x',
        model: '<b>"model"</b>',
        error: 'Undefined control sequence: \\x<script>',
      },
      'span',
      false,
      '$\\x<script>alert(1)</script>$',
    );
    expect(repaired).not.toMatch(/<(script|b)\b/);
    expect(repaired).toContain('&lt;b&gt;&quot;model&quot;&lt;/b&gt;');
    const approximated = recoveredHtml(
      {
        status: 'fallback',
        unicode: '<img src=x onerror=alert(1)>',
        error: '"><img>',
        state: 'unrepairable',
        reason: '<i>no</i>',
      },
      'div',
      true,
      '\\[<svg onload=alert(1)>\\]',
    );
    expect(approximated).not.toMatch(/<(img|svg|i)\b/);
    expect(approximated).toContain('title="KaTeX could not render this: &quot;&gt;&lt;img&gt;.');
  });

  it('say how each expression was shown, and keep it as written behind the mark', () => {
    const html = recoveredHtml(
      { status: 'fallback', unicode: 'x', error: 'e', state: 'pending', reason: null },
      'span',
      false,
      '\\(\\x\\)',
    );
    expect(html).toContain('<button type="button" class="math-mark" aria-expanded="false"');
    expect(html).toContain('>approximated</button>');
    expect(html).toContain('<span class="math-details" hidden>');
    expect(html).toContain('<code class="math-source">\\(\\x\\)</code>');
    expect(html).toContain('The reviewer model has been asked for a repair.');
  });

  it('show bidirectional controls in the formula as written as code points', () => {
    const html = recoveredHtml(
      { status: 'fallback', unicode: 'a\u202eb', error: 'e', state: 'unrepairable', reason: null },
      'span',
      false,
      '$x \\text{\u202eevil}$',
    );
    expect(html).not.toContain('\u202e');
    expect(html).toContain('<code class="math-source">$x \\text{⟦U+202E⟧evil}$</code>');
    expect(html).toContain('<span class="math-unicode">a⟦U+202E⟧b</span>');
  });

  it('lay out displayed math within a paragraph as displayed', () => {
    const fallback = { status: 'fallback', unicode: 'x', error: 'e', state: 'pending', reason: null } as const;
    expect(recoveredHtml(fallback, 'span', true, '$$x$$')).toMatch(
      /^<span class="math math-display math-approximated"/,
    );
    expect(recoveredHtml(fallback, 'div', true, '$$x$$')).toMatch(
      /^<div class="math math-display math-approximated"/,
    );
  });
});
