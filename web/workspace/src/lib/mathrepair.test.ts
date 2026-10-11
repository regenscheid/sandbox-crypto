// SPDX-License-Identifier: MIT
// The deterministic repairs, the Unicode approximation and the recovery ladder. Adapted
// from cscience-crypto's test/unit/web-math.test.mjs, contributed under the MIT License by
// its author; the tests after the original's are this project's own.
import { describe, expect, it } from 'vitest';
import { ApiError, type MathRepairAnswer, type MathRepairRequest } from './api';
import {
  definesMacros,
  describeError,
  Kept,
  MAX_MODEL_TEX,
  ModelRepairs,
  OutOfTime,
  recoveredHtml,
  recoverMath,
  repairKey,
  repairTex,
  safeRepair,
  texToUnicode,
  type MathRenderer,
  type Recovery,
  type ShownRecovery,
} from './mathrepair';

interface Call {
  tex: string;
  options: Record<string, unknown>;
}

/** A recovery the test expects to be shown. */
function shown(recovery: Recovery): ShownRecovery {
  if (recovery.status === 'unfinished') throw new Error('the recovery is unfinished');
  return recovery;
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
    expect(repairTex('x^', "Expected group after '^' at position 2: x^\u0332")[0]).toBe('x');
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
    // Escaping a comment's % brings in what it commented out.
    expect(safeRepair('x % \\def\\a{x}', 'x \\% \\def\\a{x}')).toBe(false);
  });

  it('read commands as KaTeX does: none in a \\verb body or a comment', () => {
    expect(definesMacros('\\verb|\\def\\a{x}| shows TeX')).toBeNull();
    expect(definesMacros('\\verb*@\\gdef\\a{x}@ and \\verb@\\let\\a@')).toBeNull();
    expect(definesMacros('x % \\def is a comment')).toBeNull();
    expect(definesMacros('\\\\% \\def: a line break, then a comment')).toBeNull();
    // Only a line feed ends a comment.
    expect(definesMacros('x % note\r\\def\\a{x}')).toBeNull();
    // What KaTeX does read as commands.
    expect(definesMacros('x % note\n\\def\\a{x}')).toBe('def');
    expect(definesMacros('x \\% \\def\\a{x}')).toBe('def');
    expect(definesMacros('\\verb|x|\\def\\a{x}')).toBe('def');
    expect(definesMacros('\\verb|\\def\\a{x}')).toBe('def'); // no closing delimiter
    expect(definesMacros('\\verb|\\def\n|')).toBe('def'); // nor one on its line
    expect(definesMacros('\\verbatim \\def')).toBe('def');
    // KaTeX reads % in a link as a character, so in a formula with one, nothing is a comment.
    expect(definesMacros('\\url{%}\\def\\a{x}\\a')).toBe('def');
    expect(definesMacros('\\href{%}{x}\\gdef\\a{x}')).toBe('gdef');
    expect(definesMacros('\\includegraphics{%}\\let\\a=x')).toBe('let');
    // A \verb whose delimiter ends the line, as KaTeX's lexer allows.
    expect(definesMacros('\\verb\n\\def\\a{x}\n\\a')).toBeNull();
    expect(definesMacros('\\verb\n\\def\\a{x}\r\n')).toBe('def');
  });

  it('read % with a combining mark as KaTeX does: a token, not a comment', () => {
    // The third review's bypass: KaTeX reads `%\u0301` as one token, which these macros skip
    // or peek at unparsed, and then defines.
    const acute = '\u0301';
    expect(definesMacros(`\\@secondoftwo{%${acute}}{}\\def\\a{x}\\a`)).toBe('def');
    expect(definesMacros(`\\TextOrMath{%${acute}}{}\\gdef\\a{x}\\a`)).toBe('gdef');
    expect(definesMacros(`\\@ifnextchar%${acute}{}{}\\newcommand{\\a}{x}\\a`)).toBe('newcommand');
    expect(definesMacros(`x %${acute} \\def\\a{x}\\a`)).toBe('def');
    // A combining mark as a \verb's delimiter, and a letter after \verb*, as KaTeX reads them.
    expect(definesMacros(`\\verb${acute} \\def ${acute} x`)).toBeNull();
    expect(definesMacros('\\verb*a\\def\\b{x}a\\b')).toBeNull();
    // Where KaTeX's lexer stops with an error, reading on finds more, not less.
    expect(definesMacros('x \u0001 \\def\\a{x}')).toBe('def');
    expect(definesMacros('\uD835 \\def\\a{x}')).toBe('def');
  });

  it("find every definer KaTeX's own lexer finds (the review's fuzz)", () => {
    // KaTeX 0.18.9's token pattern and comment rule (src/Lexer.ts, MIT License), unchanged,
    // as the reference: a stream of its tokens that holds a definer, where definesMacros
    // finds none, would be a way past the refusal.
    const space = '[ \r\n\t]';
    const marks = '[\u0300-\u036F]';
    const pattern = new RegExp(
      `(${space}+)|\\\\(\n|[ \r\t]+\n?)[ \r\t]*|` +
        `([!-\\[\\]-\u2027\u202A-\uD7FF\uF900-\uFFFF]${marks}*` +
        `|[\uD800-\uDBFF][\uDC00-\uDFFF]${marks}*` +
        '|\\\\verb\\*([^]).*?\\4|\\\\verb([^*a-zA-Z]).*?\\5' +
        `|(\\\\[a-zA-Z@]+)${space}*|\\\\[^\uD800-\uDFFF])`,
      'g',
    );
    const tokens = (input: string): string[] | null => {
      pattern.lastIndex = 0;
      const out: string[] = [];
      for (;;) {
        const position = pattern.lastIndex;
        if (position === input.length) return out;
        const match = pattern.exec(input);
        if (match === null || match.index !== position) return null;
        const text = match[6] || match[3] || (match[2] ? '\\ ' : ' ');
        if (text === '%') {
          const newline = input.indexOf('\n', pattern.lastIndex);
          pattern.lastIndex = newline === -1 ? input.length : newline + 1;
          continue;
        }
        out.push(text);
      }
    };
    const definers = new Set(['\\def', '\\gdef', '\\edef', '\\xdef', '\\let', '\\futurelet', '\\global', '\\long', '\\newcommand', '\\renewcommand', '\\providecommand']);
    const alphabet = ['\\', '\\', '%', 'v', 'e', 'r', 'b', 'verb', '\\verb', '\\def', 'def', '*', '|', '\n', '\r', '\u0301', '{', '}', ' ', '@', 'a', ' ', '\t', '\\%', '\\\\'];
    let seed = 12345;
    const random = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    const misses: string[] = [];
    let checked = 0;
    for (let n = 0; n < 300_000; n += 1) {
      const length = 1 + Math.floor(random() * 12);
      let tex = '';
      for (let i = 0; i < length; i += 1) tex += alphabet[Math.floor(random() * alphabet.length)];
      const stream = tokens(tex);
      if (stream === null) continue;
      checked += 1;
      if (stream.some((token) => definers.has(token)) && definesMacros(tex) === null) misses.push(tex);
    }
    expect(checked).toBeGreaterThan(200_000);
    expect(misses.slice(0, 5)).toEqual([]);
  });

  it('read \\verb openers that never close in linear time', () => {
    // Each with a delimiter of its own: looking for each one's to the end of the line
    // took time quadratic in their number.
    const openers = Array.from({ length: 20_000 }, (_, i) => `\\verb${String.fromCharCode(0x4e00 + i)}`);
    const started = performance.now();
    expect(definesMacros(`${openers.join('')}\\def`)).toBe('def');
    expect(performance.now() - started).toBeLessThan(200);
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
    expect(texToUnicode('\\Tr(A) \\quad \\hat{x} \\to y')).toBe('Tr(A) x\u0302 → y');
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
    const html = recoveredHtml(shown(result), 'span', false, '$\\bm{x}$');
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
    const html = recoveredHtml(shown(first), 'div', true, '$$\\alpha + \\undefinedmacro$$');
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
    const html = recoveredHtml(shown(result), 'span', false, '$\\broken$');
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
    const html = recoveredHtml(shown(result), 'span', false, '$\\three$');
    expect(html).toContain('data-state="unrepairable"');
    expect(html).toContain('No repair: no reviewer model configured.');
  });

  it('leaves an expression unfinished when the rules had no time, and asks no model', async () => {
    let asked = 0;
    const repairs = new ModelRepairs(async () => {
      asked += 1;
      return { enabled: true, tex: 'x', model: 'rev' };
    });
    const outOfTime: MathRenderer = {
      renderToString(tex) {
        if (tex === 'x') return '<span class="katex">x</span>';
        throw new OutOfTime();
      },
    };
    const options = { displayMode: false, error: 'Undefined control sequence: \\Tr', settings: {}, repairs };
    expect(recoverMath(outOfTime, '\\Tr x', options)).toEqual({ status: 'unfinished' });
    await repairs.settled();
    expect(asked).toBe(0);
    expect(repairs.isPending(repairKey('\\Tr x', false))).toBe(false);
    // A model's repair the page already has still shows.
    repairs.set(repairKey('\\Tr x', false), { tex: 'x', model: 'rev' });
    expect(recoverMath(outOfTime, '\\Tr x', options)).toMatchObject({ status: 'repaired', how: 'model' });
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

  it('drops a "busy" that arrives after another answer turned repairs off', async () => {
    // The review's race: two requests in flight, one answered 429, then the other `busy`.
    const answers: (() => Promise<MathRepairAnswer>)[] = [
      async () => ({ enabled: true, tex: 'x', model: 'rev' }), // the server has a model: two at once
      async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
        throw new ApiError(429, 'rate_limited', 'this session has asked for 60 repairs');
      },
      async () => {
        await new Promise((resolve) => setTimeout(resolve, 30));
        return { enabled: true, tex: null, model: 'rev', reason: 'busy' };
      },
    ];
    let asked = 0;
    const waits: number[] = [];
    const repairs = new ModelRepairs(() => answers[asked++]!(), {
      later: (callback, ms) => {
        waits.push(ms);
        setTimeout(callback, 0);
        return () => undefined;
      },
    });
    const heard: string[] = [];
    repairs.subscribe((keys) => heard.push(...keys));
    for (const tex of ['a', 'b', 'c']) {
      repairs.request(tex, { tex, display: false, error: 'e' }, () => undefined);
    }
    const settled = await Promise.race([
      repairs.settled().then(() => true),
      new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 1_000)),
    ]);
    expect(['a', 'b', 'c'].map((key) => repairs.isPending(key))).toEqual([false, false, false]);
    expect(settled).toBe(true);
    expect(asked).toBe(3);
    expect(waits).toEqual([]);
    expect(heard.sort()).toEqual(['a', 'b', 'c']);
    expect(repairs.unavailable).toBe('this session has asked for 60 repairs');
  });

  it('sends nothing for a try that comes due after repairs went off', async () => {
    const due: (() => void)[] = [];
    let asked = 0;
    const repairs = new ModelRepairs(
      async (request) => {
        asked += 1;
        if (request.tex === 'a') return { enabled: true, tex: 'a2', model: 'rev' };
        if (request.tex === 'b') return { enabled: true, tex: null, model: 'rev', reason: 'busy' };
        throw new ApiError(429, 'rate_limited', 'limit reached');
      },
      {
        // A wait that cannot be cancelled: its callback still comes due.
        later: (callback) => {
          due.push(callback);
          return () => undefined;
        },
      },
    );
    const heard: string[] = [];
    repairs.subscribe((keys) => heard.push(...keys));
    for (const tex of ['a', 'b', 'c']) {
      repairs.request(tex, { tex, display: false, error: 'e' }, () => undefined);
    }
    await repairs.settled();
    expect(repairs.unavailable).toBe('limit reached');
    expect(due).toHaveLength(1);
    due[0]!();
    await repairs.settled();
    expect(asked).toBe(3);
    expect(repairs.isPending('b')).toBe(false);
    expect(heard.filter((key) => key === 'b')).toHaveLength(1);
  });

  it('keeps at most so many repairs for the page, the least recently used dropped first', () => {
    const repairs = new ModelRepairs(null);
    for (let index = 0; index <= 2_000; index += 1) {
      repairs.set(`k${index}`, { tex: `x${index}`, model: null });
    }
    expect(repairs.get('k0')).toBeUndefined();
    expect(repairs.get('k2000')).toEqual({ tex: 'x2000', model: null });
    // And within 2,000,000 characters.
    const long = 'x'.repeat(600_000);
    for (const key of ['a', 'b', 'c', 'd']) repairs.set(key, { tex: long, model: null });
    expect(repairs.get('a')).toBeUndefined();
    expect(repairs.get('d')?.tex).toBe(long);
  });
});

