// SPDX-License-Identifier: MIT
// The Markdown component, rendered as the server would: what it lets through, and its math.
import katex from 'katex';
import { render } from 'svelte/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Markdown from './Markdown.svelte';
import { macroSet } from './macros';
import {
  forgetResults,
  MAX_FORMULA,
  MAX_FORMULAS,
  MAX_GO_TIME,
  MAX_RENDERED,
  MAX_REPAIR_MATH,
  renderMarkdown,
  textState,
  whenMathTime,
  type Rendered,
  type TextState,
} from './markdown';
import { ModelRepairs, repairKey } from './mathrepair';

// Each test starts with KaTeX's results forgotten and a go of its own.
beforeEach(() => forgetResults());

function html(source: string): string {
  return withoutCopySource(render(Markdown, { props: { source } }).body);
}

/** Existing presentation assertions exclude the copy-only source attribute. Its exact
 * escaping/delimiters/repairs are separately covered by mathcopy.test.ts. */
function withoutCopySource(html: string): string {
  return html.replace(/ data-math-source="[^"]*"/g, '');
}

function count(text: string, pattern: RegExp): number {
  return (text.match(pattern) ?? []).length;
}

/** The next go: a later task. */
function nextGo(): Promise<void> {
  return new Promise((resolve) => whenMathTime(resolve));
}

/** What `renderAll` saw. */
interface Pages {
  rendered: Rendered[];
  /** For each text, its renderings that did work, not the turns it waited through. */
  renders: number[];
  /** How long the first view took, and each go after it. */
  firstView: number;
  goes: number[];
}

/**
 * Texts rendered as their components render them: all at once for the page's first view,
 * then each again in its turn in later goes, until all are done.
 */
