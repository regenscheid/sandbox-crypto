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
import { CODES, MAX_SOURCE } from './protocol';

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
  const format = data['format'];
  const type = isObject(format) ? format['type'] : undefined;
  if (type !== undefined && !['csv', 'tsv', 'dsv', 'json'].includes(String(type)))
    throw new Refusal('invalid');
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

/** What the loader fetches a pinned file with, as the frame does. */
export type Fetch = (capability: string) => Promise<string>;

/**
 * The first refusal a chart met while Vega ran it (review of #500, M2). Vega catches what
 * its loader, its readers and its expression functions throw, says "Loading failed", and
 * draws on without the data; so each of them notes its refusal here, and the chart is
 * refused after the run.
 */
export class Refusals {
  first: Refusal | null = null;

  /** Note `error` as a refusal (anything else as `fallback`), and give it back to throw. */
  note(error: unknown, fallback: FigureCode = 'refused'): Refusal {
    const refusal = error instanceof Refusal ? error : new Refusal(fallback);
    this.first ??= refusal;
    return refusal;
  }

  /** Throw the first refusal noted, if any. */
  check(): void {
    if (this.first !== null) throw this.first;
  }
}

/**
 * The loader Vega reads with (FG-15): a data path the chart pins is read from its
 * capability; any other address, an image or a link is refused. Data are at most 8 MiB in
 * all; the readers bound each file's rows. Every refusal is noted in `refusals`.
 */
export function chartLoader(files: ReadonlyMap<string, string>, read: Fetch, refusals: Refusals) {
  let bytes = 0;
  const refuse = () => Promise.reject(refusals.note(new Refusal('address')));
  return {
    async sanitize(uri: string, options?: { context?: string }) {
      if (options?.context !== 'dataflow' || !files.has(uri))
        throw refusals.note(new Refusal('address'));
      return { href: uri };
    },
    async load(uri: string, options?: { context?: string }) {
      await this.sanitize(uri, { context: options?.context ?? 'dataflow' });
      let text: string;
      try {
        text = await read(files.get(uri)!);
      } catch (error) {
        throw refusals.note(error);
      }
      bytes += new TextEncoder().encode(text).length;
      if (bytes > MAX_DATA_BYTES) throw refusals.note(new Refusal('size'));
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
  const push = (row: string[]) => {
    if (row.some((value) => value !== '')) rows.push(row);
    // The header and at most MAX_ROWS rows, whatever ends their lines.
    if (rows.length > MAX_ROWS + 1) throw new Refusal('size');
  };
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
      push(row);
      row = [];
      field = '';
    } else field += char;
  }
  if (field !== '' || row.length) push([...row, field]);
  const [header = [], ...body] = rows;
  return body.map((values) => {
    const entry: Record<string, string> = {};
    header.forEach((name, index) => {
      entry[name] = values[index] ?? '';
    });
    return entry;
  });
}

/** A JSON file's data as Vega reads it, at `format.property` when given (a dotted path),
 * at most MAX_ROWS items (review of #500, L1). */
export function jsonRows(text: string, property?: string): unknown {
  let data: unknown = JSON.parse(text);
  for (const key of property ? property.split('.') : []) {
    data =
      data !== null && typeof data === 'object'
        ? (data as Record<string, unknown>)[key]
        : undefined;
  }
  if (Array.isArray(data) && data.length > MAX_ROWS) throw new Refusal('size');
  return data;
}

/** A `sequence(start, stop, step)` in an expression, within the bound a data `sequence`
 * has (review of #500, L2). */
export function boundedSequence(start: number, stop?: number, step = 1): number[] {
  if (stop === undefined) [start, stop] = [0, start];
  const count = step === 0 ? Infinity : Math.ceil((stop - start) / step);
  if (!(count <= MAX_SEQUENCE)) throw new Refusal('size');
  return Array.from({ length: Math.max(0, count) }, (_, index) => start + index * step);
}

/** How long the frame lets a chart's worker run before ending it (FG-17): less than the
 * page gives the frame, so the frame says why. */
export const CHART_MILLISECONDS = 4000;

/** What the frame needs of a worker. */
export interface ChartWorker {
  postMessage(message: unknown): void;
  terminate(): void;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

/**
 * A chart drawn by `worker` within `limit` milliseconds (review of #500, M1): its SVG and
 * name, or a `Refusal` with the worker's code. Past the limit, or on any answer, the
 * worker is terminated, so a chart that would run on cannot.
 */
export function drawnInWorker(
  worker: ChartWorker,
  message: { source: string; paths: string[]; capabilities: string[] },
  limit = CHART_MILLISECONDS,
): Promise<{ svg: string; name: string | null }> {
  return new Promise((resolve, reject) => {
    const finish = (outcome: () => void) => {
      clearTimeout(timer);
      worker.onmessage = worker.onerror = null;
      worker.terminate();
      outcome();
    };
    const timer = setTimeout(() => finish(() => reject(new Refusal('time'))), limit);
    worker.onerror = () => finish(() => reject(new Refusal('unreadable')));
    worker.onmessage = (event) => {
      const answer = event.data as { ok?: unknown; svg?: unknown; name?: unknown; code?: unknown };
      finish(() => {
        if (answer?.ok === true && typeof answer.svg === 'string')
          resolve({ svg: answer.svg, name: typeof answer.name === 'string' ? answer.name : null });
        else
          reject(
            new Refusal(
              CODES.includes(answer?.code as FigureCode) && answer.code !== ''
                ? (answer.code as FigureCode)
                : 'unreadable',
            ),
          );
      });
    };
    worker.postMessage(message);
  });
}
