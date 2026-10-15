// SPDX-License-Identifier: MIT
/**
 * What `ApprovalText` shows of a text the user approves and agents then read (threat
 * model, A3): the text as it is, its differences from the text before it, word by word
 * or line by line, and the links a rendering of it would hide behind their words or not
 * show at all, such as a link reference definition and its title.
 */
import type { Token } from 'markdown-it';
import { parser } from './markdown';
import { codePoint, hiddenCharacters } from './text';

export interface Change {
  kind: 'same' | 'removed' | 'added';
  text: string;
}

/** The most cells a comparison may take: past it, words give way to lines, then to none. */
const MAX_CELLS = 2_000_000;

function compare(before: string[], after: string[]): Change[] {
  const rows = before.length + 1;
  const columns = after.length + 1;
  // lengths[i][j]: the longest common run of before[i..] and after[j..].
  const lengths = new Uint32Array(rows * columns);
  for (let i = before.length - 1; i >= 0; i--) {
    for (let j = after.length - 1; j >= 0; j--) {
      lengths[i * columns + j] =
        before[i] === after[j]
          ? lengths[(i + 1) * columns + j + 1]! + 1
          : Math.max(lengths[(i + 1) * columns + j]!, lengths[i * columns + j + 1]!);
    }
  }
  const changes: Change[] = [];
  const add = (kind: Change['kind'], text: string) => {
    const last = changes[changes.length - 1];
    if (last && last.kind === kind) last.text += text;
    else changes.push({ kind, text });
  };
  let i = 0;
  let j = 0;
  while (i < before.length && j < after.length) {
    if (before[i] === after[j]) {
      add('same', before[i]!);
      i++;
      j++;
    } else if (lengths[(i + 1) * columns + j]! >= lengths[i * columns + j + 1]!) {
      add('removed', before[i++]!);
    } else {
      add('added', after[j++]!);
    }
  }
  while (i < before.length) add('removed', before[i++]!);
  while (j < after.length) add('added', after[j++]!);
  return changes;
}

/**
 * The differences between two texts, word by word (white space kept as its own parts),
 * or line by line when the texts are too long for that; null when they are too long for
 * either.
 */
export function textDiff(before: string, after: string): Change[] | null {
  for (const split of [/(\s+)/, /(?<=\n)/]) {
    const left = before.split(split).filter((part) => part !== '');
    const right = after.split(split).filter((part) => part !== '');
    if ((left.length + 1) * (right.length + 1) <= MAX_CELLS) return compare(left, right);
  }
  return null;
}

export interface FoundLink {
  /** A link in the text, an image, or a reference definition, which shows as nothing. */
  kind: 'link' | 'image' | 'definition';
  /** What the rendering shows for it: its words, or a definition's label. */
  label: string;
  address: string;
  title: string;
}

function walk(tokens: Token[], found: FoundLink[]): void {
  let open: { address: string; title: string; words: string[] } | null = null;
  for (const token of tokens) {
    if (token.type === 'link_open') {
      open = {
        address: String(token.attrGet('href') ?? ''),
        title: String(token.attrGet('title') ?? ''),
        words: [],
      };
    } else if (token.type === 'link_close' && open !== null) {
      const { address, title, words } = open;
      found.push({ kind: 'link', label: words.join(''), address, title });
      open = null;
    } else if (token.type === 'image') {
      found.push({
        kind: 'image',
        label: token.content,
        address: String(token.attrGet('src') ?? ''),
        title: String(token.attrGet('title') ?? ''),
      });
    } else if (open !== null) {
      open.words.push(token.content);
    }
    if (token.children) walk(token.children, found);
  }
}

/** Every link address, title and reference definition a rendering of the text holds. */
export function linksIn(text: string): FoundLink[] {
  const env: { references?: Record<string, { href: string; title: string }> } = {};
  const found: FoundLink[] = [];
  walk(parser.parse(text, env), found);
  for (const [label, reference] of Object.entries(env.references ?? {})) {
    found.push({ kind: 'definition', label, address: reference.href, title: reference.title });
  }
  return found;
}

/** A character reference, of any number of digits, or named. */
const REFERENCE = /&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});/g;

/**
 * Each character reference a rendering shows as a hidden character, which the server
 * refuses too: its position, in code points from 0, the first hidden character it gives
 * (a named one can give two, such as a symbol and a variation selector), and the
 * reference as written. Decoded as the renderer decodes a link's title.
 */
export function hiddenReferences(text: string): [number, string, string][] {
  const found: [number, string, string][] = [];
  for (const match of text.matchAll(REFERENCE)) {
    const [first] = hiddenCharacters(parser.utils.unescapeAll(match[0]));
    if (first) found.push([[...text.slice(0, match.index)].length, first[1], match[0]]);
  }
  return found;
}

export { codePoint };
