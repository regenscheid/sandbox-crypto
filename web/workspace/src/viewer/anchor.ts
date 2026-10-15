// SPDX-License-Identifier: MIT
/**
 * The PDF viewer's decisions, apart from its page (main.ts): what its URL's fragment holds,
 * which page a number or a printed label names, and the anchor a text selection makes
 * (docs/specs/pdf-viewer.md, PV-6; docs/specs/pdf-sources.md, "Anchors"). Nothing here
 * touches the workspace: the viewer runs on its own origin.
 */

/** What the workspace put in the viewer's URL fragment. */
export interface ViewerParams {
  /** The capability that opens one PDF on this origin (`/pdf/<capability>`). */
  capability: string;
  /** The workspace's origin: the only window the viewer sends anchors to. */
  workspace: string;
  /** The page to open at, such as a quote's; null for the first. */
  page: number | null;
}

/**
 * The fragment `#c=<capability>&w=<workspace origin>`, with `&p=<page>` to open at a page;
 * null when it is not one. A page that is not a number from 1 opens the first.
 */
export function viewerParams(hash: string): ViewerParams | null {
  const found = new URLSearchParams(hash.replace(/^#/, ''));
  const capability = found.get('c') ?? '';
  const workspace = found.get('w') ?? '';
  const asked = found.get('p') ?? '';
  const page = /^[1-9][0-9]{0,5}$/.test(asked) ? Number(asked) : null;
  if (!/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(capability)) return null;
  try {
    const origin = new URL(workspace);
    if (!['http:', 'https:'].includes(origin.protocol) || origin.origin !== workspace) return null;
  } catch {
    return null;
  }
  return { capability, workspace, page };
}

/**
 * Whether the viewer may open for this workspace: where the browser lists the origins
 * that frame the page (`location.ancestorOrigins`), the nearest must be the workspace.
 * Browsers without the list rely on the CSP's `frame-ancestors`.
 */
export function framedBy(workspace: string, ancestors: ArrayLike<string> | undefined): boolean {
  return ancestors === undefined || ancestors.length === 0 || ancestors[0] === workspace;
}

/** The most pixels a page's canvas gets, as PDF.js's own viewer caps it. */
export const MAX_CANVAS_PIXELS = 2 ** 25;

/**
 * The canvas pixels per CSS pixel for a page shown `width` by `height`: the screen's
 * ratio, lowered when the canvas would pass the cap.
 */
export function canvasRatio(width: number, height: number, device: number): number {
  const area = width * height;
  if (!(area > 0)) return device;
  return Math.min(device, Math.sqrt(MAX_CANVAS_PIXELS / area));
}

/** The SHA-256 a capability names: its payload is readable, its MAC is the server's. */
export function capabilitySha(capability: string): string | null {
  try {
    const payload = capability.split('.')[0]!.replaceAll('-', '+').replaceAll('_', '/');
    const fields: unknown = JSON.parse(atob(payload + '='.repeat((4 - (payload.length % 4)) % 4)));
    const sha = Array.isArray(fields) ? fields[3] : null;
    return typeof sha === 'string' && /^[0-9a-f]{64}$/.test(sha) ? sha : null;
  } catch {
    return null;
  }
}

/** A page's printed label; without page labels, the label is the page's number. */
export function pageLabel(labels: readonly string[] | null, index: number): string {
  return labels?.[index - 1] || String(index);
}

/** The page a printed label names; null when no page has it. */
export function pageByLabel(labels: readonly string[] | null, label: string, count: number) {
  const wanted = label.trim();
  for (let index = 1; index <= count; index++)
    if (pageLabel(labels, index) === wanted) return index;
  return null;
}

/** A rectangle of the page as shown, in CSS pixels from its top left. */
export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

type Box = [number, number, number, number];

/**
 * The box `[x0, y0, x1, y1]` in PDF points that covers the rectangles, through the page's
 * own conversion (origin at the bottom left, before rotation); null for no rectangle.
 */
export function pdfBox(rects: readonly Rect[], toPdf: (x: number, y: number) => number[]) {
  const xs: number[] = [];
  const ys: number[] = [];
  for (const rect of rects) {
    if (rect.right <= rect.left || rect.bottom <= rect.top) continue;
    for (const [x, y] of [
      [rect.left, rect.top],
      [rect.right, rect.bottom],
    ] as const) {
      const [px, py] = toPdf(x, y);
      xs.push(px!);
      ys.push(py!);
    }
  }
  if (!xs.length) return null;
  const round = (value: number) => Math.round(value * 100) / 100;
  return [
    round(Math.min(...xs)),
    round(Math.min(...ys)),
    round(Math.max(...xs)),
    round(Math.max(...ys)),
  ] as Box;
}

/** A place in the PDF, as the viewer sends it to the workspace (`PdfAnchor`, less its path). */
export interface PdfAnchor {
  sha256: string;
  page: { index: number; label: string };
  box: Box;
  quote: string;
}

/** The message the viewer posts to the workspace's window. */
export interface AnchorMessage {
  type: 'sandbox-science/pdf-anchor';
  anchor: PdfAnchor;
}

/** The longest quote an anchor carries; the box still names the whole selection. */
export const MAX_QUOTE = 2000;

export function anchorMessage(
  sha256: string,
  index: number,
  label: string,
  box: Box,
  quote: string,
): AnchorMessage {
  const text = quote.replace(/\s+/g, ' ').trim().slice(0, MAX_QUOTE);
  return {
    type: 'sandbox-science/pdf-anchor',
    anchor: { sha256, page: { index, label }, box, quote: text },
  };
}
