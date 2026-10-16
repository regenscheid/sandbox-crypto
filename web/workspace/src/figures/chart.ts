// SPDX-License-Identifier: MIT
/**
 * A chart's rules before it is drawn (docs/specs/figures.md, FG-4, FG-15, FG-17): its
 * text is JSON within 64 KiB; its data are recorded files the text pins, read by path, or
 * a `sequence` of at most 10,000 values; inline `values`, `datasets`, other generated
 * data, any other address, image marks and links are refused; and a `$schema` names
 * Vega-Lite 6, read as a label and never fetched. And the loader Vega is given: it
 * answers only the chart's pinned files, from their capabilities, within the data limits.
 */
import type { FigureCode } from './protocol';
import { MAX_SOURCE } from './protocol';

/** A chart's limits (FG-17). */
export const MAX_DATA_BYTES = 8 * 1024 * 1024;
export const MAX_ROWS = 100_000;
export const MAX_MARKS = 20_000;
export const MAX_SEQUENCE = 10_000;
/** The Vega-Lite major version the frame draws (FG-15). */
export const VEGA_LITE_MAJOR = 6;

/** Why a chart is not drawn, as one of the protocol's codes. */
export class Refusal extends Error {
  constructor(readonly code: FigureCode) {
    super(code);
  }
}

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
const isObject = (value: unknown): value is { [key: string]: Json } =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

function checkData(data: { [key: string]: Json }, pinned: ReadonlySet<string>): void {
  if ('values' in data) throw new Refusal('inline_data');
  if ('graticule' in data || 'sphere' in data) throw new Refusal('inline_data');
  if ('url' in data && !(typeof data['url'] === 'string' && pinned.has(data['url'])))
    throw new Refusal('address');
  const sequence = data['sequence'];
  if (sequence !== undefined) {
    if (!isObject(sequence)) throw new Refusal('invalid');
    const { start, stop } = sequence;
    const step = sequence['step'] ?? 1;
    if (typeof start !== 'number' || typeof stop !== 'number' || typeof step !== 'number')
      throw new Refusal('invalid');
    const count = step === 0 ? Infinity : Math.ceil((stop - start) / step);
    if (!(count <= MAX_SEQUENCE)) throw new Refusal('size');
  }
}

function walk(value: Json, pinned: ReadonlySet<string>): void {
  if (Array.isArray(value)) {
    for (const item of value) walk(item, pinned);
    return;
  }
  if (!isObject(value)) return;
  for (const [key, item] of Object.entries(value)) {
    if (key === 'datasets') throw new Refusal('inline_data');
    // A link, an image mark or an image channel would reach another address.
    if (key === 'href' || key === 'url' || key === 'image') {
      if (key === 'url' && typeof item === 'string' && pinned.has(item)) continue;
      throw new Refusal('address');
    }
    if (key === 'mark' && (item === 'image' || (isObject(item) && item['type'] === 'image')))
      throw new Refusal('address');
    if (key === 'data' && isObject(item)) {
      checkData(item, pinned);
      for (const [inner, nested] of Object.entries(item))
        if (inner !== 'url' && inner !== 'values') walk(nested, pinned);
      continue;
    }
    walk(item, pinned);
  }
}

/** A chart's text as a specification the frame may compile, or a `Refusal`. */
export function checkedChart(source: string, paths: readonly string[]): { [key: string]: Json } {
  if (source.length > MAX_SOURCE) throw new Refusal('size');
  let spec: unknown;
  try {
    spec = JSON.parse(source);
  } catch {
    throw new Refusal('invalid');
  }
  if (!isObject(spec)) throw new Refusal('invalid');
  const schema = spec['$schema'];
  if (schema !== undefined) {
    const major = typeof schema === 'string' ? /\/vega-lite\/v(\d+)/.exec(schema)?.[1] : null;
    if (Number(major) !== VEGA_LITE_MAJOR) throw new Refusal('version');
  }
  walk(spec, new Set(paths));
  return spec;
}

/** The rows of a data file Vega will read, as its format or extension says: a JSON
 * array's items, or the lines of a CSV or TSV after its header. */
export function rows(text: string, path: string): number {
  if (/\.json$/i.test(path) || /^\s*[[{]/.test(text)) {
    try {
      const data: unknown = JSON.parse(text);
      return Array.isArray(data) ? data.length : 1;
    } catch {
      return 0; // Vega's own reader says why
    }
  }
  const lines = text.split(/\r?\n/).filter((line) => line.trim() !== '');
  return Math.max(0, lines.length - 1);
}

/** What the loader fetches a pinned file with, as the frame does. */
export type Fetch = (capability: string) => Promise<string>;

/**
 * The loader Vega reads with (FG-15): a data path the chart pins is read from its
 * capability; any other address, an image or a link is refused. Data are at most 8 MiB in
 * all and 100,000 rows in each file.
 */
export function chartLoader(files: ReadonlyMap<string, string>, read: Fetch) {
  let bytes = 0;
  const refuse = () => Promise.reject(new Refusal('address'));
  return {
    async sanitize(uri: string, options?: { context?: string }) {
      if (options?.context !== 'dataflow' || !files.has(uri)) throw new Refusal('address');
      return { href: uri };
    },
    async load(uri: string, options?: { context?: string }) {
      await this.sanitize(uri, { context: options?.context ?? 'dataflow' });
      const text = await read(files.get(uri)!);
      bytes += new TextEncoder().encode(text).length;
      if (bytes > MAX_DATA_BYTES || rows(text, uri) > MAX_ROWS) throw new Refusal('size');
      return text;
    },
    http: refuse,
    file: refuse,
    fileAccess: false,
  };
}

/** The marks a drawn scene holds, counted to the limit. */
export function markCount(scene: unknown): number {
  let count = 0;
  const visit = (node: unknown): void => {
    if (!isObject(node as Json) || count > MAX_MARKS) return;
    const items = (node as { items?: unknown }).items;
    if (Array.isArray(items)) {
      count += items.length;
      for (const item of items) visit(item);
    }
  };
  visit(scene);
  return count;
}

/**
 * A delimited file's rows as objects, keyed by its header, every value a string, for
 * Vega's own type inference to read: the reader Vega's `csv`, `tsv` and `dsv` formats are
 * given in the frame. Vega's own builds each row's object with generated code, which the
 * frame's CSP refuses (no `'unsafe-eval'`). Quoted fields may hold the delimiter, line
 * breaks and doubled quotes.
 */
export function delimited(text: string, delimiter: string): Record<string, string>[] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  for (let at = 0; at < text.length; at++) {
    const char = text[at]!;
    if (quoted) {
      if (char === '"' && text[at + 1] === '"') {
        field += '"';
        at++;
      } else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"' && field === '') quoted = true;
    else if (char === delimiter) {
      row.push(field);
      field = '';
    } else if (char === '\n' || char === '\r') {
      if (char === '\r' && text[at + 1] === '\n') at++;
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else field += char;
  }
  if (field !== '' || row.length) rows.push([...row, field]);
  const [header = [], ...body] = rows.filter((found) => found.some((value) => value !== ''));
  return body.map((values) => {
    const entry: Record<string, string> = {};
    header.forEach((name, index) => {
      entry[name] = values[index] ?? '';
    });
    return entry;
  });
}
