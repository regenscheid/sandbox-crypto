// SPDX-License-Identifier: MIT
/**
 * Characters a person reading a text may not see (threat model, A3), as the server
 * refuses them in text agents read (documents/agent_text.py): controls but the tab and
 * the line feed, format characters (zero-width, bidirectional and tag characters among
 * them), surrogates, private-use and unassigned code points, line and paragraph
 * separators, and every Default_Ignorable code point. `hidden.fixtures.json` holds the set
 * and the cases both sides agree on.
 */
import fixtures from './hidden.fixtures.json';

const categories = fixtures.categories.map((category) => `\\p{${category}}`).join('');
const ranges = fixtures.default_ignorable
  .map(([first, last]) => `\\u{${first!.toString(16)}}-\\u{${last!.toString(16)}}`)
  .join('');
const allowed = fixtures.allowed.map((code) => `\\u{${code.toString(16)}}`).join('');

/** One hidden character, anywhere in a text. */
const HIDDEN = new RegExp(`(?![${allowed}])[${categories}${ranges}]`, 'gu');

/** The code point of one character, as `U+202E`. */
export function codePoint(character: string): string {
  return `U+${character.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}`;
}

/** A text's parts, its hidden characters each a part of its own. */
export interface Part {
  text: string;
  hidden: boolean;
}

export function parts(text: string): Part[] {
  const found: Part[] = [];
  let at = 0;
  for (const match of text.matchAll(HIDDEN)) {
    if (match.index > at) found.push({ text: text.slice(at, match.index), hidden: false });
    found.push({ text: match[0], hidden: true });
    at = match.index + match[0].length;
  }
  if (at < text.length) found.push({ text: text.slice(at), hidden: false });
  return found;
}

/** Each hidden character's position, in code points from 0, and code point. */
export function hiddenCharacters(text: string): [number, string][] {
  const found: [number, string][] = [];
  let position = 0;
  for (const character of text) {
    if (character.match(HIDDEN)) found.push([position, codePoint(character)]);
    position++;
  }
  return found;
}

/**
 * Text for showing as code, with each hidden character shown as its code point, such as
 * `⟦U+202E⟧`: left in, they can make a text read differently from what an agent or a
 * program receives.
 */
export function visible(text: string): string {
  return text.replace(HIDDEN, (character) => `⟦${codePoint(character)}⟧`);
}
