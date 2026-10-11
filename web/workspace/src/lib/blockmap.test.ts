// SPDX-License-Identifier: MIT
// The browser's half of the shared document cases (living documents, LD-F2): the server
// runs the same file through its parser (packages/orchestrator, test_document_parser.py).
import { describe, expect, it } from 'vitest';
import { blockMap } from './blockmap';
import documents from './documents.fixtures.json';
import dollars from './dollarmath.fixtures.json';
import { documentParser, parser } from './markdown';
import delimiters from './mathdelimiters.fixtures.json';

function parsed(markdown: string) {
  return blockMap(markdown, documentParser.parse(markdown, {}));
}

describe('blocks and formulas, as the server finds them', () => {
  for (const { name, markdown, blocks, math } of documents.cases) {
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