async function renderAll(sources: string[], repairs: ModelRepairs | null = null): Promise<Pages> {
  const states: TextState[] = sources.map(() => textState());
  const renders = sources.map(() => 0);
  const goes: number[] = [];
  let go: { started: number; ended: number } | null = null;
  const render = (index: number): Rendered => {
    const before = states[index]!.last;
    const out = renderMarkdown(sources[index]!, repairs, states[index]!);
    if (states[index]!.last !== before) renders[index]! += 1;
    return out;
  };
  const wait = (index: number): void => {
    whenMathTime(() => {
      const started = performance.now();
      rendered[index] = render(index);
      // The turns one go gives, one after another in its task, timed together.
      if (go === null) {
        go = { started, ended: performance.now() };
        queueMicrotask(() => {
          goes.push(go!.ended - go!.started);
          go = null;
        });
      } else {
        go.ended = performance.now();
      }
      if (rendered[index]!.unfinished) wait(index);
    });
  };
  const started = performance.now();
  const rendered = sources.map((_, index) => render(index));
  const firstView = performance.now() - started;
  rendered.forEach((text, index) => {
    if (text.unfinished) wait(index);
  });
  for (let turn = 0; rendered.some((text) => text.unfinished); turn += 1) {
    if (turn > 20_000) throw new Error('the texts never finished');
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return { rendered, renders, firstView, goes };
}

describe('raw HTML', () => {
  it('is escaped, never rendered', () => {
    const out = html(
      '<script>alert(1)</script>\n\nText <img src=x onerror="alert(1)"> and ' +
        '<b onclick="steal()">bold</b>.\n\n<iframe src="https://evil.example"></iframe>',
    );
    expect(out).not.toMatch(/<(script|img|iframe|b)\b/i);
    expect(out).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(out).toContain('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
    expect(out).toContain('&lt;b onclick=&quot;steal()&quot;&gt;bold&lt;/b&gt;');
  });

  it('stays text inside math and code', () => {
    const out = html('$<script>alert(1)</script>$ and `<script>x</script>`\n\n```\n<style>\n```');
    expect(out).not.toMatch(/<(script|style)\b/i);
  });
});

describe('links', () => {
  it.each([
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    'jav&#x09;ascript:alert(1)',
    'javascript&colon;alert(1)',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    '//evil.example/x',
    '/api/v0/logout',
    'relative/page',
    'http:/api/v0/logout',
    'https:evil.example',
  ])('drops %s', (url) => {
    const out = html(`[click](${url}) and <${url}> and [ref]\n\n[ref]: ${url}`);
    expect(out).not.toMatch(/<a\b/);
    expect(out).not.toMatch(/href=/);
  });

  it('keeps http, https and mailto, and opens them apart', () => {
    const out = html(
      '[a](https://example.org/x?y=1) [b](http://example.org) ' +
        '[c](mailto:someone@example.org) <https://example.org/auto>',
    );
    expect(count(out, /<a /g)).toBe(4);
    expect(count(out, /target="_blank" rel="noopener noreferrer nofollow"/g)).toBe(4);
    expect(out).toContain('href="https://example.org/x?y=1"');
    expect(out).toContain('href="mailto:someone@example.org"');
  });

  it('leaves bare addresses as text', () => {
    expect(html('see https://example.org/page')).not.toContain('<a ');
  });

  it('shows images as links and never loads them', () => {
    const out = html('![a plot](https://example.org/plot.png) ![x](javascript:alert(1))');
    expect(out).not.toMatch(/<img\b/);
    expect(out).toContain(
      '<a class="image-link" href="https://example.org/plot.png" target="_blank" ' +
        'rel="noopener noreferrer nofollow">image: a plot</a>',
    );
    expect(count(out, /<a /g)).toBe(1);
  });
});

describe('math', () => {
  it('renders inline math', () => {
    const out = html('Let $x^2 + 1$ be prime.');
    expect(out).toContain('<span class="math math-inline"><span class="katex">');
    expect(out).toContain('<annotation encoding="application/x-tex">x^2 + 1</annotation>');
  });

  it('renders display math', () => {
    const out = html('$$\n\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}\n$$');
    expect(out).toContain('<div class="math math-display"><span class="katex-display">');
    expect(out).toContain('\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}</annotation>');
  });

  it('renders display math within a paragraph', () => {
    const out = html('so $$x = y$$ holds');
    expect(out).toContain('<span class="math math-display"><span class="katex-display">');
  });

  it('shows an equation label beside the equation', () => {
    expect(html('$$E = mc^2$$ (eq:energy)')).toContain(
      '<span class="math-label">(eq:energy)</span>',
    );
  });

  it('approximates math nothing repairs, and keeps it as written behind a mark', () => {
    const out = html('A broken formula $\\frac{1$ and a good one, $y$.');
    expect(out).toContain(
      '<span class="math math-inline math-approximated" data-repair="approximation" ' +
        'data-state="unrepairable"><span class="math-unicode">1/()</span>',
    );
    expect(out).toContain('>approximated</button>');
    expect(out).toContain('<code class="math-source">$\\frac{1$</code>');
    expect(out).toContain('KaTeX could not render this: Unexpected end of input');
    expect(out).not.toContain('katex-error');
    expect(out).toContain('<annotation encoding="application/x-tex">y</annotation>');
  });

  it('approximates displayed math as a block', () => {
    const out = html('$$\n\\begin{pmatrix} 1 & 2\n$$');
    expect(out).toContain('<div class="math math-display math-approximated"');
    expect(out).toContain('<code class="math-source">$$\n\\begin{pmatrix} 1 &amp; 2\n$$</code>');
  });

  it.each([
    '\\href{https://evil.example}{x}',
    '\\url{https://evil.example}',
    '\\includegraphics{https://evil.example/x.png}',
    '\\htmlClass{evil}{x}',
    '\\htmlId{app}{x}',
    '\\htmlStyle{position:fixed}{x}',
    '\\htmlData{x=y}{z}',
  ])('keeps %s from reaching out', (command) => {
    // The command shows in red as written (its source is also in the MathML annotation),
    // and nothing of it becomes a link, an image, a class, an id, a style or data.
    const out = html(`$${command}$`);
    expect(out).toContain('mathcolor="#cc0000"');
    expect(out).not.toMatch(/<a\b|<img\b|class="[^"]*evil|id="app"|style="[^"]*position|data-x=/);
  });

  it('limits sizes and macro expansion', () => {
    expect(html('$\\rule{1000em}{1000em}$')).toContain('width="20em" height="20em"');
    const loop = html('$\\mathrm{' + 'a~'.repeat(1_100) + '}$');
    expect(loop).toContain('math-approximated');
    expect(loop).toContain('Too many expansions');
  });

  it('shows a formula that defines a macro as written, and shares no macros', () => {
    // Macros come from the project; a definition could multiply KaTeX's work. The next
    // formula's \secret is unknown: the repairs read it as an operator's name, and say so.
    const out = html('$\\gdef\\secret{42}\\secret$ and $\\secret$ and $\\def\\a{\\a\\a}\\a$');
    expect(out).not.toContain('<mn>42</mn>');
    expect(out).toContain(
      '<span class="math math-skipped"><code class="math-source">$\\gdef\\secret{42}\\secret$</code>' +
        '<span class="math-message">not rendered: a formula may not define macros (\\gdef)</span>',
    );
    expect(out).toContain('a formula may not define macros (\\def)');
    expect(out).toContain('<annotation encoding="application/x-tex">\\operatorname{secret}');
    expect(out).toContain('data-repair="heuristic"');
    expect(out).toContain('<code class="math-source">$\\secret$</code>');
  });

  it('reads no definition in a \\verb body or a comment, as KaTeX reads none there', () => {
    const out = html('$\\verb|\\def\\a{x}|$ and $x % \\def\\a{x}\\a$');
    expect(out).not.toContain('may not define macros');
    expect(count(out, /<annotation /g)).toBe(2);
    // After the comment's line, and after \verb's closing delimiter, a definition is one.
    expect(html('$x % note\n\\def\\a{x}\\a$')).toContain('may not define macros (\\def)');
    expect(html('$\\verb|x|\\def\\a{x}\\a$')).toContain('may not define macros (\\def)');
  });

  it('refuses at once a definition hidden behind % and a combining mark', () => {
    // The third review's formula: \@secondoftwo skips `{%\u0301}` unparsed, and KaTeX took 39 s.
    const bomb =
      `\\@secondoftwo{%\u0301}{}\\def\\a{${'x'.repeat(400)}}\\def\\b{${'\\a'.repeat(10)}}` +
      `\\def\\c{${'\\b'.repeat(10)}}${'\\c'.repeat(8)}`;
    const started = performance.now();
    const out = html(`$$${bomb}$$`);
    expect(performance.now() - started).toBeLessThan(500);
    expect(out).toContain('not rendered: a formula may not define macros (\\def)');
    expect(out).not.toContain('katex-display');
  });

  it('shows bidirectional controls in a formula shown as written as code points', () => {
    const out = html(`$\\gdef\\a{x}\\text{\u202Eevil}$`);
    expect(out).not.toContain('\u202E');
    expect(out).toContain('\\text{⟦U+202E⟧evil}');
  });

  it("shows bidirectional controls in KaTeX's error, in a mark's message and title, as code points", () => {
    // KaTeX's error quotes the formula.
    const out = html('$x^^\u202Eabc$');
    expect(out).not.toContain('\u202E');
    expect(out).toMatch(/<button [^>]*title="[^"]*⟦U\+202E⟧/);
    expect(out).toMatch(/<span class="math-message">[^<]*⟦U\+202E⟧/);
  });

  it('leaves currency as text', () => {
    expect(html('It costs $5 and $10 a run.')).not.toContain('katex');
  });

  it('keeps every formula’s source for copying', () => {
    expect(html('$a < b \\land c > d$')).toContain(
      '<annotation encoding="application/x-tex">a &lt; b \\land c &gt; d</annotation>',
    );
  });

  it('renders LaTeX’s own delimiters, and shows them as written', () => {
    const out = html('Let \\(x^2\\) and\n\\[\ny < 1\n\\]\nand \\(\\frac{1\\).');
    expect(out).toContain('<span class="math math-inline"><span class="katex">');
    expect(out).toContain('<div class="math math-display"><span class="katex-display">');
    expect(out).toContain('<code class="math-source">\\(\\frac{1\\)</code>');
  });
});

