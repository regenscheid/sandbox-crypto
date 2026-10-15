// SPDX-License-Identifier: MIT
/**
 * Characters a person reading a text may not see (threat model, A3), as the server
 * refuses them in text agents read (documents/agent_text.py): controls but the tab and
 * the line feed, format characters (zero-width, bidirectional and tag characters among
 * them), surrogates, private-use and unassigned code points, line and paragraph
 * separators, and every Default_Ignorable code point, but U+FE0F where it asks for an
 * emoji's colour form (after an emoji, or in a keycap). `hidden.fixtures.json` holds the
 * set and the cases both sides agree on.
 */
import fixtures from './hidden.fixtures.json';

const categories = fixtures.categories.map((category) => `\\p{${category}}`).join('');
const ranges = fixtures.default_ignorable
  .map(([first, last]) => `\\u{${first!.toString(16)}}-\\u{${last!.toString(16)}}`)
  .join('');
const allowed = fixtures.allowed.map((code) => `\\u{${code.toString(16)}}`).join('');
const code = (value: number) => `\\u{${value.toString(16)}}`;
const bases = fixtures.emoji_bases.map(([first, last]) => `${code(first!)}-${code(last!)}`);
const keycaps = fixtures.keycaps.bases.map(code).join('');

/** U+FE0F where it asks for an emoji's colour form: after an emoji, or in a keycap. */
const EMOJI = `(?:(?<=[${bases.join('')}])\\u{fe0f}|(?<=[${keycaps}])\\u{fe0f}(?=${code(fixtures.keycaps.mark)}))`;

/** One hidden character, anywhere in a text: what the server refuses. */
const HIDDEN = new RegExp(`(?!${EMOJI})(?![${allowed}])[${categories}${ranges}]`, 'gu');

const chips = fixtures.chips;
const lookalikes =
  chips.categories.map((category) => `\\p{${category}}`).join('') +
  chips.extra.map((code) => `\\u{${code.toString(16)}}`).join('');
const plain = chips.except.map((code) => `\\u{${code.toString(16)}}`).join('');

/**
 * One character shown as its code point: a hidden one, or one that can pass for other
 * text without the server refusing it (spaces other than U+0020, the blank Braille
 * pattern, the object replacement character).
 */
const CHIPPED = new RegExp(
  `(?!${EMOJI})(?![${allowed}${plain}])[${categories}${ranges}${lookalikes}]`,
  'gu',
);

/** The code point of one character, as `U+202E`. */
export function codePoint(character: string): string {
  return `U+${character.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}`;
}

/** A text's parts, each character shown as its code point a part of its own. */
export interface Part {
  text: string;
  hidden: boolean;
}

export function parts(text: string): Part[] {
  const found: Part[] = [];
  let at = 0;
  for (const match of text.matchAll(CHIPPED)) {
    if (match.index > at) found.push({ text: text.slice(at, match.index), hidden: false });
    found.push({ text: match[0], hidden: true });
    at = match.index + match[0].length;
  }
  if (at < text.length) found.push({ text: text.slice(at), hidden: false });
  return found;
}

/** Each character the server refuses: its position, in code points from 0, and code point. */
export function hiddenCharacters(text: string): [number, string][] {
  const found: [number, string][] = [];
  let position = 0;
  let at = 0;
  for (const match of text.matchAll(HIDDEN)) {
    position += [...text.slice(at, match.index)].length;
    found.push([position, codePoint(match[0])]);
    at = match.index;
  }
  return found;
}

/**
 * Text for showing as code, with each hidden character shown as its code point, such as
 * `⟦U+202E⟧`: left in, they can make a text read differently from what an agent or a
 * program receives.
 */
export function visible(text: string): string {
  return text.replace(CHIPPED, (character) => `⟦${codePoint(character)}⟧`);
}

/** How many characters a text shows as their code points. */
export function chipped(text: string): number {
  return [...text.matchAll(CHIPPED)].length;
}

/**
 * Line ends as line feeds, as the server keeps an agent's text before checking it:
 * a carriage return and line feed, or a carriage return alone.
 */
export function newlines(text: string): string {
  return text.replace(/\r\n?/g, '\n');
}

/** An ANSI colour or style sequence, which terminals draw as nothing. */
const STYLE = /\u001b\[[\d;:]*m/g;

/**
 * Program output and code as `visible` shows them, without the noise of terminals:
 * colour and style sequences gone, and a carriage return a line end, as a progress bar
 * that redraws its line reads best line by line. Every other hidden character stays a
 * code point.
 */
export function visibleOutput(text: string): string {
  return visible(text.replace(STYLE, '').replace(/\r\n?/g, '\n'));
}

/**
 * The line, from 1, where more than `max_blank_lines` blank lines in a row begin between
 * lines of text, or null: the server refuses such text, since what follows could sit out
 * of view. A line of spaces and tabs is blank; blank lines before the first line of text
 * or after the last push nothing out of view.
 */
export function blankRun(text: string): number | null {
  let run = 0;
  let seen = false;
  const lines = text.split('\n');
  for (let index = 0; index < lines.length; index++) {
    if (/^[ \t]*$/.test(lines[index]!)) {
      run++;
      continue;
    }
    if (seen && run > fixtures.max_blank_lines) return index + 1 - run;
    run = 0;
    seen = true;
  }
  return null;
}
