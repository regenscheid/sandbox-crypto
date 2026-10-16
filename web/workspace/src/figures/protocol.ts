// SPDX-License-Identifier: MIT
/**
 * The figure frame's message protocol (docs/specs/figures.md, FG-14), shared by the frame
 * and the workspace's page. The frame accepts one message, from the workspace's origin
 * and its parent window only, in an exact shape: the figure's kind, a fence's text, and
 * one capability for each pinned file the figure shows or reads. It sends back only its
 * size and a status: `rendered`, or an error with its reason or the limit passed. The
 * workspace accepts only that shape, from that frame's window. Nothing here touches the
 * DOM; the frame's page is `main.ts`.
 */

/** The kinds the frame draws now; Vega-Lite and Mermaid come with F5 and F6. */
export const KINDS = ['svg'] as const;
export type FigureKind = (typeof KINDS)[number];

export interface FigureRequest {
  type: 'figure';
  kind: FigureKind;
  /** A fence's text; empty for a file. */
  source: string;
  capabilities: string[];
}

export interface FigureStatus {
  type: 'figure-status';
  status: 'rendered' | 'error';
  width: number;
  height: number;
  /** Why it failed, or the limit it passed; empty when rendered. */
  reason: string;
}

/** A capability's one spelling: its payload and its MAC, unpadded base64url. */
export const CAPABILITY = /^[A-Za-z0-9_-]{1,2048}\.[A-Za-z0-9_-]{43}$/;
/** The most a fence's text and a figure's capabilities may be (FG-17's chart limit). */
export const MAX_SOURCE = 64 * 1024;
export const MAX_CAPABILITIES = 16;
/** The largest file the frame draws, as the preview origin serves it (FG-13). */
export const MAX_FILE_BYTES = 8 * 1024 * 1024;

const exact = (value: object, keys: readonly string[]): boolean => {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => own.includes(key));
};

/** A message's data as a figure request, only in the exact shape; null otherwise. */
export function figureRequest(data: unknown): FigureRequest | null {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return null;
  if (!exact(data, ['type', 'kind', 'source', 'capabilities'])) return null;
  const found = data as Record<string, unknown>;
  const { kind, source, capabilities } = found;
  if (found['type'] !== 'figure' || !KINDS.includes(kind as FigureKind)) return null;
  if (typeof source !== 'string' || source.length > MAX_SOURCE) return null;
  if (!Array.isArray(capabilities) || capabilities.length > MAX_CAPABILITIES) return null;
  if (!capabilities.every((c): c is string => typeof c === 'string' && CAPABILITY.test(c)))
    return null;
  // An SVG file is drawn from its one capability, with no text of its own.
  if (kind === 'svg' && (capabilities.length !== 1 || source !== '')) return null;
  return { type: 'figure', kind: kind as FigureKind, source, capabilities: [...capabilities] };
}

/** The request a message carries, if the workspace's origin sent it from the parent. */
export function accepted(
  event: { origin: string; source: unknown; data: unknown },
  workspace: string,
  parent: unknown,
): FigureRequest | null {
  if (event.origin !== workspace || event.source !== parent) return null;
  return figureRequest(event.data);
}

/** A status the frame sends: its size, bounded, and the reason cut short. */
export function statusMessage(
  status: FigureStatus['status'],
  width: number,
  height: number,
  reason = '',
): FigureStatus {
  const size = (value: number) => (Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0);
  return {
    type: 'figure-status',
    status,
    width: size(width),
    height: size(height),
    reason: reason.slice(0, 300),
  };
}

/** A message's data as a frame's status, only in the exact shape; null otherwise. */
export function figureStatus(data: unknown): FigureStatus | null {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return null;
  if (!exact(data, ['type', 'status', 'width', 'height', 'reason'])) return null;
  const found = data as Record<string, unknown>;
  const { status, width, height, reason } = found;
  if (found['type'] !== 'figure-status' || (status !== 'rendered' && status !== 'error'))
    return null;
  const sized = (value: unknown) =>
    typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 100_000;
  if (!sized(width) || !sized(height) || typeof reason !== 'string' || reason.length > 300)
    return null;
  return statusMessage(status, width as number, height as number, reason);
}

/** The workspace's origin from the frame's URL fragment (`#w=<origin>`); null unless it
 * is an http(s) origin exactly. */
export function frameWorkspace(hash: string): string | null {
  const workspace = new URLSearchParams(hash.replace(/^#/, '')).get('w') ?? '';
  try {
    const origin = new URL(workspace);
    return ['http:', 'https:'].includes(origin.protocol) && origin.origin === workspace
      ? workspace
      : null;
  } catch {
    return null;
  }
}

/** Every link, `href` and event attribute removed from what a library drew, before it is
 * shown (FG-14): a figure links nowhere and runs nothing. */
export function inert(root: Element): void {
  for (const element of [root, ...root.querySelectorAll('*')]) {
    for (const name of element.getAttributeNames()) {
      const lower = name.toLowerCase();
      if (lower.startsWith('on') || lower === 'href' || lower.endsWith(':href') || lower === 'src')
        element.removeAttribute(name);
    }
  }
  for (const link of root.querySelectorAll('a, script, foreignObject, iframe, object, embed'))
    link.replaceWith(...(link.localName === 'a' ? link.childNodes : []));
}
