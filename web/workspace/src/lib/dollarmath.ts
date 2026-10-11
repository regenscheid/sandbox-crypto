// SPDX-License-Identifier: MIT
/**
 * Dollar math for markdown-it: `$…$` inline, `$$…$$` displayed, and `$$…$$ (label)`.
 *
 * The rules are a port of the `dollarmath` plugin of mdit-py-plugins v0.6.1
 * (executablebooks/mdit-py-plugins at da70e05, MIT License, Copyright (c) 2020
 * ExecutableBookProject), which the server is to use with markdown-it-py (D65). Both sides
 * must find the same math spans, since anchors address them, so the port keeps that
 * plugin's behavior, quirks included:
 *
 * - whitespace is Python's (`str.isspace()`), for `strip()` and for `\s`;
 * - lengths are counted, and positions moved, in code points;
 * - a read before the start of the text wraps to its end, as Python's index -1 does;
 * - the text is as markdown-it-py gives it, stripped of all Python whitespace, where
 *   markdown-it trims only ASCII whitespace.
 *
 * One rule is this project's own, and the server's parser must apply it too: a `$$`
 * block looks for its end in at most `MAX_BLOCK_LINES` lines. The plugin looks to the
 * end of the text from every `$$` that does not close, which makes some texts take
 * quadratic time; with the limit, such a `$$` is ordinary text.
 *
 * The plugin's `allow_digits` is always on here. Off, it would compare characters with
 * Python's `isdigit()`, which has no exact JavaScript twin, and its check before an
 * opening `$` that starts the text reads the text's last character instead.
 *
 * The tokens are `math_inline`, `math_inline_double` (display math within a paragraph),
 * `math_block` and `math_block_label`; rendering them is left to the caller.
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
import type { MarkdownIt, StateBlock, StateInline } from 'markdown-it';

export interface DollarMathOptions {
  /** `$$a=1$$ (eq1)` labels a displayed equation (`allow_labels`). */
  allowLabels: boolean;
  /** Whitespace may follow an opening `$` and precede a closing one (`allow_space`). */
  allowSpace: boolean;
  /** Blank lines may appear inside `$$…$$` (`allow_blank_lines`). */
  allowBlankLines: boolean;
  /** `$$…$$` within a paragraph is displayed math (`double_inline`). */
  doubleInline: boolean;
}

/** The lines after a `$$` that its block may end in. */
export const MAX_BLOCK_LINES = 200;

/** Python's `str.isspace()` characters, all single UTF-16 units. */
const PY_SPACE =
  '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PY_SPACES = new RegExp(`[${PY_SPACE}]+`, 'gu');
/** A displayed equation's end with its label, `$$ (label)`, matched on the reversed line. */
const EQNO_REVERSED = new RegExp(`^[${PY_SPACE}]*\\)([^)$\\r\\n]+?)\\([${PY_SPACE}]*\\$\\$`, 'u');

function isPySpace(code: number): boolean {
  return (
    (code >= 0x09 && code <= 0x0d) ||
    (code >= 0x1c && code <= 0x20) ||
    code === 0x85 ||
    code === 0xa0 ||
    code === 0x1680 ||
    (code >= 0x2000 && code <= 0x200a) ||
    code === 0x2028 ||
    code === 0x2029 ||
    code === 0x202f ||
    code === 0x205f ||
    code === 0x3000
  );
}

/** The bounds of `text[from, to)` without Python whitespace at either end. */
function pyTrim(text: string, from: number, to: number): [number, number] {
  let start = from;
  let end = to;
  while (start < end && isPySpace(text.charCodeAt(start))) start += 1;
  while (end > start && isPySpace(text.charCodeAt(end - 1))) end -= 1;
  return [start, end];
}

function codePoints(text: string): number {
  return Array.from(text).length;
}

function reversed(text: string): string {
  return Array.from(text).reverse().join('');
}

/** The position `count` code points before `from`. */
function backCodePoints(text: string, from: number, count: number): number {
  let at = from;
  for (let step = 0; step < count; step += 1) {
    const low = text.charCodeAt(at - 1);
    const high = text.charCodeAt(at - 2);
    at -= low >= 0xdc00 && low <= 0xdfff && high >= 0xd800 && high <= 0xdbff ? 2 : 1;
  }
  return at;
}

/** The inline text's ends as markdown-it-py has them, kept for the text being parsed. */
let ends: { text: string; first: number; end: number } | null = null;

function pythonEnds(text: string): { first: number; end: number } {
  if (ends?.text !== text) {
    const [first, end] = pyTrim(text, 0, text.length);
    ends = { text, first, end };
  }
  return ends;
}

/**
 * Whether an odd number of backslashes precede `position`. Positions count from the
 * first character markdown-it-py keeps, and the read before it, which Python makes at
 * index -1, takes the last character it keeps.
 */
function isEscaped(src: string, position: number): boolean {
  const { first, end } = pythonEnds(src);
  let backslashes = 0;
  let back = position;
  while (back >= first) {
    back -= 1;
    if ((back >= first ? src[back] : src[end - 1]) === '\\') {
      backslashes += 1;
    } else {
      break;
    }
  }
  return backslashes % 2 !== 0;
}

