// SPDX-License-Identifier: MIT
/**
 * The server's block and math-span IDs on the browser's parse of the same revision
 * (living documents, LD-9).
 *
 * A browser block takes the ID of the server's block of the same kind, depth and lines,
 * in document order; within a block that has its ID, the formulas take the server's span
 * IDs in order when both sides read the same kinds and TeX. Where the two parsers
 * disagree, as they do on a few tables whose lines start or end with rare Unicode
 * whitespace (living documents as built), that part gets no ID, and the disagreement is
 * reported: the view never guesses which block is which.
 */
import type { Token } from 'markdown-it';
import type { BrowserParse } from './blockmap';

/** A formula of the server's block map: its TeX is null when the server could not
 * parse the revision as its block map was made (another parser's version). */
export interface ServerSpan {
  id: string;
  kind: 'inline' | 'display';
  tex: string | null;
}

export interface ServerBlock {
  id: string;
  kind: string;
  depth: number;
  /** `[first line, end line)`, as the browser's blocks have them. */
  lines: [number, number];
  /** `kept`, `moved`, `changed` or `added`, against the revision before. */
  status: string;
  math: ServerSpan[];
}

export interface Mismatch {
  /** The lines where the parsers disagree, counted from 0, the end excluded. */
  lines: [number, number];
  message: string;
}

export interface Attached {
  /** Each block token's server block. */
  blocks: Map<Token, ServerBlock>;
  /** Each math token's span ID. */
  spans: Map<Token, string>;
  /** Block tokens the server's map has no block for. */
  unmatched: Set<Token>;
  mismatches: Mismatch[];
}

function keyOf(kind: string, depth: number, lines: readonly [number, number]): string {
  return `${kind}:${depth}:${lines[0]}:${lines[1]}`;
}

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

export function attachIds(browser: BrowserParse, server: readonly ServerBlock[]): Attached {
  const waiting = new Map<string, ServerBlock[]>();
  for (const block of server) {
    const key = keyOf(block.kind, block.depth, block.lines);
    const queue = waiting.get(key);
    if (queue === undefined) waiting.set(key, [block]);
    else queue.push(block);
  }
  const attached: Attached = {
    blocks: new Map(),
    spans: new Map(),
    unmatched: new Set(),
    mismatches: [],
  };
  const matched = new Map<number, ServerBlock>();
  for (const block of browser.blocks) {
    const found = waiting.get(keyOf(block.kind, block.depth, block.lines))?.shift();
    if (found === undefined) {
      attached.unmatched.add(block.token);
      attached.mismatches.push({
        lines: block.lines,
        message: `the browser reads a ${block.kind.replace('_', ' ')} the server does not`,
      });
      continue;
    }
    matched.set(block.index, found);
    attached.blocks.set(block.token, found);
  }
  for (const left of waiting.values()) {
    for (const block of left) {
      attached.mismatches.push({
        lines: block.lines,
        message: `the server reads a ${block.kind.replace('_', ' ')} (${block.id}) the browser does not`,
      });
    }
  }
  const spans = new Map<number, BrowserParse['spans']>();
  for (const span of browser.spans) {
    const list = spans.get(span.block);
    if (list === undefined) spans.set(span.block, [span]);
    else list.push(span);
  }
  for (const [index, found] of matched) {
    const mine = spans.get(index) ?? [];
    const same =
      mine.length === found.math.length &&
      mine.every((span, at) => {
        const theirs = found.math[at]!;
        return span.kind === theirs.kind && (theirs.tex === null || span.tex === theirs.tex);
      });
    if (!same) {
      const count = found.math.length;
      attached.mismatches.push({
        lines: browser.blocks[index]!.lines,
        message:
          `${found.id} has ${count} ${plural(count, 'formula', 'formulas')} on the server, ` +
          `which the browser reads otherwise`,
      });
      continue;
    }
    mine.forEach((span, at) => attached.spans.set(span.token, found.math[at]!.id));
  }
  attached.mismatches.sort((a, b) => a.lines[0] - b.lines[0] || a.lines[1] - b.lines[1]);
  return attached;
}