describe('repairs', () => {
  it('repairs what KaTeX rejects when a rule fits, and marks it', () => {
    const out = html('The trace $\\Tr(A)$ and $x_i_j$.');
    expect(out).toContain('<annotation encoding="application/x-tex">\\operatorname{Tr}(A)');
    expect(out).toContain('<annotation encoding="application/x-tex">x_{i_j}');
    expect(count(out, />auto-repaired<\/button>/g)).toBe(2);
    expect(out).toContain('<code class="math-source">$x_i_j$</code>');
  });

  it('shows the reviewer model’s repair the page has, and asks for the others', async () => {
    const asked: string[] = [];
    const repairs = new ModelRepairs(async (request) => {
      asked.push(request.tex);
      return { enabled: true, tex: '\\frac{1}{2}', model: 'reviewer' };
    });
    repairs.set(repairKey('\\frac{1', false), { tex: '\\frac{1}{x}', model: 'reviewer' });
    const first = renderMarkdown('$\\frac{1$ and $\\frac{2$', repairs);
    expect(first.html).toContain('>model-repaired</button>');
    expect(first.html).toContain('<annotation encoding="application/x-tex">\\frac{1}{x}');
    expect(first.html).toContain('data-state="pending"');
    expect([...first.waiting]).toEqual([repairKey('\\frac{2', false)]);
    await repairs.settled();
    expect(asked).toEqual(['\\frac{2']);
    const second = renderMarkdown('$\\frac{1$ and $\\frac{2$', repairs);
    expect(count(second.html, />model-repaired<\/button>/g)).toBe(2);
    expect(second.waiting.size).toBe(0);
  });

  it('sends KaTeX a bounded amount of repaired math', async () => {
    // Every formula fails, and each repair is tried in turn until the budget is spent.
    const started = performance.now();
    const {
      rendered: [out],
    } = await renderAll(['$\\frac{1$ '.repeat(MAX_FORMULAS)]);
    expect(performance.now() - started).toBeLessThan(3_000);
    expect(count(out!.html, /class="math-mark"/g)).toBe(MAX_FORMULAS);
    expect(count(out!.html, />auto-repaired</g)).toBe(0);
    expect(MAX_REPAIR_MATH).toBe(20_000);
  });

  it('renders a text again without sending KaTeX what it already did', async () => {
    // The review's case: many failing formulas, then the server says it has no model.
    const repairs = new ModelRepairs(async () => ({ enabled: false, reason: 'no model' }));
    const source = Array.from({ length: 300 }, (_, i) => `$x^{${i}}^{\\rule{1q}{1q}}$`).join(' ');
    const calls = vi.spyOn(katex, 'renderToString');
    try {
      const {
        rendered: [first],
      } = await renderAll([source], repairs);
      expect(first!.waiting.size).toBe(300);
      const firstCalls = calls.mock.calls.length;
      expect(firstCalls).toBeGreaterThanOrEqual(300);
      await repairs.settled();
      const started = performance.now();
      const second = renderMarkdown(source, repairs);
      expect(performance.now() - started).toBeLessThan(500);
      expect(calls.mock.calls.length).toBe(firstCalls);
      expect(second.waiting.size).toBe(0);
      expect(count(second.html, /data-state="unrepairable"/g)).toBe(300);
      expect(second.html).toContain('No repair: no model.');
    } finally {
      calls.mockRestore();
    }
  });
});

