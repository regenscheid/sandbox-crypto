// SPDX-License-Identifier: MIT
// The Markdown component, rendered as the server would: what it lets through, and its math.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Markdown from './Markdown.svelte';
import {
  MAX_FORMULA,
  MAX_FORMULAS,
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
    const loop = html('$\\def\\a{\\a\\a}\\a$');
    expect(loop).toContain('math-approximated');
    expect(loop).toContain('Too many expansions');
  });

  it('shares no macros between formulas', () => {
    // A formula's \gdef does not reach the next one, where \secret is unknown: the repairs
    // read it as an operator's name, and say so.
    const out = html('$\\gdef\\secret{42}\\secret$ and $\\secret$');
    expect(count(out, /<mn>42<\/mn>/g)).toBe(1);
    expect(out).toContain('<annotation encoding="application/x-tex">\\operatorname{secret}');
    expect(out).toContain('data-repair="heuristic"');
    expect(out).toContain('<code class="math-source">$\\secret$</code>');
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
