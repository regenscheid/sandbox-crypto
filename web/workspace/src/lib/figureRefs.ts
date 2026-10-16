// SPDX-License-Identifier: MIT
/**
 * A text's references to recorded files, drawn where the text writes them (docs/specs/
 * figures.md, FG-6 to FG-9, FG-12, FG-17). The renderer leaves a path image as text
 * (LD-29); the server's reference map pins each one. Here each `![alt](path)` the text
 * shows outside code, math and links is matched, in order, to the pin of the same path,
 * and its place is given to a figure: an image, a frame, a link for another type, or the
 * reason it is broken. A text draws at most 20 figures; the rest wait for a click.
 */
import { FRAMED_TYPES, IMAGE_TYPES } from './figures';

/** A pin as the server's reference map keeps it (FG-10). */
export type Pin = Record<string, unknown>;

/** What a text draws at most at once; the rest show their source and a button (FG-17). */
export const FIGURES_PER_TEXT = 20;

const REASONS: Record<string, string> = {
  no_file: 'no recorded file at that path',
  no_revision: 'no such revision of the file',
  deleted: 'the file was deleted',
  content_not_kept: 'its bytes were not kept',
  needs_session: 'a relative path needs a session; write shared/… or /sessions/<s>/…',
};

/** A pin in the page's words: how it is shown, its revision and a newer one, or why not. */
export interface PinView {
  path: string;
  block: string | null;
  shown: 'image' | 'frame' | 'link' | 'broken';
  reason: string | null;
  artifact: string;
  revision: number;
  sha256: string;
  latest: number | null;
}

const text = (value: unknown): string => (typeof value === 'string' ? value : '');
const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null;

export function pinView(pin: Pin): PinView {
  const base = {
    path: text(pin.path),
    block: typeof pin.block === 'string' ? pin.block : null,
    artifact: text(pin.artifact),
    revision: count(pin.revision) ?? 0,
    sha256: text(pin.sha256),
    latest: count(pin.latest),
  };
  const unresolved = text(pin.unresolved);
  if (unresolved || !base.artifact || !base.revision || !/^[0-9a-f]{64}$/.test(base.sha256))
    return { ...base, shown: 'broken', reason: REASONS[unresolved] ?? 'it is not pinned' };
  const media = text(pin.media_type);
  const shown = IMAGE_TYPES.has(media) ? 'image' : FRAMED_TYPES.has(media) ? 'frame' : 'link';
  return { ...base, shown, reason: null };
}

/** The pin's revision, and a newer one when there is: `r3, newer: r5` (FG-8). */
export function revisionText(view: PinView): string {
  if (view.shown === 'broken') return '';
  return view.latest ? `r${view.revision}, newer: r${view.latest}` : `r${view.revision}`;
}

/** One reference placed in the rendered text. */
export interface Placed {
  target: HTMLElement;
  alt: string;
  pin: Pin;
  /** Drawn at once; past the text's limit it waits for a click. */
  drawn: boolean;
}

const REFERENCE = /!\[([^\]\n]{0,1000})\]\(([^()\s]{1,4096})\)/g;
const SKIPPED = 'code, pre, a, .katex, .math, .math-source, .figure-ref, script, style';

/**
 * Give each path image in `root`'s text a place of its own, matched in order to the pin
 * of its path; a path image with no pin is left as text. Elements are made with the DOM,
 * never from markup.
 */
export function placeReferences(root: HTMLElement, pins: readonly Pin[]): Placed[] {
  const byPath = new Map<string, Pin[]>();
  for (const pin of pins) {
    if (text(pin.kind) !== 'image') continue;
    byPath.set(text(pin.path), [...(byPath.get(text(pin.path)) ?? []), pin]);
  }
  if (byPath.size === 0) return [];
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode())
    if (!node.parentElement?.closest(SKIPPED)) nodes.push(node as Text);
  const placed: Placed[] = [];
  for (const node of nodes) {
    const value = node.data;
    const parts: (string | Placed)[] = [];
    let last = 0;
    for (const match of value.matchAll(REFERENCE)) {
      const queue = byPath.get(match[2]!);
      const pin = queue?.shift();
      if (!pin) continue;
      parts.push(value.slice(last, match.index));
      const target = root.ownerDocument.createElement('span');
      target.className = 'figure-ref';
      const found = { target, alt: match[1]!, pin, drawn: placed.length < FIGURES_PER_TEXT };
      placed.push(found);
      parts.push(found);
      last = match.index! + match[0].length;
    }
    if (!parts.length) continue;
    parts.push(value.slice(last));
    node.replaceWith(...parts.map((part) => (typeof part === 'string' ? part : part.target)));
  }
  return placed;
}
