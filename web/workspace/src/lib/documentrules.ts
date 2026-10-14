// SPDX-License-Identifier: MIT
/**
 * The rules living documents add to the workspace's Markdown (LD-4): YAML front matter and
 * theorem-like containers, `::: lemma (label)` to `:::`.
 *
 * Both are ports of mdit-py-plugins 0.6.1 (executablebooks/mdit-py-plugins, MIT License,
 * Copyright (c) 2020 ExecutableBookProject), which the server parses with: `front_matter`,
 * itself a port of markdown-it-front-matter 0.2.1, and `container`, a port of
 * markdown-it-container 3.0.0. They are ported from the Python, not taken from the npm
 * packages, so that both sides find the same blocks; the shared cases in
 * documents.fixtures.json pin them.
 *
 * The rules record where each block they make ends (`meta.endLine`), since a container's
 * own map leaves out its closing marker.
 *
 * The original's license:
 *
 * MIT License
 *
 * Copyright (c) 2020 ExecutableBookProject
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
import type { MarkdownIt, StateBlock } from 'markdown-it';

/** The kinds of theorem-like containers (LD-4); the server's parser.THEOREM_KINDS. */
export const THEOREM_KINDS: ReadonlySet<string> = new Set([
  'theorem',
  'lemma',
  'proposition',
  'corollary',
  'definition',
  'claim',
  'assumption',
  'conjecture',
  'proof',
  'remark',
  'example',
]);

/** The container rule's name, as the server's: tokens `container_theorem_like_open`. */
export const CONTAINER = 'theorem_like';

function isStrSpace(character: string | undefined): boolean {
  return character === ' ' || character === '\t';
}

/** markdown-it-py's `skipSpaces`: spaces and tabs only. */
function skipSpaces(state: StateBlock, from: number): number {
  let position = from;
  while (position < state.src.length && isStrSpace(state.src[position])) position += 1;
  return position;
}

/** mdit-py-plugins' `is_code_block`, with the code rule on. */
function isCodeBlock(state: StateBlock, line: number): boolean {
  return state.sCount[line]! - state.blkIndent >= 4;
}

function frontMatter(
  state: StateBlock,
  startLine: number,
  endLine: number,
  silent: boolean,
): boolean {
  const marker = '-';
  const minimum = 3;
  let autoClosed = false;
  let start = state.bMarks[startLine]! + state.tShift[startLine]!;
  let maximum = state.eMarks[startLine]!;
  const length = state.src.length;
  // At the very start of the text alone, as the server has it: never a list item's or quote's line.
  if (startLine !== 0 || state.bMarks[0]! + state.tShift[0]! !== 0 || state.blkIndent !== 0)
    return false;
  if (state.src[0] !== marker) return false;
  let position = start + 1;
  while (position <= maximum && position < length) {
    if (state.src[position] !== marker) break;
    position += 1;
  }
  const markerCount = position - start;
  if (markerCount < minimum) return false;
  if (silent) return true;
  let nextLine = startLine;
  for (;;) {
    nextLine += 1;
    if (nextLine >= endLine) return false; // unclosed: not front matter
    if (state.src.slice(start, maximum) === '...') break;
    start = state.bMarks[nextLine]! + state.tShift[nextLine]!;
    maximum = state.eMarks[nextLine]!;
    if (start < maximum && state.sCount[nextLine]! < state.blkIndent) break;
    if (state.src[start] !== marker) continue;
    if (isCodeBlock(state, nextLine)) continue;
    position = start + 1;
    while (position < maximum) {
      if (state.src[position] !== marker) break;
      position += 1;
    }
    if (position - start < markerCount) continue;
    position = skipSpaces(state, position);
    if (position < maximum) continue;
    autoClosed = true;
    break;
  }
  const oldParent = state.parentType;
  const oldLineMax = state.lineMax;
  state.parentType = 'container' as typeof state.parentType;
  state.lineMax = nextLine;
  const token = state.push('front_matter', '', 0);
  token.hidden = true;
  token.markup = marker.repeat(minimum);
  token.content = state.src.slice(state.bMarks[startLine + 1]!, state.eMarks[nextLine - 1]!);
  token.block = true;
  state.parentType = oldParent;
  state.lineMax = oldLineMax;
  state.line = nextLine + (autoClosed ? 1 : 0);
  token.map = [startLine, state.line];
  token.meta = { endLine: state.line };
  return true;
}

