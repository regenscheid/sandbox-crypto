// SPDX-License-Identifier: MIT
/**
 * The artifacts table on the project's Artifacts page: its columns, their widths, the order
 * of the rows loaded so far, and each file's kind in words. Sorting orders the rows the
 * page has read; the server sends them newest first, a page at a time.
 */
import type { ArtifactEntry } from './api-types';

export type ColumnKey = 'name' | 'kind' | 'revision' | 'size' | 'when';
export type SortDirection = 'asc' | 'desc';

export const COLUMNS: { key: ColumnKey; label: string; width: number }[] = [
  { key: 'name', label: 'Name', width: 340 },
  { key: 'kind', label: 'Kind', width: 140 },
  { key: 'revision', label: 'Revision', width: 100 },
  { key: 'size', label: 'Size', width: 100 },
  { key: 'when', label: 'Last change', width: 170 },
];

export const MIN_WIDTH = 72;
export const MAX_WIDTH = 720;

/** A column's width kept within its bounds, in whole pixels. */
export function clampWidth(width: number): number {
  return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(width)));
}

/** The direction a column sorts in first: numbers and times newest or largest first. */
export function firstDirection(key: ColumnKey): SortDirection {
  return key === 'name' || key === 'kind' ? 'asc' : 'desc';
}

/** A path's file name and the folder it is in. */
export function splitPath(path: string): { name: string; folder: string } {
  const at = path.lastIndexOf('/');
  return at < 0
    ? { name: path, folder: '' }
    : { name: path.slice(at + 1), folder: path.slice(0, at) };
}

const BY_TYPE: Record<string, string> = {
  'text/markdown': 'Markdown',
  'application/json': 'JSON data',
  'text/csv': 'CSV table',
  'application/pdf': 'PDF',
  'image/png': 'PNG image',
  'image/jpeg': 'JPEG image',
  'image/svg+xml': 'SVG figure',
  'text/html': 'HTML page',
  'text/x-python': 'Python script',
  'text/x-tex': 'LaTeX',
  'application/zip': 'Zip archive',
  'application/gzip': 'Gzip archive',
  'application/x-tar': 'Tar archive',
};

const BY_EXTENSION: Record<string, string> = {
  sage: 'Sage script',
  py: 'Python script',
  log: 'Log',
  txt: 'Text',
  md: 'Markdown',
  json: 'JSON data',
  jsonl: 'JSON lines',
  tex: 'LaTeX',
  sh: 'Shell script',
  lean: 'Lean source',
  c: 'C source',
  rs: 'Rust source',
  csv: 'CSV table',
  yaml: 'YAML',
  yml: 'YAML',
  toml: 'TOML',
};

/** A file's kind in words: from its extension, else its media type as recorded. */
export function kindText(entry: Pick<ArtifactEntry, 'path' | 'media_type' | 'deleted'>): string {
  if (entry.deleted) return 'Deleted';
  const extension = /\.([A-Za-z0-9]+)$/.exec(entry.path)?.[1]?.toLowerCase();
  return (extension && BY_EXTENSION[extension]) || BY_TYPE[entry.media_type] || entry.media_type;
}

/** Whether a file reads as text, so the viewer may show it. */
export function textual(entry: Pick<ArtifactEntry, 'path' | 'media_type'>): boolean {
  const extension = /\.([A-Za-z0-9]+)$/.exec(entry.path)?.[1]?.toLowerCase();
  return (
    entry.media_type.startsWith('text/') ||
    /json|xml|yaml|toml|javascript/.test(entry.media_type) ||
    (extension !== undefined && extension in BY_EXTENSION)
  );
}

/** The rows in the order asked, ties broken by path so the order is stable. */
export function sortEntries(
  entries: readonly ArtifactEntry[],
  key: ColumnKey,
  direction: SortDirection,
): ArtifactEntry[] {
  const value = (entry: ArtifactEntry): string | number => {
    switch (key) {
      case 'name':
        return splitPath(entry.path).name.toLocaleLowerCase();
      case 'kind':
        return kindText(entry).toLocaleLowerCase();
      case 'revision':
        return entry.revision;
      case 'size':
        return entry.deleted ? -1 : entry.size;
      case 'when':
        return Date.parse(entry.created_at) || 0;
    }
  };
  const sign = direction === 'asc' ? 1 : -1;
  return [...entries].sort((a, b) => {
    const [x, y] = [value(a), value(b)];
    const order =
      typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
    return order * sign || a.path.localeCompare(b.path);
  });
}

/** A time for the table: the clock today, else the day and the clock. */
export function shortTime(iso: string, now = new Date()): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return iso;
  const clock = time.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (time.toDateString() === now.toDateString()) return clock;
  const day = time.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(time.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  });
  return `${day}, ${clock}`;
}
