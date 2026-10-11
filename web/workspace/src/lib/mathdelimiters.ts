// SPDX-License-Identifier: MIT
/**
 * LaTeX's own math delimiters in Markdown: `\(…\)` inline, `\[…\]` displayed, and display
 * environments written without delimiters (`\begin{align}…\end{align}` and the like), as
 * agents often write them. They give the tokens dollar math gives (dollarmath.ts), so the
 * renderer shows both alike, and `$…$` and `$$…$$` keep their own rules unchanged.
 *
 * Adapted from cscience-crypto's src/web/cscience-math.js (its `normalizeMathDelimiters`),
 * contributed under the MIT License by its author. The original rewrote the text into
 * dollar math before parsing it. Here markdown-it reads these delimiters as it reads the
 * rest of the text, which a rewrite cannot match: code blocks stay as written wherever
 * they are (indented, or fenced inside a list or a quote), a displayed formula stays in
 * its list item or quote, and no delimiter pairs with a stray dollar sign. The rules
 * follow dollar math's:
 *
 * - a delimiter preceded by an odd number of backslashes is escaped (`\\[` is a line
 *   break and a bracket), counted as dollar math counts them, quirks included;
 * - inline, a formula ends at the first closing delimiter in its paragraph, and a formula
 *   of only whitespace, Python's as for dollar math, is not math. Like `$…$`, a formula
 *   that opens before a code span runs over the span's opening backtick;
 * - a block begins a line and its closing delimiter ends one, within `MAX_BLOCK_LINES`
 *   lines and without a blank line between.
 *
 * Unlike `$$`, which follows mdit-py-plugins, a `\[` or `\begin{…}` line may begin a block
 * after a line of a paragraph, as a code fence may: it can only be math, and agents seldom
 * leave a blank line before it. As a fence does, a block stops at a line outside its list
 * item or quote (a line less indented, a lazy line), where `$$` reads on. A block whose
 * closing delimiter is followed by more text on its line is read within its paragraph
 * instead. A block's content leaves out the markers of the quotes and the indentation of
 * the list items it is in.
 *
 * Markdown writers escape brackets (`\[1\]`, `\[sic\]`, `- \[x\] done`, Pandoc's
 * `\[@smith2020\]` and `\[^1\]`), so within a paragraph, `\[…\]` is displayed math only
 * when its content holds a character that math uses and bracketed prose seldom does
 * (`TEX_CHARACTERS`), and is not a footnote's reference; otherwise it stays text, as the
 * escapes make it.
 *
 * The tokens carry the delimiters as written: `markup` opens and `meta.close` closes.
 * A display environment is its own delimiter, so its content is the whole environment.
 */
import type { MarkdownIt, StateBlock, StateInline } from 'markdown-it';
import { isBlank, isEscaped as isEscapedLikeDollars, MAX_BLOCK_LINES } from './dollarmath';

/** Display environments that are math by themselves (the original's list). */
export const DISPLAY_ENVIRONMENTS: ReadonlySet<string> = new Set([
  'equation', 'equation*', 'align', 'align*', 'alignat', 'alignat*', 'gather', 'gather*',
  'multline', 'multline*', 'flalign', 'flalign*', 'eqnarray', 'eqnarray*', 'displaymath',
]);

/** A math token's closing delimiter, when it is not its opening one. */
export interface MathMeta {
  close: string;
}

const BACKSLASH = 0x5c;
const ENVIRONMENT = /\\begin\{([A-Za-z*]+)\}/y;
/**
 * Characters that math uses and prose between brackets seldom does: within a paragraph,
 * `\[…\]` is math only when its content holds one. Found anywhere in it (Python's
 * `re.search`).
 */
const TEX_CHARACTERS = /[\\^_={}+<>]/;
/**
 * A footnote's reference as Pandoc writes it with its brackets escaped, `\[^1\]`: text.
 * Matched against the whole content, as JavaScript's `^…$` matches without the `m` flag. A
 * port to Python must use `re.fullmatch` (or `\A…\Z`): Python's `$` also matches before a
 * final line feed, so `re.match` with `^…$` would take `^1` and a line break for one too.
 */
const FOOTNOTE = /^\^[A-Za-z0-9-]+$/;

/** Whether `\[…\]` around `content`, within a paragraph, is escaped brackets: text. */
function escapedBrackets(content: string): boolean {
  return !TEX_CHARACTERS.test(content) || FOOTNOTE.test(content);
}

interface Opener {
  /** The opening delimiter as the token keeps it; empty for an environment. */
  markup: string;
  close: string;
  /** Where the formula's body begins. */
  body: number;
  display: boolean;
  /** Whether the delimiters are part of the formula, as an environment's are. */
  kept: boolean;
}

/** The delimiter that opens at `start`, if one does. `displaymath` is only a delimiter. */
function openerAt(src: string, start: number): Opener | null {
  const next = src[start + 1];
  if (next === '(') {
    return { markup: '\\(', close: '\\)', body: start + 2, display: false, kept: false };
  }
  if (next === '[') {
    return { markup: '\\[', close: '\\]', body: start + 2, display: true, kept: false };
  }
  if (next !== 'b') return null;
  ENVIRONMENT.lastIndex = start;
  const match = ENVIRONMENT.exec(src);
  const name = match?.[1];
  if (match === null || name === undefined || !DISPLAY_ENVIRONMENTS.has(name)) return null;
  const close = `\\end{${name}}`;
  const body = start + match[0].length;
  if (name === 'displaymath') return { markup: match[0], close, body, display: true, kept: false };
  return { markup: '', close, body, display: true, kept: true };
}