/** Python's whitespace (`str.isspace()`), all single UTF-16 units, at either end. */
const PY_SPACE_ENDS = new RegExp(
  String.raw`^[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$`,
  'g',
);

/** The container's parameters name its kind first: `::: lemma (fermat)`. */
function theoremLike(params: string): boolean {
  // Python's str.strip() and split(" ", 2): the first word, split on spaces only.
  const first = params.replace(PY_SPACE_ENDS, '').split(' ', 1)[0] ?? '';
  return THEOREM_KINDS.has(first);
}

function container(
  state: StateBlock,
  startLine: number,
  endLine: number,
  silent: boolean,
): boolean {
  const markerString = ':';
  const markerChar = ':';
  const markerLength = 1;
  const minimum = 3;
  if (isCodeBlock(state, startLine)) return false;
  let autoClosed = false;
  let start = state.bMarks[startLine]! + state.tShift[startLine]!;
  let maximum = state.eMarks[startLine]!;
  if (state.src[start] !== markerChar) return false;
  let position = start + 1;
  while (position <= maximum) {
    const character = state.src[position];
    if (character === undefined) break;
    if (markerString[(position - start) % markerLength] !== character) break;
    position += 1;
  }
  const markerCount = Math.floor((position - start) / markerLength);
  if (markerCount < minimum) return false;
  position -= (position - start) % markerLength;
  const markup = state.src.slice(start, position);
  const params = state.src.slice(position, maximum);
  if (!theoremLike(params)) return false;
  if (silent) return true;
  let nextLine = startLine;
  for (;;) {
    nextLine += 1;
    if (nextLine >= endLine) break;
    start = state.bMarks[nextLine]! + state.tShift[nextLine]!;
    maximum = state.eMarks[nextLine]!;
    if (start < maximum && state.sCount[nextLine]! < state.blkIndent) break;
    if (markerChar !== state.src[start]) continue;
    if (isCodeBlock(state, nextLine)) continue;
    position = start + 1;
    while (position <= maximum) {
      const character = state.src[position];
      if (character === undefined) break;
      if (markerString[(position - start) % markerLength] !== character) break;
      position += 1;
    }
    if (Math.floor((position - start) / markerLength) < markerCount) continue;
    position -= (position - start) % markerLength;
    position = skipSpaces(state, position);
    if (position < maximum) continue;
    autoClosed = true;
    break;
  }
  const oldParent = state.parentType;
  const oldLineMax = state.lineMax;
  state.parentType = 'container' as typeof state.parentType;
  state.lineMax = nextLine;
  const open = state.push(`container_${CONTAINER}_open`, 'div', 1);
  open.markup = markup;
  open.block = true;
  open.info = params;
  open.map = [startLine, nextLine];
  state.md.block.tokenize(state, startLine + 1, nextLine);
  const close = state.push(`container_${CONTAINER}_close`, 'div', -1);
  close.markup = state.src.slice(start, position);
  close.block = true;
  state.parentType = oldParent;
  state.lineMax = oldLineMax;
  state.line = nextLine + (autoClosed ? 1 : 0);
  open.meta = { endLine: state.line };
  return true;
}

/** Front matter before tables, as the server registers it; then, after dollar math and
 * LaTeX's delimiters, containers before fences. */
export function frontMatterRule(md: MarkdownIt): void {
  md.block.ruler.before('table', 'front_matter', frontMatter, {
    alt: ['paragraph', 'reference', 'blockquote', 'list'],
  });
}

export function containerRule(md: MarkdownIt): void {
  md.block.ruler.before('fence', `container_${CONTAINER}`, container, {
    alt: ['paragraph', 'reference', 'blockquote', 'list'],
  });
}