describe('the caches', () => {
  it('keep no entry heavier than their whole limit, and drop no other for it', () => {
    const kept = new Kept<string>(10, 100, (key, value) => key.length + value.length);
    kept.set('a', 'x'.repeat(10));
    kept.set('big', 'y'.repeat(1_000));
    expect(kept.get('big')).toBeUndefined();
    expect(kept.get('a')).toBe('x'.repeat(10));
    expect(kept.weight).toBe(11);
  });

  it('weigh what they keep: the oldest go once the total passes the limit', () => {
    const kept = new Kept<string>(10, 100, (key, value) => key.length + value.length);
    for (const key of ['a', 'b', 'c']) kept.set(key, 'x'.repeat(39));
    expect(kept.size).toBe(2);
    expect(kept.has('a')).toBe(false);
    expect(kept.weight).toBe(80);
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

  it("show bidirectional controls in KaTeX's error and the reason as code points", () => {
    // KaTeX's error quotes the expression; the message is also the mark's title.
    const html = recoveredHtml(
      {
        status: 'fallback',
        unicode: 'x',
        error: 'Double superscript at position 3: x^^\u202Eabc',
        state: 'unrepairable',
        reason: 'no \u2066repair',
      },
      'span',
      false,
      '$x$',
    );
    expect(html).not.toMatch(/[\u202E\u2066]/);
    expect(html.match(/⟦U\+202E⟧/g)).toHaveLength(2);
    expect(html.match(/⟦U\+2066⟧/g)).toHaveLength(2);
    expect(html).toContain('<span class="math-message">KaTeX could not render this: Double superscript');
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