/**
 * Whether an odd number of backslashes precede `position`. For closing delimiters, whose
 * backslashes never reach the start of the text, this is the dollar rules' count.
 */
function isEscaped(src: string, position: number): boolean {
  let count = 0;
  for (let at = position - 1; at >= 0 && src.charCodeAt(at) === BACKSLASH; at -= 1) count += 1;
  return count % 2 === 1;
}

/** Where each closing delimiter that is not escaped begins, for the text being parsed. */
let found: { src: string; closers: Map<string, number[]> } | null = null;

function closers(src: string, close: string): number[] {
  if (found?.src !== src) found = { src, closers: new Map() };
  let positions = found.closers.get(close);
  if (positions === undefined) {
    positions = [];
    for (let at = src.indexOf(close); at !== -1; at = src.indexOf(close, at + 1)) {
      if (!isEscaped(src, at)) positions.push(at);
    }
    found.closers.set(close, positions);
  }
  return positions;
}

/** The first of the sorted `positions` at or after `from`. */
function firstFrom(positions: number[], from: number): number | undefined {
  let low = 0;
  let high = positions.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (positions[middle]! < from) low = middle + 1;
    else high = middle;
  }
  return positions[low];
}

function mathInline(state: StateInline, silent: boolean): boolean {
  const src = state.src;
  const start = state.pos;
  if (src.charCodeAt(start) !== BACKSLASH) return false;
  // The opener first: counting the backslashes before every backslash of a long run of
  // them would take time quadratic in its length.
  const opener = openerAt(src, start);
  if (opener === null || isEscapedLikeDollars(src, start)) return false;
  const close = firstFrom(closers(src, opener.close), opener.body);
  if (close === undefined) return false;
  const end = close + opener.close.length;
  if (end > state.posMax) return false;
  const content = opener.kept ? src.slice(start, end) : src.slice(opener.body, close);
  if (isBlank(content, 0, content.length)) return false;
  if (opener.markup === '\\[' && escapedBrackets(content)) return false;
  if (!silent) {
    const token = state.push(opener.display ? 'math_inline_double' : 'math_inline', 'math', 0);
    token.content = content;
    token.markup = opener.markup;
    token.meta = { close: opener.kept ? '' : opener.close } satisfies MathMeta;
  }
  state.pos = end;
  return true;
}

function mathBlock(
  state: StateBlock,
  startLine: number,
  endLine: number,
  silent: boolean,
): boolean {
  // An indented code block, as for `$$`.
  if (state.sCount[startLine]! - state.blkIndent >= 4) return false;
  const src = state.src;
  // Only indentation or a container's marker precedes this, so it is never escaped.
  const start = state.bMarks[startLine]! + state.tShift[startLine]!;
  if (src.charCodeAt(start) !== BACKSLASH) return false;
  const opener = openerAt(src, start);
  if (opener === null || !opener.display) return false;
  const close = firstFrom(closers(src, opener.close), opener.body);
  if (close === undefined) return false;
  const end = close + opener.close.length;
  if (end > state.eMarks[Math.min(endLine - 1, startLine + MAX_BLOCK_LINES)]!) return false;

  // The closing delimiter's line: within the limit, after no blank line, and inside the
  // list item or quote, as a fence's lines are (a lazy line's indent is negative).
  let line = startLine;
  while (end > state.eMarks[line]!) {
    line += 1;
    if (line >= endLine || line - startLine > MAX_BLOCK_LINES) return false;
    if (state.sCount[line]! < state.blkIndent) return false;
    if (isBlank(src, state.bMarks[line]! + state.tShift[line]!, state.eMarks[line]!)) return false;
  }
  // The line must end with it; more text after it is read within a paragraph.
  if (!isBlank(src, end, state.eMarks[line]!)) return false;

  const [from, to] = opener.kept ? [start, end] : [opener.body, close];
  let content: string;
  if (line === startLine) {
    content = src.slice(from, to);
  } else {
    const first = src.slice(from, state.eMarks[startLine]!);
    const middle = state.getLines(startLine + 1, line, state.blkIndent, true);
    const last = src.slice(state.bMarks[line]! + state.tShift[line]!, to);
    content = `${first}\n${middle}${last}`;
  }
  if (isBlank(content, 0, content.length)) return false;
  if (silent) return true;

  state.line = line + 1;
  const token = state.push('math_block', 'math', 0);
  token.block = true;
  token.content = content;
  token.markup = opener.markup;
  token.meta = { close: opener.kept ? '' : opener.close } satisfies MathMeta;
  token.map = [startLine, state.line];
  return true;
}

/** Add the rules: inline before the escapes, and blocks before fences, as dollar math's. */
export function backslashMath(md: MarkdownIt): void {
  md.inline.ruler.before('escape', 'math_inline_backslash', mathInline);
  md.block.ruler.before('fence', 'math_block_backslash', mathBlock, {
    alt: ['paragraph', 'reference', 'blockquote', 'list'],
  });
}
