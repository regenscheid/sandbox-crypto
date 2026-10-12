// SPDX-License-Identifier: MIT
// A living document rendered with the server's IDs (LD-9) and its front matter's macros
// (LD-28), within the bounds on what they expand to.
import { render } from 'svelte/server';
import { beforeEach, describe, expect, it } from 'vitest';
import type { ServerBlock } from './attach';
import Markdown from './Markdown.svelte';
import { MAX_EXPANDED, MAX_MACRO, MAX_MACROS, macroSet, NO_MACROS } from './macros';
import { forgetResults, renderDocument, type DocumentInput } from './markdown';

beforeEach(() => forgetResults());

const SOURCE = [
  '---',
  'title: Fermat',
  'macros:',
  '  \\F: \\mathbb{F}',
  '---',
  '# Fermat $x$',
  '',
  'Let $a \\in \\F_p$.',
  '',
  '::: lemma (fermat)',
  'Every $a$.',
  ':::',
  '',
  '$$',
  'a^{p-1} = 1',
  '$$',
  '',
].join('\n');

function server(paragraphLines: [number, number] = [7, 8]): ServerBlock[] {
  return [
    { id: 'b1', kind: 'front_matter', depth: 0, lines: [0, 5], status: 'kept', math: [] },
    {
      id: 'b2',
      kind: 'heading',
      depth: 0,
      lines: [5, 6],
      status: 'kept',
      math: [{ id: 'm1', kind: 'inline', tex: 'x' }],
    },
    {
      id: 'b3',
      kind: 'paragraph',
      depth: 0,
      lines: paragraphLines,
      status: 'changed',
      math: [{ id: 'm2', kind: 'inline', tex: 'a \\in \\F_p' }],
    },
    { id: 'b4', kind: 'container', depth: 0, lines: [9, 12], status: 'kept', math: [] },
    {
      id: 'b5',
      kind: 'paragraph',
      depth: 1,
      lines: [10, 11],
      status: 'added',
      math: [{ id: 'm3', kind: 'inline', tex: 'a' }],
    },
    {
      id: 'b6',
      kind: 'equation',
      depth: 0,
      lines: [13, 16],
      status: 'kept',
      math: [{ id: 'm4', kind: 'display', tex: '\na^{p-1} = 1\n' }],
    },
  ];
}

const MACROS = macroSet({ '\\F': '\\mathbb{F}' });

function document(blocks = server(), macros = MACROS): DocumentInput {
  return { blocks, macros };
}

/** The element whose attribute `name` is `value`, as far as its opening tag. */
function tagWith(html: string, name: string, value: string): string {
  const at = html.indexOf(`${name}="${value}"`);
  expect(at, `${name}="${value}"`).toBeGreaterThan(-1);
  return html.slice(html.lastIndexOf('<', at), html.indexOf('>', at) + 1);
}

/** The formula whose element carries `data-math="id"`, up to the next formula. */
function formula(html: string, id: string): string {
  const at = html.lastIndexOf('<', html.indexOf(`data-math="${id}"`));
  const next = html.indexOf('data-math="', at + 20);
  return html.slice(at, next === -1 ? undefined : next);
}

describe('the server’s IDs on the browser’s parse', () => {
  it('marks each block and formula with its ID, and each block the revision changed', () => {
    const rendered = renderDocument(SOURCE, document());
    expect(rendered.mismatches).toEqual([]);
    const html = rendered.html;
    expect(tagWith(html, 'data-block', 'b1')).toMatch(/^<details class="front-matter"/);
    expect(tagWith(html, 'data-block', 'b2')).toMatch(/^<h1 /);
    expect(tagWith(html, 'data-block', 'b3')).toBe('<p data-block="b3" data-status="changed">');
    expect(tagWith(html, 'data-block', 'b4')).toBe(
      '<section data-block="b4" class="theorem theorem-lemma">',
    );
    expect(html).toContain('<p class="theorem-head">Lemma (fermat).</p>');
    expect(tagWith(html, 'data-block', 'b5')).toBe('<p data-block="b5" data-status="added">');
    for (const id of ['m1', 'm2', 'm3']) expect(tagWith(html, 'data-math', id)).toMatch(/^<span /);
    // A displayed equation is a block of its own and a formula.
    expect(tagWith(html, 'data-math', 'm4')).toMatch(/^<div data-math="m4" data-block="b6" /);
    expect(html).not.toContain('data-unmatched');
  });

  it('shows where the parsers disagree, without anchors there', () => {
    const rendered = renderDocument(SOURCE, document(server([6, 8])));
    expect(rendered.mismatches).toEqual([
      { lines: [6, 8], message: 'the server reads a paragraph (b3) the browser does not' },
      { lines: [7, 8], message: 'the browser reads a paragraph the server does not' },
    ]);
    expect(rendered.html).toContain('<p data-unmatched="">');
    expect(rendered.html).not.toContain('data-math="m2"');
    expect(rendered.html).toContain('data-math="m3"');
  });

  it('gives no formula an ID when the two read its TeX otherwise', () => {
    const blocks = server();
    blocks[2] = { ...blocks[2]!, math: [{ id: 'm2', kind: 'inline', tex: 'a \\in \\F_q' }] };
    const rendered = renderDocument(SOURCE, document(blocks));
    expect(rendered.mismatches).toEqual([
      { lines: [7, 8], message: 'b3 has 1 formula on the server, which the browser reads otherwise' },
    ]);
    expect(rendered.html).not.toContain('data-math="m2"');
    // The block keeps its ID: only its formulas lose theirs.
    expect(rendered.html).toContain('data-block="b3"');
  });

  it("takes the server's formulas without their TeX when it could not parse them", () => {
    const blocks = server().map((block) => ({
      ...block,
      math: block.math.map((span) => ({ ...span, tex: null })),
    }));
    const rendered = renderDocument(SOURCE, document(blocks));
    expect(rendered.mismatches).toEqual([]);
    expect(rendered.html).toContain('data-math="m2"');
  });

  it('escapes what a container says of itself', () => {
    const rendered = renderDocument('::: lemma <b>x</b> "y"\nText.\n:::\n', {
      blocks: [],
      macros: NO_MACROS,
    });
    expect(rendered.html).toContain('<p class="theorem-head">Lemma &lt;b&gt;x&lt;/b&gt; &quot;y&quot;.</p>');
  });
});

