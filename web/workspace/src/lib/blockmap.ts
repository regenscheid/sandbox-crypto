// SPDX-License-Identifier: MIT
/**
 * A document's blocks and math spans as the browser parses them, to match with the
 * server's block map (living documents, LD-9).
 *
 * This is the server's derivation (`documents/blocks.py`) in the browser: the same block
 * kinds, nesting and lines, and the same spans in the same order. The parser's block rules
 * are wrapped, as on the server, to record where each block ends and where its own text
 * begins on each line, which decides the trailing blank lines a block leaves out. The
 * shared cases in documents.fixtures.json pin the result on both sides.
 */
import type { MarkdownIt, StateBlock, Token } from 'markdown-it';
import { CONTAINER } from './documentrules';

export interface BrowserBlock {
  index: number;
  kind: string;
  parent: number | null;
  depth: number;
  /** `[first line, end line)`, without trailing blank lines. */
  lines: [number, number];
  /** The token that opened or made the block, for attaching an ID when rendering. */
  token: Token;
}

export interface BrowserSpan {
  block: number;
  kind: 'inline' | 'display';
  tex: string;
  token: Token;
}

export interface BrowserParse {
  blocks: BrowserBlock[];
  spans: BrowserSpan[];
}

const OPEN: Record<string, string> = {
  paragraph_open: 'paragraph',
  heading_open: 'heading',
  bullet_list_open: 'list',
  ordered_list_open: 'list',
  list_item_open: 'list_item',
  blockquote_open: 'quote',
  table_open: 'table',
  [`container_${CONTAINER}_open`]: 'container',
};
const LEAF: Record<string, string> = {
  front_matter: 'front_matter',
  fence: 'code',
  code_block: 'code',
  math_block: 'equation',
  math_block_label: 'equation',
  hr: 'thematic_break',
  html_block: 'html',
};
const INLINE_MATH: Record<string, 'inline' | 'display'> = {
  math_inline: 'inline',
  math_inline_double: 'display',
};

type BlockRule = (
  state: StateBlock,
  startLine: number,
  endLine: number,
  silent: boolean,
) => boolean;

/** Where a line's own text begins for a block at `indent`, as `getLines` finds it. */
function lineFirst(state: StateBlock, line: number, indent: number): number {
  const lineStart = state.bMarks[line]!;
  let first = lineStart;
  const end = state.eMarks[line]!;
  let lineIndent = 0;
  const src = state.src;
  while (first < end && lineIndent < indent) {
    const character = src[first];
    if (character === ' ' || character === '\t') {
      if (character === '\t') lineIndent += 4 - ((lineIndent + state.bsCount[line]!) % 4);
      else lineIndent += 1;
    } else if (first - lineStart < state.tShift[line]!) {
      lineIndent += 1;
    } else {
      break;
    }
    first += 1;
  }
  return first;
}

interface Place {
  lines: [number, number];
  firsts: number[];
}

function instrumented(rule: BlockRule): BlockRule {
  return (state, startLine, endLine, silent) => {
    if (silent) return rule(state, startLine, endLine, silent);
    const before = state.tokens.length;
    const found = rule(state, startLine, endLine, silent);
    if (!found || state.tokens.length === before) return found;
    state.tokens.slice(before).forEach((token, index) => {
      if (token.map === null || (token.meta as { place?: Place } | null)?.place) return;
      // The rule's first token is the block it made, which ends where it left the parse.
      const end = index === 0 ? state.line : token.map[1];
      const begin = token.map[0];
      const firsts: number[] = [];
      for (let line = begin; line < end; line += 1)
        firsts.push(lineFirst(state, line, state.blkIndent));
      token.meta = { ...(token.meta ?? {}), place: { lines: [begin, end], firsts } };
    });
    return found;
  };
}

/** Wrap every block rule, after all of them are registered. */
export function recordPlaces(md: MarkdownIt): void {
  // markdown-it keeps its rules in `__rules__`, with each rule's function in `fn`.
  const ruler = md.block.ruler as unknown as {
    __rules__: { fn: BlockRule }[];
    __cache__: unknown;
  };
  for (const rule of ruler.__rules__) rule.fn = instrumented(rule.fn);
  ruler.__cache__ = null;
}

/**
 * A `$$` block's TeX without the markers of the quotes and the indentation of the list
 * items it is in, which its content keeps (dollarmath.ts takes it from the text between
 * the delimiters, as the plugin does): what KaTeX is to render. Its lines after the first
 * each begin with their line's prefix, which the recorded places give.
 */
export function bareTex(src: string, token: Token): string {
  const place = (token.meta as { place?: Place } | null)?.place;
  if (place === undefined || token.markup !== '$$') return token.content;
  const lines = token.content.split('\n');
  for (let index = 1; index < lines.length; index += 1) {
    const first = place.firsts[index];
    if (first === undefined) break;
    const prefix = src.slice(src.lastIndexOf('\n', first - 1) + 1, first);
    if (prefix && lines[index]!.startsWith(prefix))
      lines[index] = lines[index]!.slice(prefix.length);
  }
  return lines.join('\n');
}

/** The blocks and spans of `src`'s tokens, as the server derives them. */
export function blockMap(src: string, tokens: Token[]): BrowserParse {
  const lineStarts = [0];
  for (let index = 0; index < src.length; index += 1)
    if (src[index] === '\n') lineStarts.push(index + 1);
  const lineEnd = (line: number) =>
    line + 1 < lineStarts.length ? lineStarts[line + 1]! - 1 : src.length;
  const blocks: BrowserBlock[] = [];
  const spans: BrowserSpan[] = [];
  const stack: number[] = [];

  const add = (token: Token, kind: string): number => {
    const place = (token.meta as { place?: Place } | null)?.place;
    if (!place) throw new Error(`the parser left a ${kind} without its place`);
    let [begin, end] = place.lines;
    // Trailing blank lines belong to no block (as the server's strip(" \t")).
    while (
      end - begin > 1 &&
      !/[^ \t]/.test(src.slice(place.firsts[end - begin - 1], lineEnd(end - 1)))
    ) {
      end -= 1;
    }
    const parent = stack.length ? stack[stack.length - 1]! : null;
    blocks.push({
      index: blocks.length,
      kind,
      parent,
      depth: stack.length,
      lines: [begin, end],
      token,
    });
    return blocks.length - 1;
  };

  const inline = (block: number, token: Token) => {
    for (const child of token.children ?? []) {
      const kind = INLINE_MATH[child.type];
      if (kind) spans.push({ block, kind, tex: child.content, token: child });
    }
  };

  let inTable = 0;
  for (const token of tokens) {
    if (inTable) {
      if (token.type === 'table_open') inTable += 1;
      else if (token.type === 'table_close') {
        inTable -= 1;
        if (!inTable) stack.pop();
      } else if (token.type === 'inline') inline(stack[stack.length - 1]!, token);
      continue;
    }
    if (token.nesting === 1) {
      const kind = OPEN[token.type] ?? 'other';
      stack.push(add(token, kind));
      if (kind === 'table') inTable = 1;
    } else if (token.nesting === -1) {
      stack.pop();
    } else if (token.type === 'inline') {
      if (stack.length) inline(stack[stack.length - 1]!, token);
    } else {
      const kind = LEAF[token.type] ?? 'other';
      const index = add(token, kind);
      if (kind === 'equation')
        spans.push({ block: index, kind: 'display', tex: token.content, token });
    }
  }
  return { blocks, spans };
}
