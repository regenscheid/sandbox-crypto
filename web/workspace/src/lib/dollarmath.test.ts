// SPDX-License-Identifier: MIT
// Where math begins and ends: the shared cases, which mdit-py-plugins 0.6.1 also passes.
import { describe, expect, it } from 'vitest';
import type { Token } from 'markdown-it';
import fixtures from './dollarmath.fixtures.json';
import { parser } from './markdown';

const MATH = new Set(['math_inline', 'math_inline_double', 'math_block', 'math_block_label']);

function spans(markdown: string): [string, string, string][] {
  const found: [string, string, string][] = [];
  const visit = (tokens: Token[]) => {
    for (const token of tokens) {
      if (MATH.has(token.type)) found.push([token.type, token.content, token.info]);
      if (token.children) visit(token.children);
    }
  };
  visit(parser.parse(markdown, {}));
  return found;
}

describe('math spans', () => {
  for (const { name, markdown, math } of fixtures.cases) {
    it(name, () => {
      expect(spans(markdown)).toEqual(math);
    });
  }
});