describe('goes', () => {
  /** A clock that moves only while KaTeX works: each formula takes it `ms`. */
  function slowKatex(ms: number) {
    let now = 0;
    const original = katex.renderToString.bind(katex);
    const calls: string[] = [];
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => now);
    const spy = vi.spyOn(katex, 'renderToString').mockImplementation((tex, options) => {
      calls.push(tex);
      now += ms;
      return original(tex, options);
    });
    return {
      calls,
      restore: () => {
        clock.mockRestore();
        spy.mockRestore();
      },
    };
  }

  it('render texts one after another until the time is spent; the rest wait their turn', async () => {
    expect(MAX_GO_TIME).toBe(250);
    const katexTime = slowKatex(100);
    try {
      const sources = ['$a$ $b$ $c$', '$d$ $e$ $f$', '$g$'];
      const states = sources.map(() => textState());
      const rendered = sources.map((source, index) => renderMarkdown(source, null, states[index]));
      // The first view: a, b and c spend the go, so the other texts wait, shown as written.
      expect(katexTime.calls).toEqual(['a', 'b', 'c']);
      expect(rendered.map((text) => text.unfinished)).toEqual([false, true, true]);
      expect(rendered[1]!.html).toBe('<pre class="markdown-waiting">$d$ $e$ $f$</pre>');
      expect(states.map((state) => state.last !== null)).toEqual([true, false, false]);
      // Each renders in its turn, as its component does; a turn the time does not reach
      // waits for the next go, unrendered.
      for (const index of [1, 2]) {
        whenMathTime(() => {
          rendered[index] = renderMarkdown(sources[index]!, null, states[index]);
        });
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(katexTime.calls).toEqual(['a', 'b', 'c', 'd', 'e', 'f']);
      expect(rendered.map((text) => text.unfinished)).toEqual([false, false, true]);
      expect(states[2]!.last).toBeNull();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(katexTime.calls).toEqual(['a', 'b', 'c', 'd', 'e', 'f', 'g']);
      expect(rendered.every((text) => !text.unfinished)).toBe(true);
    } finally {
      katexTime.restore();
    }
  });

  it('show a text that waits as it was last rendered', () => {
    const katexTime = slowKatex(300); // one formula spends the go
    try {
      const state = textState();
      const first = renderMarkdown('$a$ and *more*', null, state);
      expect(first.unfinished).toBe(false);
      // Rendered again in the same go, as when a repair arrives: it waits, as it was.
      const again = renderMarkdown('$a$ and *more*', null, state);
      expect(again.unfinished).toBe(true);
      expect(again.html).toBe(first.html);
      expect(katexTime.calls).toEqual(['a']);
    } finally {
      katexTime.restore();
    }
  });

  it('keep a text’s own results for its next go, whatever the page’s cache drops', () => {
    const katexTime = slowKatex(100);
    try {
      const state = textState();
      const first = renderMarkdown('$a$ $b$ $c$ $d$', null, state);
      expect(first.unfinished).toBe(true);
      expect(withoutCopySource(first.html)).toContain(
        '<span class="math math-skipped"><code class="math-source">$d$</code>' +
          '<span class="math-message">not rendered yet: KaTeX renders a page’s math a part at a time',
      );
      forgetResults(); // the page's cache dropped all, and a go began
      expect(renderMarkdown('$a$ $b$ $c$ $d$', null, state).unfinished).toBe(false);
      expect(katexTime.calls).toEqual(['a', 'b', 'c', 'd']);
    } finally {
      katexTime.restore();
    }
  });

  it('always give the first formula of a go, however long the Markdown took', () => {
    // Without that, a text whose Markdown alone took a go would never get further.
    let now = 0;
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => (now += 1_000));
    try {
      const out = renderMarkdown('*slow* $a$ $b$', null, textState());
      expect(count(out.html, /<annotation /g)).toBe(1);
      expect(out.unfinished).toBe(true);
    } finally {
      clock.mockRestore();
    }
  });

  it('do not ask the model for a formula whose rules had no time, and try them next go', async () => {
    let asked = 0;
    const repairs = new ModelRepairs(async () => {
      asked += 1;
      return { enabled: true, tex: 'x', model: 'reviewer' };
    });
    const katexTime = slowKatex(300); // one formula spends the go
    try {
      const state = textState();
      const first = renderMarkdown('$\\Tr x$', repairs, state);
      expect(first.unfinished).toBe(true);
      expect(first.waiting.size).toBe(0);
      expect(first.html).toContain('not rendered yet');
      await repairs.settled();
      expect(asked).toBe(0);
      await nextGo();
      const second = renderMarkdown('$\\Tr x$', repairs, state);
      expect(second.unfinished).toBe(false);
      expect(second.html).toContain('data-repair="heuristic"');
      await repairs.settled();
      expect(asked).toBe(0);
    } finally {
      katexTime.restore();
    }
  });

  it("hold the page's thread a bounded time, render each text only in its turn, and finish", async () => {
    // The review's page, smaller: long texts of Markdown, each with slow formulas, broken
    // so that the repairs run too.
    const prose =
      'Lorem *ipsum* `code` [link](https://example.org/a) dolor $5 and **bold** text.\n';
    const formula = (n: number) => `$$${'\\Huge xy\\tiny xy'.repeat(300)}${n}\\notdefined$$`;
    const sources = Array.from(
      { length: 10 },
      (_, text) =>
        `${prose.repeat(1_250)}\n\n${[0, 1].map((n) => formula(text * 10 + n)).join('\n\n')}`,
    );
    const pages = await renderAll(sources);
    expect(pages.firstView).toBeLessThan(1_000);
    expect(pages.goes.length).toBeGreaterThan(1);
    expect(Math.max(...pages.goes)).toBeLessThan(1_000);
    for (const text of pages.rendered) expect(count(text.html, />auto-repaired</g)).toBe(2);
    // Before, every waiting text rendered again in every go: here, only in its turn.
    expect(Math.max(...pages.renders)).toBeLessThanOrEqual(6);
  }, 60_000);
});

