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
import { expressionFunction, parse, View } from 'vega';
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
  type Fetch,
} from './chart';

/** The refusals and the spending of the chart running now; a worker runs one chart at a
 * time. */
let running = new Refusals();
let spending = new Spending();

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
// The functions that build strings or arrays, each within the limits and counted in the
// run's spending; registered here, they come before the interpreter's own.
const pad = expressionFunction('pad') as (...args: unknown[]) => unknown;
const truncate = expressionFunction('truncate') as (...args: unknown[]) => unknown;
type Call = (...args: never[]) => unknown;
const builders: Record<string, Call> = {
  sequence: (start: number, stop?: number, step?: number) => boundedSequence(start, stop, step),
  pad: (...args: unknown[]) => pad(...args),
  truncate: (...args: unknown[]) => truncate(...args),
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
      return spending.spend((build as (...args: unknown[]) => unknown)(...args));
    }),
  );

/** Vega's interpreter, each value it gives spent against the run's limits, so string
 * concatenation (`+`), which no function makes, is bounded too: an expression's text is
 * at most 1 KiB, so one evaluation builds little before it is checked. */
type Evaluator = (...args: unknown[]) => unknown;
type Interpreter = Record<string, (ctx: unknown, expr: unknown) => Evaluator>;
const interpreter = expressionInterpreter as unknown as Interpreter;
const spent =
  (make: string) =>
  (ctx: unknown, expr: unknown): Evaluator => {
    const evaluate = interpreter[make]!(ctx, expr);
    return (...args) => noted(() => spending.spend(evaluate(...args)));
  };
const boundedInterpreter = {
  operator: spent('operator'),
  parameter: spent('parameter'),
  event: spent('event'),
  handler: spent('handler'),
  encode(ctx: unknown, encode: { channels: Record<string, unknown> }): Evaluator {
    const evaluate = interpreter['encode']!(ctx, encode);
    return (item, ...rest) => {
      const changed = evaluate(item, ...rest);
      noted(() => {
        for (const name in encode.channels) spending.spend((item as Record<string, unknown>)[name]);
      });
      return changed;
    };
  },
};

/** A chart's SVG, before sanitizing, and its accessible name; or a `Refusal`. */
export async function chartSvg(
  source: string,
  files: ReadonlyMap<string, string>,
  read: Fetch,
): Promise<{ svg: string; name: string | null }> {
  const spec = checkedChart(source, [...files.keys()]);
  running = new Refusals();
  spending = new Spending();
  const refusals = running;
  let runtime;
  let datasets: string[];
  try {
    const compiled = compile(spec as never).spec;
    datasets = (compiled.data ?? []).map((data: { name: string }) => data.name);
    runtime = parse(compiled, undefined, { ast: true });
  } catch {
    throw new Refusal('invalid');
  }
  const view = new View(runtime, {
    expr: boundedInterpreter,
    loader: chartLoader(files, read, refusals) as never,
    renderer: 'none',
    hover: false,
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
    const svg = checkSvg(await view.toSVG());
    refusals.check();
    const name = [spec['description'], spec['title']].find((value) => typeof value === 'string');
    return { svg, name: typeof name === 'string' ? name : null };
  } catch (error) {
    throw refusals.first ?? (error instanceof Refusal ? error : new Refusal('invalid'));
  } finally {
    view.finalize();
  }
}
