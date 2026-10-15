// SPDX-License-Identifier: MIT
/**
 * The workspace's side of the PDF viewer (docs/specs/pdf-viewer.md, PV-2, PV-6): which
 * artifacts it opens, where the viewer's frame may come from, and which messages from that
 * frame are anchors. The viewer runs on the preview origin; the workspace believes a
 * message only when it comes from that origin and that frame, names the PDF the frame
 * shows, and has exactly an anchor's shape.
 */
import type { ArtifactEntry } from './api-types';

export const PDF = 'application/pdf';
export const ANCHOR_MESSAGE = 'sandbox-science/pdf-anchor';
/** The viewer's own limit on a quote. */
export const MAX_QUOTE = 2000;
const MAX_LABEL = 200;

/** A place in a published PDF (docs/specs/pdf-sources.md, "Anchors"). */
export interface PdfAnchor {
  sha256: string;
  path: string;
  page: { index: number; label: string };
  box: [number, number, number, number];
  quote: string;
}

/** What the frame shows: where it comes from, which window it is, and which PDF. */
export interface ViewerFrame {
  origin: string;
  window: unknown;
  sha256: string;
  path: string;
}

/** The artifacts the viewer opens, when the server has it on. */
export function viewable(entry: ArtifactEntry, viewer: boolean): boolean {
  return viewer && entry.media_type === PDF;
}

/** The origin of a viewer URL the server gave; null for any URL that is not http(s). */
export function viewerOrigin(url: string): string | null {
  try {
    const found = new URL(url);
    return found.protocol === 'http:' || found.protocol === 'https:' ? found.origin : null;
  } catch {
    return null;
  }
}

// Control characters and the bidirectional formatting characters, which could make a
// quote read differently from what it holds (a right-to-left override, say).
const HIDDEN = /[\p{Cc}\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/gu;

/** Text from the frame with its whitespace collapsed and every hidden character shown. */
export function visible(text: string): string {
  return text
    .replace(/\s+/g, ' ')
    .trim()
    .replace(
      HIDDEN,
      (found) => `⟨U+${found.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}⟩`,
    );
}

function record(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const own = Object.keys(value).sort();
  return own.length === keys.length && [...keys].sort().every((key, i) => own[i] === key);
}

/**
 * The anchor a `message` event carries, or null when it is not one from the frame: another
 * origin or window, another PDF, or anything but an anchor's exact shape.
 */
export function receivedAnchor(
  event: { origin: string; source: unknown; data: unknown },
  frame: ViewerFrame,
): PdfAnchor | null {
  if (event.origin !== frame.origin || event.source !== frame.window || frame.window == null) {
    return null;
  }
  const data = event.data;
  if (!record(data, ['type', 'anchor']) || data.type !== ANCHOR_MESSAGE) return null;
  const anchor = data.anchor;
  if (!record(anchor, ['sha256', 'page', 'box', 'quote']) || anchor.sha256 !== frame.sha256) {
    return null;
  }
  const { page, box, quote } = anchor;
  if (!record(page, ['index', 'label'])) return null;
  const { index, label } = page;
  if (!Number.isSafeInteger(index) || (index as number) < 1) return null;
  if (typeof label !== 'string' || label.length > MAX_LABEL) return null;
  // Each position, so a sparse array's holes are refused too.
  if (!Array.isArray(box) || box.length !== 4) return null;
  if (![0, 1, 2, 3].every((i) => typeof box[i] === 'number' && Number.isFinite(box[i]))) {
    return null;
  }
  if (typeof quote !== 'string' || quote.length > MAX_QUOTE) return null;
  return {
    sha256: frame.sha256,
    path: frame.path,
    page: { index: index as number, label: visible(label) },
    box: [box[0], box[1], box[2], box[3]] as PdfAnchor['box'],
    quote: visible(quote),
  };
}

/** The anchor as it is copied: JSON, for pasting into a comment or a chat message. */
export function anchorText(anchor: PdfAnchor): string {
  return JSON.stringify(anchor);
}

/** A file's size for the artifact list. */
export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
}
