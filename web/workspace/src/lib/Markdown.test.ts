// SPDX-License-Identifier: MIT
// The Markdown component, rendered as the server would: what it lets through, and its math.
import katex from 'katex';
import { render } from 'svelte/server';
import { describe, expect, it, vi } from 'vitest';
import Markdown from './Markdown.svelte';
import {
  forgetResults,
  MAX_FORMULA,
  MAX_FORMULAS,
  MAX_MATH_TIME,
  MAX_RENDERED,
  MAX_REPAIR_MATH,
  renderMarkdown,
} from './markdown';
import { ModelRepairs, repairKey } from './mathrepair';

function html(source: string): string {
  return render(Markdown, { props: { source } }).body;
}

function count(text: string, pattern: RegExp): number {
  return (text.match(pattern) ?? []).length;
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
    expect(html('$$E = mc^2$$ (eq:energy)')).toContain('<span class="math-label">(eq:energy)</span>');
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

  it('shows bidirectional controls in a formula shown as written as code points', () => {
    const out = html(`$\\gdef\\a{x}\\text{‮evil}$`);
    expect(out).not.toContain('‮');
    expect(out).toContain('\\text{⟦U+202E⟧evil}');
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

  it('sends KaTeX a bounded amount of repaired math', () => {
    // Every formula fails, and each repair is tried in turn until the budget is spent.
    const started = performance.now();
    const out = html('$\\frac{1$ '.repeat(MAX_FORMULAS));
    expect(performance.now() - started).toBeLessThan(3_000);
    expect(count(out, /class="math-mark"/g)).toBe(MAX_FORMULAS);
    expect(count(out, />auto-repaired</g)).toBe(0);
    expect(MAX_REPAIR_MATH).toBe(20_000);
  });

  it('renders a text again without sending KaTeX what it already did', async () => {
    // The review's case: many failing formulas, then the server says it has no model.
    forgetResults();
    const repairs = new ModelRepairs(async () => ({ enabled: false, reason: 'no model' }));
    const source = Array.from({ length: 300 }, (_, i) => `$x^{${i}}^{\\rule{1q}{1q}}$`).join(' ');
    const calls = vi.spyOn(katex, 'renderToString');
    try {
      const first = renderMarkdown(source, repairs);
      expect(first.waiting.size).toBe(300);
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

  it("stops sending KaTeX a text's math once it has worked on it for a while", () => {
    forgetResults();
    const clock = vi.spyOn(performance, 'now');
    let now = 0;
    clock.mockImplementation(() => (now += 400));
    try {
      const out = html('$a$ $b$ $c$ $d$ $e$');
      expect(count(out, /<annotation /g)).toBeLessThan(5);
      expect(out).toContain("not rendered: this text's math took too long");
      expect(MAX_MATH_TIME).toBe(1_000);
    } finally {
      clock.mockRestore();
    }
  });
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
    ['a run of backslashes in every table cell', `| a |\n| - |\n${`| ${'\\'.repeat(1_000)} |\n`.repeat(190)}`],
    [
      'nested definitions',
      `$$\\def\\a{${'x'.repeat(400)}}\\def\\b{\\a\\a\\a\\a\\a\\a\\a\\a\\a\\a}` +
        `\\def\\c{\\b\\b\\b\\b\\b\\b\\b\\b\\b\\b}${'\\c'.repeat(8)}$$`,
    ],
    ['a definition used 20 times', `$$\\def\\a{${'x'.repeat(2_000)}}${'\\a'.repeat(20)}$$`],
    ['a definition joined by the repairs', `$\\d\\label{x}ef\\a{${'x'.repeat(2_000)}}${'\\a'.repeat(20)}\\foo$`],
    ['the heaviest formulas without definitions', '$\\Huge x\\tiny y$'.repeat(1).concat(`$$${'\\Huge x\\tiny y'.repeat(357)}$$\n\n`.repeat(4))],
  ])('renders %s quickly', (_name, source) => {
    const started = performance.now();
    html(source);
    expect(performance.now() - started).toBeLessThan(3_000);
  });
});

describe('code', () => {
  it('shows bidirectional controls as code points, and leaves prose alone', () => {
    const out = html('Run `rm -i ‮exe.txt` or\n\n```\nok⁦ ⁩\n```\n\nתודה‏.');
    expect(out).toContain('<code>rm -i ⟦U+202E⟧exe.txt</code>');
    expect(out).toContain('ok⟦U+2066⟧ ⟦U+2069⟧');
    expect(out).toContain('תודה‏.');
  });
});
