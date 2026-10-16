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
/** A drawn chart's text in all, in characters, and its SVG, in characters (review of #500,
 * memory): a chart within the other limits can still draw tens of megabytes of text. */
export const MAX_TEXT = 1024 * 1024;
export const MAX_SVG = 3 * 1024 * 1024;
/** What a chart's expressions may make (review of #500, memory): any one string at most
 * 64 Ki characters; at most 16 Mi characters and 1 M array elements in all its run; and
 * an expression's own text at most 1 KiB, so no one evaluation can build much before it
 * is checked. */
export const MAX_FIELD = 64 * 1024;
export const MAX_RUN_CHARACTERS = 16 * 1024 * 1024;
export const MAX_RUN_ELEMENTS = 1_000_000;
export const MAX_EXPRESSION = 1024;
/** The keys whose string values are expressions, in a Vega-Lite specification. */
const EXPRESSIONS = new Set([
  'calculate',
  'filter',
  'expr',
  'signal',
  'test',
  'labelExpr',
  'update',
]);
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
    if (EXPRESSIONS.has(key) && typeof item === 'string' && item.length > MAX_EXPRESSION)
      throw new Refusal('size');
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

/** The marks a drawn scene holds, counted to the limit, and the characters of their text,
 * counted to its limit. */
export function sceneSize(scene: unknown): { marks: number; text: number } {
  let marks = 0;
  let text = 0;
  const visit = (node: unknown): void => {
    if (!isObject(node as Json) || marks > MAX_MARKS || text > MAX_TEXT) return;
    const said = (node as { text?: unknown }).text;
    for (const line of Array.isArray(said) ? said : [said])
      if (typeof line === 'string' || typeof line === 'number') text += String(line).length;
    const items = (node as { items?: unknown }).items;
    if (Array.isArray(items)) {
      marks += items.length;
      for (const item of items) visit(item);
    }
  };
  visit(scene);
  return { marks, text };
}

/** A drawn chart within its limits after the run: its marks, their text and each
 * dataset's rows after its transforms (a `flatten` among them); or a `Refusal`. */
export function checkDrawn(scene: unknown, rows: readonly number[]): void {
  const size = sceneSize(scene);
  if (size.marks > MAX_MARKS || size.text > MAX_TEXT) throw new Refusal('size');
  if (rows.some((count) => count > MAX_ROWS)) throw new Refusal('size');
}

/** A chart's SVG within its limit, before it leaves the worker and before the page parses
 * it; or a `Refusal`. */
export function checkSvg(svg: unknown): string {
  if (typeof svg !== 'string' || svg.length > MAX_SVG) throw new Refusal('size');
  return svg;
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

/**
 * What a chart's run has made so far (review of #500, memory): each value an expression
 * gives, a string function or a transform's rows, spent against the run's limits. A
 * string past `MAX_FIELD`, or the run past its characters or array elements, is a
 * `Refusal` (`size`).
 */
export class Spending {
  characters = 0;
  elements = 0;

  /** Spend `value`, and give it back: a string's characters, an array's elements and
   * the strings among them. */
  spend<T>(value: T): T {
    if (typeof value === 'string') this.text(value);
    else if (Array.isArray(value)) {
      this.elements += value.length;
      if (this.elements > MAX_RUN_ELEMENTS) throw new Refusal('size');
      for (const item of value) if (typeof item === 'string') this.text(item);
    }
    return value;
  }

  private text(value: string): void {
    if (value.length > MAX_FIELD) throw new Refusal('size');
    this.characters += value.length;
    if (this.characters > MAX_RUN_CHARACTERS) throw new Refusal('size');
  }

  /** Check the rows one transform added or changed: no string field past `MAX_FIELD`, and
   * at most `MAX_RUN_ELEMENTS` array elements in all its output. */
  rows(...outputs: unknown[]): void {
    let elements = 0;
    for (const tuples of outputs) {
      if (!Array.isArray(tuples)) continue;
      for (const tuple of tuples)
        if (tuple !== null && typeof tuple === 'object')
          for (const value of Object.values(tuple as Record<string, unknown>)) {
            if (typeof value === 'string' && value.length > MAX_FIELD) throw new Refusal('size');
            if (Array.isArray(value) && (elements += value.length) > MAX_RUN_ELEMENTS)
              throw new Refusal('size');
          }
    }
  }
}

/** A string argument a chart's string function takes, within `MAX_FIELD`. */
export function argument(value: unknown): void {
  if (typeof value === 'string' && value.length > MAX_FIELD) throw new Refusal('size');
  if (Array.isArray(value) && value.length > MAX_RUN_ELEMENTS) throw new Refusal('size');
}
