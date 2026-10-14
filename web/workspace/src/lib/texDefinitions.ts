// SPDX-License-Identifier: MIT
/**
 * Which commands a formula names, as KaTeX's lexer reads them, so that a formula or a
 * repair that defines a macro or needs trust is refused (`definesMacros`, `safeRepair`).
 * Security-relevant and pure: the server-side math checker uses it too.
 */

/**
 * Commands that define macros. A formula with one is not rendered, nor is a document's
 * macro that has one (macros.ts): macros come from a document's front matter and the
 * project (living documents, LD-28), and a definition can make KaTeX's work grow far
 * beyond the formula's length within its limit on expansions.
 */
const DEFINITIONS: ReadonlySet<string> = new Set([
  'def',
  'gdef',
  'edef',
  'xdef',
  'let',
  'futurelet',
  'global',
  'long',
  'outer',
  'newcommand',
  'renewcommand',
  'providecommand',
  'DeclareRobustCommand',
  'DeclareMathOperator',
  'newenvironment',
  'renewenvironment',
]);

/** Commands that KaTeX runs only when trusted; the renderer never trusts. */
const TRUSTED_ONLY: ReadonlySet<string> = new Set([
  'href',
  'url',
  'includegraphics',
  'htmlClass',
  'htmlId',
  'htmlStyle',
  'htmlData',
]);

/** Commands whose link KaTeX reads with `%` as a character rather than a comment. */
const LINK_ARGUMENTS: readonly string[] = ['href', 'url', 'includegraphics'];

// KaTeX's token pattern, from KaTeX 0.18.9's src/Lexer.ts (MIT License, Copyright (c)
// 2013-2020 Khan Academy and other contributors), with its two `\verb` alternatives left
// out: `verbEnd` finds the same `\verb` tokens through an index, in linear time, where the
// pattern's lazy search takes time quadratic in the number of openers that never close.
// Its groups: 1 whitespace, 2 a backslash before whitespace, 3 any other token, 4 the name
// of a control word. Sticky, as KaTeX's lexer only takes a match where it stands.
const SPACE = '[ \r\n\t]';
const CONTROL_WORD = '\\\\[a-zA-Z@]+';
const CONTROL_SYMBOL = '\\\\[^\uD800-\uDFFF]';
const CONTROL_WORD_WHITESPACE = `(${CONTROL_WORD})${SPACE}*`;
const CONTROL_SPACE = '\\\\(\n|[ \r\t]+\n?)[ \r\t]*';
const COMBINING_MARKS = '[\u0300-\u036F]';
const TOKEN = new RegExp(
  `(${SPACE}+)|` +
    `${CONTROL_SPACE}|` +
    `([!-\\[\\]-\u2027\u202A-\uD7FF\uF900-\uFFFF]${COMBINING_MARKS}*` +
    `|[\uD800-\uDBFF][\uDC00-\uDFFF]${COMBINING_MARKS}*` +
    `|${CONTROL_WORD_WHITESPACE}` +
    `|${CONTROL_SYMBOL})`,
  'y',
);

/** Whether `code` ends a line as JavaScript's `.` reads lines, where a `\verb` body ends. */
function endsLine(code: number): boolean {
  return code === 0x0a || code === 0x0d || code === 0x2028 || code === 0x2029;
}

/**
 * For each position of `tex`, where its character next occurs (`next`), and where the
 * next line ends (`lineEnd`, one longer), or -1: so that each `\verb` finds its closing
 * delimiter at once, and openers that never close take linear time, not quadratic.
 */
interface VerbIndex {
  next: Int32Array;
  lineEnd: Int32Array;
}

function verbIndex(tex: string): VerbIndex {
  const next = new Int32Array(tex.length);
  const lineEnd = new Int32Array(tex.length + 1);
  const seen = new Map<number, number>();
  lineEnd[tex.length] = -1;
  for (let index = tex.length - 1; index >= 0; index -= 1) {
    const code = tex.charCodeAt(index);
    next[index] = seen.get(code) ?? -1;
    seen.set(code, index);
    lineEnd[index] = endsLine(code) ? index : lineEnd[index + 1]!;
  }
  return { next, lineEnd };
}

