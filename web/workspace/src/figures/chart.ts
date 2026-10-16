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

/** Why a chart is not drawn, as one of the protocol's codes, with the library's message
 * when a library refused it, which the figure check reports (FG-21) and the frame does not
 * send. */
export class Refusal extends Error {
  constructor(
    readonly code: FigureCode,
    readonly detail?: string,
  ) {
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
    // A number format that pads to a width past a string's limit (review of #500, L2).
    if (key === 'format' && formatWidth(item) > MAX_FIELD) throw new Refusal('size');
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

/** An object whose values a walk reads: an array or a plain object, never a class's. */
function plain(value: object): boolean {
  const prototype: unknown = Object.getPrototypeOf(value);
  return Array.isArray(value) || prototype === Object.prototype || prototype === null;
}

/** Call `text` on each string within `value`, through its arrays and plain objects at any
 * depth, and `array` on each array; but not within an object `owned` says is accounted
 * for already (a row of the run's data, whose own transform checked it), nor again within
 * one `seen` before. */
function within(
  value: unknown,
  owned: (value: object) => boolean,
  text: (value: string) => void,
  array: (value: readonly unknown[]) => void = () => {},
  seen = new Set<object>(),
): void {
  if (typeof value === 'string') return text(value);
  if (value === null || typeof value !== 'object') return;
  const pending: object[] = [value];
  for (let next = pending.pop(); next !== undefined; next = pending.pop()) {
    if (seen.has(next) || !plain(next) || owned(next)) continue;
    seen.add(next);
    if (Array.isArray(next)) array(next);
    for (const item of Object.values(next) as unknown[])
      if (typeof item === 'string') text(item);
      else if (item !== null && typeof item === 'object') pending.push(item);
  }
}

/**
 * What a chart's run has made so far (review of #500, memory): each value an expression
 * gives, a string function or a transform's rows, spent against the run's limits, with
 * the strings and arrays nested in its arrays and plain objects (review of #500, L1). A
 * string past `MAX_FIELD`, or the run past its characters or array elements, is a
 * `Refusal` (`size`). `owned` names the objects already accounted for: the run's rows.
 */
export class Spending {
  characters = 0;
  elements = 0;

  constructor(private readonly owned: (value: object) => boolean = () => false) {}

  /** Spend `value`, and give it back: every string's characters and every array's
   * elements within it. */
  spend<T>(value: T): T {
    within(
      value,
      this.owned,
      (text) => this.text(text),
      (array) => {
        this.elements += array.length;
        if (this.elements > MAX_RUN_ELEMENTS) throw new Refusal('size');
      },
    );
    return value;
  }

  private text(value: string): void {
    if (value.length > MAX_FIELD) throw new Refusal('size');
    this.characters += value.length;
    if (this.characters > MAX_RUN_CHARACTERS) throw new Refusal('size');
  }

  /** Check the rows one transform added or changed: no string past `MAX_FIELD` in any
   * field, at any depth, and at most `MAX_RUN_ELEMENTS` elements in its fields' arrays
   * in all its output. */
  rows(...outputs: unknown[]): void {
    let elements = 0;
    const long = (text: string) => {
      if (text.length > MAX_FIELD) throw new Refusal('size');
    };
    // Rows share what they point to (a mark's items point to the mark, and it to them
    // all): each object is read once in all the output.
    const seen = new Set<object>();
    for (const tuples of outputs) {
      if (!Array.isArray(tuples)) continue;
      for (const tuple of tuples)
        if (tuple !== null && typeof tuple === 'object')
          for (const value of Object.values(tuple as Record<string, unknown>)) {
            if (Array.isArray(value) && (elements += value.length) > MAX_RUN_ELEMENTS)
              throw new Refusal('size');
            within(value, this.owned, long, undefined, seen);
          }
    }
  }
}

/** An argument a chart's function takes: a string within `MAX_FIELD`, an array within
 * `MAX_RUN_ELEMENTS`. */
export function argument(value: unknown): void {
  if (typeof value === 'string' && value.length > MAX_FIELD) throw new Refusal('size');
  if (Array.isArray(value) && value.length > MAX_RUN_ELEMENTS) throw new Refusal('size');
}

/** The width a number format's specifier pads to (d3-format's `[[fill]align][sign]
 * [symbol][0][width]…`), or 0. */
export function formatWidth(specifier: unknown): number {
  if (typeof specifier !== 'string') return 0;
  const parts = /^(?:.?[<>=^])?[+\-( ]?[$#]?0?(\d+)/.exec(specifier);
  return parts ? Number(parts[1]) : 0;
}

/** The fewest milliseconds in one step of each of Vega's time units. */
const UNIT_MILLISECONDS: Readonly<Record<string, number>> = {
  year: 365 * 86_400_000,
  quarter: 89 * 86_400_000,
  month: 28 * 86_400_000,
  week: 7 * 86_400_000,
  date: 23 * 3_600_000,
  day: 23 * 3_600_000,
  dayofyear: 23 * 3_600_000,
  hours: 3_600_000,
  minutes: 60_000,
  seconds: 1_000,
  milliseconds: 1,
};

/** At most how many characters `join` would make of `values`, counted until past the
 * limit: a nested array is joined with commas. */
function joinedLength(values: readonly unknown[], separator: string, depth = 0): number {
  let total = Math.max(0, values.length - 1) * separator.length;
  for (const value of values) {
    if (total > MAX_FIELD) break;
    if (typeof value === 'string') total += value.length;
    else if (Array.isArray(value))
      total += depth < 8 ? joinedLength(value, ',', depth + 1) : MAX_FIELD + 1;
    else if (value !== null && value !== undefined)
      total += typeof value === 'object' ? 64 : String(value).length;
  }
  return total;
}

/** At most how many characters `replace` would make: each match adds the replacement,
 * and each `$` in it up to the whole string (`$&`, `` $` ``, `$'`, a group). */
function replacedLength(value: string, pattern: unknown, replacement: unknown): number {
  const text = String(replacement);
  const each = text.length + (text.split('$').length - 1) * value.length;
  if (each === 0) return value.length;
  let matches = 0;
  if (pattern instanceof RegExp && pattern.global) {
    const search = new RegExp(pattern.source, pattern.flags);
    for (let found = search.exec(value); found !== null; found = search.exec(value)) {
      matches += 1;
      if (value.length + matches * each > MAX_FIELD) break;
      if (found[0] === '') search.lastIndex += 1;
    }
  } else matches = 1;
  return value.length + matches * each;
}

/** Milliseconds since the epoch of a date, a number or a date's text; NaN otherwise. */
function milliseconds(value: unknown): number {
  return value instanceof Date || typeof value === 'number' || typeof value === 'string'
    ? new Date(value).getTime()
    : NaN;
}

/**
 * Refuse (`size`), before it is built, what one of a chart's functions would make past
 * the limits (review of #500, L2): `pad` to a length, `join` with a separator, `truncate`
 * with an ellipsis, `replace` of many matches, a number `format` of a wide width, or a
 * `timeSequence` or `utcSequence` of more steps than `sequence` makes. Its arguments are
 * each within `argument` already.
 */
export function checkBuild(name: string, args: readonly unknown[]): void {
  const [first, second, third, fourth] = args;
  let size = 0;
  switch (name) {
    case 'pad': {
      const value = String(first);
      const fill = third === undefined ? 1 : Math.max(1, String(third).length);
      size = value.length + Math.max(0, Number(second) - value.length) * fill;
      break;
    }
    case 'truncate':
      size =
        Math.min(String(first).length, Number(second)) +
        (fourth === undefined ? 1 : String(fourth).length);
      break;
    case 'join':
      if (Array.isArray(first))
        size = joinedLength(first, second === undefined ? ',' : String(second));
      break;
    case 'replace':
      size = replacedLength(String(first), second, third);
      break;
    case 'format':
      size = formatWidth(second);
      break;
    case 'timeSequence':
    case 'utcSequence': {
      const span = Math.abs(milliseconds(third) - milliseconds(second));
      const step = (UNIT_MILLISECONDS[String(first)] ?? 1) * Math.max(1, Number(fourth) || 1);
      size = span / step <= MAX_SEQUENCE ? 0 : Infinity;
      if (Number.isNaN(span)) size = 0;
      break;
    }
  }
  if (!(size <= MAX_FIELD) && !Number.isNaN(size)) throw new Refusal('size');
}
