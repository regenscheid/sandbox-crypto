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
  // Only figures count toward the limit: a file that is not drawn is a link, and costs nothing.
  let figures = 0;
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
      const figure = ['image', 'frame'].includes(pinView(pin).shown);
      const found = { target, alt: match[1]!, pin, drawn: !figure || figures < FIGURES_PER_TEXT };
      if (figure) figures += 1;
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

/** A chart's data file, as its pin names it, for the figure frame (FG-15). */
export interface ChartData {
  path: string;
  artifact: string;
  revision: number;
  sha256: string;
}

/** A figure check's result as the server records it (FG-21, FG-23); `unchecked` when
 * the revision shown has none, as an earlier revision or one not checked yet. */
export interface CheckView {
  status: 'accepted' | 'rejected' | 'unavailable' | 'unchecked';
  errors: { kind: string; message: string; line: number | null; column: number | null }[];
  notes: { kind: string; message: string }[];
  reason: string | null;
}

/** The check of the `nth` chart at `place`: its block, or `statement` in a ledger
 * statement, which has no blocks of its own. */
export function chartCheck(checks: readonly Pin[], place: string, nth: number): CheckView {
  const found = checks.find((item) => item.place === place && item.figure === `chart:${nth}`);
  const status = text(found?.status);
  const problems = (value: unknown) =>
    (Array.isArray(value) ? (value as Pin[]) : []).map((item) => ({
      kind: text(item.kind),
      message: text(item.message),
      line: count(item.line),
      column: count(item.column),
    }));
  return {
    status: ['accepted', 'rejected', 'unavailable'].includes(status)
      ? (status as CheckView['status'])
      : 'unchecked',
    errors: problems(found?.errors),
    notes: problems(found?.notes),
    reason: text(found?.reason) || null,
  };
}

/** A chart fence placed in the rendered text. */
export interface PlacedChart {
  target: HTMLElement;
  source: string;
  data: ChartData[];
  /** Its data paths that are not pinned, each with why: the chart is not drawn. */
  broken: { path: string; reason: string }[];
  drawn: boolean;
  /** Its check, in a text that is checked (FG-23); none in one that is not, as chat. */
  check: CheckView | null;
}

/** The data paths a chart's text names (`data.url`), each occurrence in order, as the
 * server pins each (review of #500, L3); none if it is not JSON. */
export function chartPaths(source: string): string[] {
  const found: string[] = [];
  const walk = (value: unknown): void => {
    if (Array.isArray(value)) value.forEach(walk);
    else if (value !== null && typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) {
        const url = (item as { url?: unknown } | null)?.url;
        if (key === 'data' && typeof url === 'string') found.push(url);
        walk(item);
      }
    }
  };
  try {
    walk(JSON.parse(source));
  } catch {
    return [];
  }
  return found;
}

/**
 * Give each `vega-lite` fence in `root` a place of its own (FG-1, FG-15), with the pins of
 * the data paths it names, one for each occurrence, in order: in a document, only its own
 * block's pins, so one chart never takes another's (review of #500, L3). A text's first
 * `FIGURES_PER_TEXT` figures, counting `before` already placed, are drawn at once. Given
 * `checks`, the text's figure checks, each chart has its own.
 */
export function placeCharts(
  root: HTMLElement,
  pins: readonly Pin[],
  before = 0,
  checks: readonly Pin[] | null = null,
): PlacedChart[] {
  const seen = new Map<string, number>();
  const queues = new Map<string, Pin[]>();
  const key = (block: string | null, path: string) => `${block ?? ''}\u0000${path}`;
  for (const pin of pins) {
    if (text(pin.kind) !== 'chart_data') continue;
    for (const block of new Set([null, typeof pin.block === 'string' ? pin.block : null])) {
      const at = key(block, text(pin.path));
      queues.set(at, [...(queues.get(at) ?? []), pin]);
    }
  }
  const taken = new Set<Pin>();
  const next = (block: string | null, path: string): Pin | undefined => {
    const queue = queues.get(key(block, path)) ?? [];
    const found = queue.find((pin) => !taken.has(pin));
    if (found) taken.add(found);
    return found;
  };
  const placed: PlacedChart[] = [];
  for (const code of root.querySelectorAll('pre > code.language-vega-lite')) {
    const source = code.textContent ?? '';
    const block = code.closest('[data-block]')?.getAttribute('data-block') ?? null;
    const data: ChartData[] = [];
    const broken: { path: string; reason: string }[] = [];
    for (const path of chartPaths(source)) {
      const view = pinView(next(block, path) ?? { path, unresolved: 'no_file' });
      if (view.shown === 'broken') broken.push({ path, reason: view.reason ?? '' });
      else
        data.push({ path, artifact: view.artifact, revision: view.revision, sha256: view.sha256 });
    }
    const target = root.ownerDocument.createElement('span');
    target.className = 'figure-ref';
    code.parentElement!.replaceWith(target);
    const place = block ?? 'statement';
    const nth = seen.get(place) ?? 0;
    seen.set(place, nth + 1);
    const drawn = before + placed.length < FIGURES_PER_TEXT;
    const check = checks === null ? null : chartCheck(checks, place, nth);
    placed.push({ target, source, data, broken, drawn, check });
  }
  return placed;
}