describe('long text', () => {
  it('is shown as written', () => {
    const out = html('$x$ '.repeat(MAX_RENDERED / 4 + 1));
    expect(out).toContain('shown as written');
    expect(out).not.toContain('katex');
  });

  it('sends KaTeX a bounded amount of math', () => {
    const many = html('$x$ '.repeat(MAX_FORMULAS + 5));
    expect(count(many, /<annotation /g)).toBe(MAX_FORMULAS);
    expect(count(many, /this text has too much math/g)).toBe(5);
    const long = html(`$${'x+'.repeat(MAX_FORMULA)}1$ and $y$`);
    expect(long).toContain('not rendered: too long a formula');
    expect(long).toContain('<annotation encoding="application/x-tex">y</annotation>');
  });

  // Inputs that took from seconds to minutes before (the review of 2026-10-11): a trim
  // with a quadratic regex, and every unclosed $$ looking to the end of the text.
  it.each([
    ['spaces after $$', '$$' + ' '.repeat(40_000) + 'x'],
    ['ideographic spaces after $$', '$$' + '　'.repeat(MAX_RENDERED - 3) + 'x'],
    ['an unclosed $$ in every list item', '- $$a\n'.repeat(20_000)],
    ['an unclosed $$ on every line', '$$a\n'.repeat(30_000)],
    ['unclosed $$ with blank lines between halves', '- $$a\n'.repeat(15_000) + '\n- $$a'],
    ['an unclosed \\( in every word', '\\(a '.repeat(45_000)],
    ['an unclosed \\[ on every line', '\\[\n'.repeat(60_000)],
    ['an unclosed environment on every line', '\\begin{align}\n'.repeat(14_000)],
    ['an unclosed \\[ in every list item', '- \\[\n'.repeat(40_000)],
    ['a closing \\] too far from every \\[', '\\[\nx\n'.repeat(39_000) + '\\]'],
    ['a closing \\] after text on every line', '\\[ x \\] y\n'.repeat(19_000)],
    // The review of 2026-10-11 (PR #61): a long run of backslashes, and macro bombs.
    ['a run of backslashes', '\\'.repeat(199_000)],
    [
      'a run of backslashes in every table cell',
      `| a |\n| - |\n${`| ${'\\'.repeat(1_000)} |\n`.repeat(190)}`,
    ],
    [
      'nested definitions',
      `$$\\def\\a{${'x'.repeat(400)}}\\def\\b{\\a\\a\\a\\a\\a\\a\\a\\a\\a\\a}` +
        `\\def\\c{\\b\\b\\b\\b\\b\\b\\b\\b\\b\\b}${'\\c'.repeat(8)}$$`,
    ],
    ['a definition used 20 times', `$$\\def\\a{${'x'.repeat(2_000)}}${'\\a'.repeat(20)}$$`],
    [
      'a definition joined by the repairs',
      `$\\d\\label{x}ef\\a{${'x'.repeat(2_000)}}${'\\a'.repeat(20)}\\foo$`,
    ],
    [
      'the heaviest formulas without definitions',
      '$\\Huge x\\tiny y$'.repeat(1).concat(`$$${'\\Huge x\\tiny y'.repeat(357)}$$\n\n`.repeat(4)),
    ],
    // The third review's round: every \[ before one \] judged by reading its content.
    ['escaped brackets opening before one closing bracket', `x ${'\\[a '.repeat(45_000)}\\]`],
    ['escapes in brackets opening before one closing bracket', `x ${'\\[\\* '.repeat(35_000)}\\]`],
  ])('renders %s quickly', (_name, source) => {
    const started = performance.now();
    html(source);
    expect(performance.now() - started).toBeLessThan(3_000);
  });
});

describe('code', () => {
  it('shows bidirectional controls as code points, and leaves prose alone', () => {
    const out = html('Run `rm -i \u202Eexe.txt` or\n\n```\nok\u2066 \u2069\n```\n\nתודה\u200F.');
    expect(out).toContain('<code>rm -i ⟦U+202E⟧exe.txt</code>');
    expect(out).toContain('ok⟦U+2066⟧ ⟦U+2069⟧');
    expect(out).toContain('תודה\u200F.');
  });
});

it('renders comment text with explicit document macros without attaching document IDs', () => {
  const macros = macroSet({ '\\F': '\\mathbb{F}' });
  const rendered = renderMarkdown('Comment on $\\F_{17}$.', null, null, macros);
  expect(rendered.html).toContain('katex');
  expect(rendered.html).not.toContain('data-block');
  expect(rendered.html).not.toContain('Undefined control sequence');
  const unrelated = renderMarkdown('Comment on $\\F_{17}$.');
  expect(unrelated.html).toContain('Undefined control sequence');
});
