// SPDX-License-Identifier: MIT
/**
 * The figure frame's message protocol (docs/specs/figures.md, FG-14), shared by the frame
 * and the workspace's page. The frame accepts one message, from the workspace's origin
 * and its parent window only, in an exact shape: the figure's kind, a fence's text, and
 * one capability for each pinned file the figure shows or reads, with the path the text
 * names it by (a chart's `data.url`). It sends back only its
 * size and a status: `rendered`, or an error with one of a fixed set of codes, which the
 * workspace words itself, so no text from the frame reaches the workspace's page (review
 * of #492, L2). The workspace accepts only that shape, from that frame's window.
 */

/** The kinds the frame draws now; Mermaid comes with F6. */
export const KINDS = ['svg', 'vega-lite'] as const;
export type FigureKind = (typeof KINDS)[number];

export interface FigureRequest {
  type: 'figure';
  kind: FigureKind;
  /** A fence's text; empty for a file. */
  source: string;
  /** The path the text names each file by, in the order of `capabilities`. */
  paths: string[];
  capabilities: string[];
}

/** Why a figure was not drawn, as the frame says it; empty when it was. */
export const CODES = [
  '',
  'refused',
  'type',
  'size',
  'unreadable',
  'framing',
  'inline_data',
  'address',
  'version',
  'invalid',
] as const;
export type FigureCode = (typeof CODES)[number];

/** Each code in the workspace's words. */
export const CODE_TEXT: Record<Exclude<FigureCode, ''>, string> = {
  refused: 'the preview origin refused the file, or its link expired',
  type: 'the file is not the type it should be',
  size: 'the figure is larger than a page draws',
  unreadable: 'the figure could not be read or drawn',
  framing: 'the frame was not opened by this workspace',
  inline_data:
    'the chart holds data of its own: a chart reads a recorded file, or makes a sequence',
  address: 'the chart names an address that is not one of its pinned files, an image or a link',
  version: 'the chart names a Vega-Lite version other than 6',
  invalid: 'the chart is not a Vega-Lite specification that compiles',
};

export interface FigureStatus {
  type: 'figure-status';
  status: 'rendered' | 'error';
  width: number;
  height: number;
  code: FigureCode;
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
  if (!exact(data, ['type', 'kind', 'source', 'paths', 'capabilities'])) return null;
  const found = data as Record<string, unknown>;
  const { kind, source, paths, capabilities } = found;
  if (found['type'] !== 'figure' || !KINDS.includes(kind as FigureKind)) return null;
  if (typeof source !== 'string' || source.length > MAX_SOURCE) return null;
  if (!Array.isArray(capabilities) || capabilities.length > MAX_CAPABILITIES) return null;
  if (!capabilities.every((c): c is string => typeof c === 'string' && CAPABILITY.test(c)))
    return null;
  if (!Array.isArray(paths) || paths.length !== capabilities.length) return null;
  if (!paths.every((p): p is string => typeof p === 'string' && p.length <= 4096)) return null;
  // An SVG file is drawn from its one capability, with no text of its own.
  if (kind === 'svg' && (capabilities.length !== 1 || source !== '')) return null;
  if (kind === 'vega-lite' && source === '') return null;
  return {
    type: 'figure',
    kind: kind as FigureKind,
    source,
    paths: [...paths],
    capabilities: [...capabilities],
  };
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

/** A status the frame sends: its size, bounded, and why when it failed. */
export function statusMessage(
  status: FigureStatus['status'],
  width: number,
  height: number,
  code: FigureCode = '',
): FigureStatus {
  const size = (value: number) =>
    Number.isFinite(value) ? Math.min(100_000, Math.max(0, Math.round(value))) : 0;
  return {
    type: 'figure-status',
    status,
    width: size(width),
    height: size(height),
    code: status === 'rendered' ? '' : code || 'unreadable',
  };
}

/** A message's data as a frame's status, only in the exact shape; null otherwise. */
export function figureStatus(data: unknown): FigureStatus | null {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return null;
  if (!exact(data, ['type', 'status', 'width', 'height', 'code'])) return null;
  const found = data as Record<string, unknown>;
  const { status, width, height, code } = found;
  if (found['type'] !== 'figure-status' || (status !== 'rendered' && status !== 'error'))
    return null;
  const sized = (value: unknown) =>
    typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 100_000;
  if (!sized(width) || !sized(height) || !CODES.includes(code as FigureCode)) return null;
  if ((status === 'rendered') !== (code === '')) return null;
  return statusMessage(status, width as number, height as number, code as FigureCode);
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
