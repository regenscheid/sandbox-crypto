// SPDX-License-Identifier: MIT
// The browser's half of the shared document cases (living documents, LD-F2): the server
// runs the same file through its parser (packages/orchestrator, test_document_parser.py).
import { describe, expect, it } from 'vitest';
import { attachIds, type ServerBlock } from './attach';
import { blockMap } from './blockmap';
import documents from './documents.fixtures.json';
import dollars from './dollarmath.fixtures.json';
import { documentParser, parser } from './markdown';
import delimiters from './mathdelimiters.fixtures.json';
import tables from './tables.fixtures.json';

function parsed(markdown: string) {
  return blockMap(markdown, documentParser.parse(markdown, {}));
}

describe('blocks and formulas, as the server finds them', () => {
  for (const { name, markdown, blocks, math } of [...documents.cases, ...tables.cases]) {
    it(name, () => {
      const found = parsed(markdown);
      expect(found.blocks.map((block) => [block.kind, block.depth, ...block.lines])).toEqual(blocks);
      expect(found.spans.map((span) => [span.block, span.kind, span.tex])).toEqual(math);
    });
  }
});

describe("a document's rules change no formula of the other cases", () => {
  const MATH = new Set(['math_inline', 'math_inline_double', 'math_block', 'math_block_label']);

  function spans(parse: (markdown: string) => ReturnType<typeof parser.parse>, markdown: string) {
    const found: string[] = [];
    const visit = (tokens: ReturnType<typeof parser.parse>) => {
      for (const token of tokens) {
        if (MATH.has(token.type)) found.push(`${token.type} ${token.content} ${token.markup}`);
        if (token.children) visit(token.children);
      }
    };
    visit(parse(markdown));
    return found;
  }

  for (const { name, markdown } of [...dollars.cases, ...delimiters.cases]) {
    it(name, () => {
      // The texts that start with front matter are documents' alone.
      if (markdown.startsWith('---')) return;
      const asText = spans((source) => parser.parse(source, {}), markdown);
      const asDocument = spans((source) => documentParser.parse(source, {}), markdown);
      expect(asDocument).toEqual(asText);
    });
  }
});

describe('the places the parser records', () => {
  it('leaves trailing blank lines out of a block, as the server does', () => {
    const found = parsed('- one\n\n\n\nAfter.\n');
    expect(found.blocks.map((block) => [block.kind, ...block.lines])).toEqual([
      ['list', 0, 1],
      ['list_item', 0, 1],
      ['paragraph', 0, 1],
      ['paragraph', 4, 5],
    ]);
  });

  it("takes a container's closing marker into its lines", () => {
    const found = parsed('::: lemma\nText $x$.\n:::\n');
    expect(found.blocks.map((block) => [block.kind, ...block.lines])).toEqual([
      ['container', 0, 3],
      ['paragraph', 1, 2],
    ]);
  });
});

describe('historical profile 1 maps', () => {
  for (const { name, markdown, blocks } of tables.legacy) {
    it(`keeps the existing mismatch guard for ${name}`, () => {
      const attached = attachIds(parsed(markdown), blocks as ServerBlock[]);
      expect(attached.mismatches.length).toBeGreaterThan(0);
      expect(attached.blocks.size).toBeLessThan(blocks.length);
    });
  }
});

it('keeps image-description math escapes untrimmed and outside document anchors', () => {
  const source = '| ![\x85$x$\\\x85](https://example.org) |\n| --- |\n';
  const tokens = documentParser.parse(source, {});
  const image = tokens.find((token) => token.type === 'inline')?.children?.find(
    (token) => token.type === 'image',
  );
  expect(image?.children?.filter((token) => token.type === 'math_inline').map((token) => token.content)).toEqual(['x']);
  expect(blockMap(source, tokens).spans).toEqual([]);
});
