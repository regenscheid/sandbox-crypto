// SPDX-License-Identifier: MIT
/**
 * A Vega-Lite chart drawn to SVG (docs/specs/figures.md, FG-15): its text checked by the
 * chart's rules (chart.ts), compiled to Vega, its expressions run by Vega's interpreter
 * (`vega-interpreter`), never by generated code, its data read only through the
 * platform's loader, at most 20,000 marks. Delimited and JSON data are read by the frame's
 * own readers, which bound their rows; Vega's delimited reader builds each row with
 * generated code, which the frame's CSP refuses. A refusal Vega would swallow, from the
 * loader, a reader or an expression's `sequence`, refuses the chart after the run (review
 * of #500, M2). It runs in the frame's worker (chartWorker.ts), which the frame ends at
 * the time limit. Apart from the page, so a test draws a chart and checks that no code
 * was generated.
 */
import * as vega from 'vega';
import { expressionFunction, parse, tupleid, View } from 'vega';
import { expressionInterpreter } from 'vega-interpreter';
import { compile } from 'vega-lite';
import {
  boundedSequence,
  chartLoader,
  checkedChart,
  delimited,
  jsonRows,
  checkDrawn,
  checkSvg,
  MAX_ROWS,
  Refusal,
  Refusals,
  Spending,
  argument,
  checkBuild,
  type Fetch,
} from './chart';

/** A row of the run's data, whose transform checked what it holds. */
const owned = (value: object): boolean => tupleid(value) != null;

/** The refusals and the spending of the chart running now; a worker runs one chart at a
 * time. */
let running = new Refusals();
let spending = new Spending(owned);

/** Run `read` for Vega, noting a refusal it throws so the chart is refused after. */
function noted<T>(read: () => T): T {
  try {
    return read();
  } catch (error) {
    throw running.note(error, 'unreadable');
  }
}

type Reader = ((data: unknown, format?: { delimiter?: string; property?: string }) => unknown) & {
  responseType?: string;
};
const formats = (vega as unknown as { formats: (name: string, reader: Reader) => void }).formats;
for (const [name, delimiter] of [
  ['csv', ','],
  ['tsv', '\t'],
  ['dsv', null],
] as const) {
  const reader: Reader = (data, format) =>
    noted(() => delimited(String(data), delimiter ?? format?.delimiter ?? ','));
  reader.responseType = 'text';
  formats(name, reader);
}
const json: Reader = (data, format) =>
  noted(() => jsonRows(typeof data === 'string' ? data : JSON.stringify(data), format?.property));
json.responseType = 'text';
formats('json', json);
// Every transform's output is bounded as a data file's rows are, whatever makes them: a
// `flatten` of one row's long array, an `impute` or a `fold` (review of #500, memory).
type Step = { prototype: { transform?: (this: unknown, ...args: unknown[]) => unknown } };
for (const step of Object.values(
  (vega as unknown as { transforms: Record<string, Step> }).transforms,
)) {
  const own = step?.prototype?.transform;
  if (typeof own !== 'function') continue;
  step.prototype.transform = function (this: unknown, ...args: unknown[]) {
    const out = own.apply(this, args) as { add?: unknown; mod?: unknown } | undefined;
    if (Array.isArray(out?.add) && out.add.length > MAX_ROWS)
      throw running.note(new Refusal('size'));
    // No string field past its limit, nor too many array elements, in what it made.
    noted(() => spending.rows(out?.add, out?.mod));
    return out;
  };
}
// The functions that build strings or arrays, each refused before it builds past the
// limits (checkBuild), and what it built counted in the run's spending; registered here,
// they come before the interpreter's own.
type Call = (...args: never[]) => unknown;
const vegas = (name: string) => expressionFunction(name) as (...args: unknown[]) => unknown;
const [pad, truncate, timeSequence, utcSequence] = [
  'pad',
  'truncate',
  'timeSequence',
  'utcSequence',
].map(vegas);
const builders: Record<string, Call> = {
  sequence: (start: number, stop?: number, step?: number) => boundedSequence(start, stop, step),
  pad: (...args: unknown[]) => pad!(...args),
  truncate: (...args: unknown[]) => truncate!(...args),
  timeSequence: (...args: unknown[]) => timeSequence!(...args),
  utcSequence: (...args: unknown[]) => utcSequence!(...args),
  btoa: (value: unknown) => btoa(String(value)),
  atob: (value: unknown) => atob(String(value)),
  encodeURIComponent: (value: unknown) => encodeURIComponent(String(value)),
  upper: (value: unknown) => String(value).toUpperCase(),
  lower: (value: unknown) => String(value).toLowerCase(),
  trim: (value: unknown) => String(value).trim(),
  substring: (value: unknown, ...args: [number, number?]) => String(value).substring(...args),
  replace: (value: unknown, ...args: [string | RegExp, string]) => String(value).replace(...args),
  split: (value: unknown, ...args: [string | RegExp, number?]) => String(value).split(...args),
  slice: (value: string | unknown[], ...args: [number?, number?]) => value.slice(...args),
  join: (value: unknown[], separator?: string) => value.join(separator),
  reverse: (value: unknown[]) => value.slice().reverse(),
};
for (const [name, build] of Object.entries(builders))
  expressionFunction(name, (...args: unknown[]) =>
    noted(() => {
      args.forEach(argument);
      checkBuild(name, args);
      return spending.spend((build as (...args: unknown[]) => unknown)(...args));
    }),
  );