/**
 * Where the `\verb|…|` or `\verb*|…|` that starts at `index` ends, or -1 when none starts
 * there: KaTeX's two `\verb` alternatives. After `\verb`, any character but `*` and a
 * letter is the delimiter (after `\verb*`, any at all), and the body runs to the same
 * character on the same line. Without that, `\verb` is a command, which KaTeX rejects.
 */
function verbEnd(tex: string, index: number, verbs: VerbIndex): number {
  if (!tex.startsWith('\\verb', index)) return -1;
  let at = index + 5;
  const starred = tex.charCodeAt(at) === 0x2a;
  if (starred) at += 1;
  if (at >= tex.length) return -1;
  if (!starred && (tex.charCodeAt(at) === 0x2a || /[A-Za-z]/.test(tex[at]!))) return -1;
  // The delimiter's next occurrence, if no line ends before it (it may end the line itself).
  const close = verbs.next[at]!;
  const lineEnd = verbs.lineEnd[at + 1]!;
  return close !== -1 && (lineEnd === -1 || close <= lineEnd) ? close + 1 : -1;
}

/**
 * The names of the control words in `tex`, token by token as KaTeX's lexer reads it: a
 * comment starts only at a token that is `%` alone (`%` with a combining mark after it is
 * another token, which KaTeX may skip unparsed, as `\@secondoftwo` does its argument),
 * and runs to the next line feed; a `\verb` is one token. Where KaTeX's lexer would stop
 * with an error, this reads on, a character later: more names, never fewer.
 */
function lexedNames(tex: string): Set<string> {
  const names = new Set<string>();
  const verbs = tex.includes('\\verb') ? verbIndex(tex) : null;
  let position = 0;
  while (position < tex.length) {
    if (verbs !== null && tex.charCodeAt(position) === 0x5c) {
      const end = verbEnd(tex, position, verbs);
      if (end !== -1) {
        position = end;
        continue;
      }
    }
    TOKEN.lastIndex = position;
    const match = TOKEN.exec(tex);
    if (match === null) {
      position += 1;
      continue;
    }
    position = TOKEN.lastIndex;
    const name = match[4];
    if (name !== undefined) {
      names.add(name.slice(1));
    } else if (match[3] === '%') {
      const newline = tex.indexOf('\n', position);
      if (newline === -1) break;
      position = newline + 1;
    }
  }
  return names;
}

/** Every name after a backslash in `tex`, wherever the backslash stands. */
function everyName(tex: string): Set<string> {
  const names = new Set<string>();
  for (const match of tex.matchAll(/\\([A-Za-z@]+)/g)) names.add(match[1]!);
  return names;
}

/**
 * The names of the commands in `tex`, as KaTeX reads them (`lexedNames`). Only in the link
 * of `\href`, `\url` or `\includegraphics` does KaTeX read a `%` as a character rather
 * than a comment, and which `%` that is depends on how it parses the formula. So in a
 * formula with one of them and a `%`, every name after a backslash counts, however the
 * formula is read: more names, never fewer.
 */
function commandNames(tex: string): Set<string> {
  const names = lexedNames(tex);
  if (tex.includes('%') && LINK_ARGUMENTS.some((name) => names.has(name))) {
    for (const name of everyName(tex)) names.add(name);
  }
  return names;
}

/** The first command in `tex` that defines a macro, or null. */
export function definesMacros(tex: string): string | null {
  for (const name of commandNames(tex)) if (DEFINITIONS.has(name)) return name;
  return null;
}

/**
 * Whether a repair of `original` is one to show: it defines no macro, and it brings in no
 * command that KaTeX gates by trust that the original did not have. Removals can join the
 * pieces of one (`\hr\label{a}ef` would become `\href`), and a model can be steered.
 */
export function safeRepair(original: string, repair: string): boolean {
  const before = commandNames(original);
  for (const name of commandNames(repair)) {
    if (DEFINITIONS.has(name)) return false;
    if (TRUSTED_ONLY.has(name) && !before.has(name)) return false;
  }
  return true;
}