describe("a document's displayed equations", () => {
  it("render without the markers of the quote they are in, which their TeX keeps", () => {
    const source = '> Quoted:\n>\n> $$\n> \\sum_{a} a = 0\n> $$\n';
    const rendered = renderDocument(source, { blocks: [], macros: NO_MACROS });
    const annotation = /<annotation encoding="application\/x-tex">([^<]*)<\/annotation>/.exec(
      rendered.html,
    );
    expect(annotation?.[1]).toBe('\\sum_{a} a = 0');
    // Shown as written, it keeps them.
    const broken = renderDocument('> $$\n> \\frac{1\n> $$\n', { blocks: [], macros: NO_MACROS });
    expect(broken.html).toContain('data-repair');
  });
});

describe("a document's macros", () => {
  it('render its math, and the same formula without them goes to the repairs', () => {
    const withMacros = renderDocument(SOURCE, document());
    expect(formula(withMacros.html, 'm2')).toContain('mathbb');
    expect(formula(withMacros.html, 'm2')).not.toContain('data-repair');
    // KaTeX's results are kept by the macros in force: no result crosses over.
    const without = renderDocument(SOURCE, document(server(), NO_MACROS));
    expect(formula(without.html, 'm2')).toContain('data-repair');
    const again = renderDocument(SOURCE, document());
    expect(formula(again.html, 'm2')).not.toContain('data-repair');
  });

  it('may not expand too far: three short macros once froze a page for 39 s', () => {
    const macros = macroSet({
      '\\a': 'x'.repeat(400),
      '\\b': '\\a'.repeat(10),
      '\\c': '\\b'.repeat(10),
    });
    expect(macros.problems).toEqual([]);
    const started = performance.now();
    const rendered = renderDocument(`$$${'\\c'.repeat(8)}$$\n\n$\\a$\n`, { blocks: [], macros });
    expect(performance.now() - started).toBeLessThan(500);
    expect(rendered.html).toContain(
      `not rendered: its macros expand to more than ${MAX_EXPANDED} characters`,
    );
    // The equation is a block of its own, which no server's block map names here.
    const shown = rendered.html.replace(/ data-math-source="[^"]*"/g, '');
    expect(shown).toMatch(/^<div data-unmatched="" class="math math-skipped">/);
    expect(shown).toContain('<span class="math math-inline"><span class="katex">');
  });

  it('are left out when they define others, use an argument twice, or are too many or long', () => {
    const definitions: Record<string, string> = {
      '\\ok': '\\left|#1\\right|',
      '\\hash': '##1 #1',
      '\\defines': '\\def\\x{1}',
      '\\global': '\\gdef\\y{2}',
      '\\twice': '#1 \\cdot #1',
      '\\long': 'x'.repeat(MAX_MACRO + 1),
      'plain': 'x',
    };
    const found = macroSet(definitions);
    expect(found.definitions).toEqual({ '\\ok': '\\left|#1\\right|', '\\hash': '##1 #1' });
    expect(found.problems).toEqual([
      '\\defines defines another (\\def)',
      '\\global defines another (\\gdef)',
      '\\twice uses #1 more than once',
      `\\long is longer than ${MAX_MACRO} characters`,
      "plain is not a macro's name and definition",
    ]);
    const many: Record<string, string> = {};
    for (let index = 0; index <= MAX_MACROS; index += 1) many[`\\m${'a'.repeat(index + 1)}`] = 'x';
    const capped = macroSet(many);
    expect(Object.keys(capped.definitions)).toHaveLength(MAX_MACROS);
    expect(capped.problems).toEqual([`only the first ${MAX_MACROS} macros are used`]);
  });

  it('have one key for the same definitions, whatever their order', () => {
    const first = macroSet({ '\\a': 'x', '\\b': 'y' });
    const second = macroSet({ '\\b': 'y', '\\a': 'x' });
    expect(first.key).toBe(second.key);
    expect(first.key).not.toBe('');
    expect(macroSet({ '\\a': 'x' }).key).not.toBe(first.key);
    expect(NO_MACROS.key).toBe('');
  });
});

describe('the Markdown component', () => {
  it('renders a document with its IDs when given one, and a text without', () => {
    const asDocument = render(Markdown, { props: { source: SOURCE, document: document() } }).body;
    expect(asDocument).toContain('data-block="b3"');
    expect(asDocument).toContain('<details class="front-matter"');
    const asText = render(Markdown, { props: { source: SOURCE } }).body;
    expect(asText).not.toContain('data-block');
    // A text knows no front matter or containers: they stay text.
    expect(asText).toContain('::: lemma (fermat)');
  });
});