// The interpreter's own functions that read a string, registered so that the checks
// below see their arguments too (review of #500, L1): a long one is refused before it is
// read, which would flatten a string built by concatenation.
const readers: Record<string, Call> = {
  length: (value: { length: unknown }) => value.length,
  parseFloat: (value: string) => parseFloat(value),
  parseInt: (value: string, radix?: number) => parseInt(value, radix),
  regexp: (pattern: string, flags?: string) => new RegExp(pattern, flags),
  test: (pattern: string | RegExp, value: unknown) => new RegExp(pattern).test(String(value)),
};
for (const [name, read] of Object.entries(readers))
  if (expressionFunction(name) === undefined) expressionFunction(name, read);

/** The functions an expression calls, each checked first: every argument within
 * `argument`, and what it would build within `checkBuild` (review of #500, L1 and L2). */
const checked = new WeakMap<object, object>();
function checking(functions: object): object {
  let proxy = checked.get(functions);
  if (proxy !== undefined) return proxy;
  const wrapped = new Map<PropertyKey, [unknown, unknown]>();
  proxy = new Proxy(functions, {
    get(target, name, receiver) {
      const value: unknown = Reflect.get(target, name, receiver);
      if (typeof value !== 'function') return value;
      const known = wrapped.get(name);
      if (known?.[0] === value) return known[1];
      const call = function (this: unknown, ...args: unknown[]): unknown {
        noted(() => {
          args.forEach(argument);
          checkBuild(String(name), args);
        });
        return (value as (...args: unknown[]) => unknown).apply(this, args);
      };
      wrapped.set(name, [value, call]);
      return call;
    },
  });
  checked.set(functions, proxy);
  return proxy;
}
/** A parse context whose functions are checked. */
const bounded = (ctx: unknown): unknown => {
  const { functions } = ctx as { functions: object };
  return Object.create(ctx as object, { functions: { value: checking(functions) } });
};

/** Vega's interpreter, each value it gives spent against the run's limits, so string
 * concatenation (`+`), which no function makes, is bounded too: an expression's text is
 * at most 1 KiB, so one evaluation builds little before it is checked. */
type Evaluator = (...args: unknown[]) => unknown;
type Interpreter = Record<string, (ctx: unknown, expr: unknown) => Evaluator>;
const interpreter = expressionInterpreter as unknown as Interpreter;
const spent =
  (make: string) =>
  (ctx: unknown, expr: unknown): Evaluator => {
    const evaluate = interpreter[make]!(bounded(ctx), expr);
    return (...args) => noted(() => spending.spend(evaluate(...args)));
  };
const boundedInterpreter = {
  operator: spent('operator'),
  parameter: spent('parameter'),
  event: spent('event'),
  handler: spent('handler'),
  encode(ctx: unknown, encode: { channels: Record<string, unknown> }): Evaluator {
    const evaluate = interpreter['encode']!(bounded(ctx), encode);
    return (item, ...rest) => {
      const changed = evaluate(item, ...rest);
      noted(() => {
        for (const name in encode.channels) spending.spend((item as Record<string, unknown>)[name]);
      });
      return changed;
    };
  },
};

/** A logger that says nothing: Vega-Lite's warnings and Vega's errors echo a chart's own
 * text, and the refusals noted here carry no use for a stack trace; the check runs with
 * bounded output, and the frame has no console to fill (security review of #513, M2). */
interface Quiet {
  level(): Quiet;
  error(): Quiet;
  warn(): Quiet;
  info(): Quiet;
  debug(): Quiet;
}
const quiet: Quiet = {
  level: () => quiet,
  error: () => quiet,
  warn: () => quiet,
  info: () => quiet,
  debug: () => quiet,
};

/** A library's message, for the figure check (FG-21). */
const said = (error: unknown): string | undefined =>
  error instanceof Error ? error.message.slice(0, 500) : undefined;

/** A chart's SVG, before sanitizing, and its accessible name; or a `Refusal`. The figure
 * check (FG-19) also looks at the run's view, with `inspect`, before it is finalized. */
export async function chartSvg(
  source: string,
  files: ReadonlyMap<string, string>,
  read: Fetch,
  inspect?: (view: View, datasets: readonly string[]) => void,
): Promise<{ svg: string; name: string | null }> {
  const spec = checkedChart(source, [...files.keys()]);
  running = new Refusals();
  spending = new Spending(owned);
  const refusals = running;
  let runtime;
  let datasets: string[];
  try {
    const compiled = compile(spec as never, { logger: quiet as never }).spec;
    datasets = (compiled.data ?? []).map((data: { name: string }) => data.name);
    runtime = parse(compiled, undefined, { ast: true });
  } catch (error) {
    throw new Refusal('invalid', said(error));
  }
  const view = new View(runtime, {
    expr: boundedInterpreter,
    loader: chartLoader(files, read, refusals) as never,
    renderer: 'none',
    hover: false,
    logger: quiet as never,
    logLevel: vega.None,
  });
  try {
    await view.runAsync();
    refusals.check();
    const scene = view.scenegraph() as unknown as { root: unknown };
    const rows = datasets.map((name) => {
      const values: unknown = view.data(name);
      return Array.isArray(values) ? values.length : 0;
    });
    checkDrawn(scene.root, rows);
    inspect?.(view, datasets);
    const svg = checkSvg(await view.toSVG());
    refusals.check();
    const name = [spec['description'], spec['title']].find((value) => typeof value === 'string');
    return { svg, name: typeof name === 'string' ? name : null };
  } catch (error) {
    throw (
      refusals.first ?? (error instanceof Refusal ? error : new Refusal('invalid', said(error)))
    );
  } finally {
    view.finalize();
  }
}