function mathInline(md: MarkdownIt, options: DollarMathOptions) {
  const isWhiteSpace = md.utils.isWhiteSpace;
  return (state: StateInline, silent: boolean): boolean => {
    const src = state.src;
    if (src[state.pos] !== '$') return false;
    if (!options.allowSpace) {
      const next = src.charCodeAt(state.pos + 1);
      if (Number.isNaN(next) || isWhiteSpace(next)) return false;
    }
    if (isEscaped(src, state.pos)) return false;
    if (state.pos + 1 >= src.length) return false;
    const isDouble = options.doubleInline && src[state.pos + 1] === '$';

    let position = state.pos + 1 + (isDouble ? 1 : 0);
    let end: number;
    for (;;) {
      end = src.indexOf('$', position);
      if (end === -1) return false;
      if (isEscaped(src, end)) {
        position = end + 1;
        continue;
      }
      if (isDouble) {
        if (end + 1 >= src.length) return false;
        if (src[end + 1] !== '$') {
          position = end + 1;
          continue;
        }
        end += 1;
      }
      break;
    }
    if (!options.allowSpace && isWhiteSpace(src.charCodeAt(end - 1))) return false;

    const text = isDouble ? src.slice(state.pos + 2, end - 1) : src.slice(state.pos + 1, end);
    if (!text) return false;
    if (!silent) {
      const token = state.push(isDouble ? 'math_inline_double' : 'math_inline', 'math', 0);
      token.content = text;
      token.markup = isDouble ? '$$' : '$';
    }
    state.pos = end + 1;
    return true;
  };
}

/** How a line ends a `$$` block: where its math ends, and its label, or null. */
function blockEnd(
  src: string,
  lineStart: number,
  lineEnd: number,
  allowLabels: boolean,
): { end: number; label: string | null } | null {
  const [start, end] = pyTrim(src, lineStart, lineEnd);
  if (end - start >= 2 && src[end - 1] === '$' && src[end - 2] === '$') {
    // The plugin steps back over the "$$" and as many characters as strip() removed,
    // from both ends of the line.
    const stripped = start - lineStart + (lineEnd - end);
    return { end: backCodePoints(src, lineEnd, 2 + stripped), label: null };
  }
  // The label's pattern ends the line with ")", so most lines need no regex.
  if (allowLabels && end > start && src[end - 1] === ')') {
    const match = EQNO_REVERSED.exec(reversed(src.slice(lineStart, lineEnd)));
    if (match) return { end: lineEnd - match[0].length, label: reversed(match[1]!) };
  }
  return null;
}

function mathBlock(options: DollarMathOptions) {
  return (state: StateBlock, startLine: number, endLine: number, silent: boolean): boolean => {
    // An indented code block, as markdown-it-py's is_code_block (the code rule is on).
    if (state.sCount[startLine]! - state.blkIndent >= 4) return false;
    const src = state.src;
    const startPos = state.bMarks[startLine]! + state.tShift[startLine]!;
    const lineEnd = state.eMarks[startLine]!;
    if (startPos + 2 > lineEnd) return false;
    if (src[startPos] !== '$' || src[startPos + 1] !== '$') return false;

    let found: { end: number; label: string | null } | null = null;
    let nextLine = startLine;
    // The end on the same line, when the line has more than "$$" and one character.
    const [start, end] = pyTrim(src, startPos, lineEnd);
    if (codePoints(src.slice(start, end)) > 3) {
      found = blockEnd(src, startPos, lineEnd, options.allowLabels);
    }
    // The end on a later line.
    while (found === null) {
      nextLine += 1;
      if (nextLine >= endLine || nextLine - startLine > MAX_BLOCK_LINES) break;
      const lineStart = state.bMarks[nextLine]! + state.tShift[nextLine]!;
      const nextEnd = state.eMarks[nextLine]!;
      found = blockEnd(src, lineStart, nextEnd, options.allowLabels);
      if (found === null && !options.allowBlankLines) {
        const [trimmedStart, trimmedEnd] = pyTrim(src, lineStart, nextEnd);
        if (trimmedStart === trimmedEnd) break; // blank lines are not allowed within $$
      }
    }
    if (found === null) return false;
    if (silent) return true;

    state.line = nextLine + 1;
    const token = state.push(found.label ? 'math_block_label' : 'math_block', 'math', 0);
    token.block = true;
    token.content = src.slice(startPos + 2, found.end);
    token.markup = '$$';
    token.map = [startLine, state.line];
    if (found.label) token.info = found.label.replace(PY_SPACES, '-');
    return true;
  };
}

/** Add the dollar math rules, before the escape and fence rules as the original does. */
export function dollarMath(md: MarkdownIt, options: DollarMathOptions): void {
  md.inline.ruler.before('escape', 'math_inline', mathInline(md, options));
  md.block.ruler.before('fence', 'math_block', mathBlock(options));
}
