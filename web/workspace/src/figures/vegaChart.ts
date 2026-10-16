// SPDX-License-Identifier: MIT
/**
 * A Vega-Lite chart drawn to SVG (docs/specs/figures.md, FG-15): its text checked by the
 * chart's rules (chart.ts), compiled to Vega, its expressions run by Vega's interpreter
 * (`vega-interpreter`), never by generated code, its data read only through the
 * platform's loader, at most 20,000 marks. Delimited data are read by the frame's own
 * reader, since Vega's builds each row with generated code, which the frame's CSP refuses.
 * Apart from the page, so a test draws a chart and checks that no code was generated.
 */
import * as vega from 'vega';
import { parse, View } from 'vega';
import { expressionInterpreter } from 'vega-interpreter';
import { compile } from 'vega-lite';
import {
  chartLoader,
  checkedChart,
  delimited,
  MAX_MARKS,
  markCount,
  Refusal,
  type Fetch,
} from './chart';

type Reader = ((data: unknown, format?: { delimiter?: string }) => object[]) & {
  responseType?: string;
};
const formats = (vega as unknown as { formats: (name: string, reader: Reader) => void }).formats;
for (const [name, delimiter] of [
  ['csv', ','],
  ['tsv', '\t'],
  ['dsv', null],
] as const) {
  const reader: Reader = (data, format) =>
    delimited(String(data), delimiter ?? format?.delimiter ?? ',');
  reader.responseType = 'text';
  formats(name, reader);
}

/** A chart's SVG, before sanitizing, and its accessible name; or a `Refusal`. */
export async function chartSvg(
  source: string,
  files: ReadonlyMap<string, string>,
  read: Fetch,
): Promise<{ svg: string; name: string | null }> {
  const spec = checkedChart(source, [...files.keys()]);
  let runtime;
  try {
    runtime = parse(compile(spec as never).spec, undefined, { ast: true });
  } catch {
    throw new Refusal('invalid');
  }
  const view = new View(runtime, {
    expr: expressionInterpreter,
    loader: chartLoader(files, read) as never,
    renderer: 'none',
    hover: false,
  });
  try {
    await view.runAsync();
    const scene = view.scenegraph() as unknown as { root: unknown };
    if (markCount(scene.root) > MAX_MARKS) throw new Refusal('size');
    const name = [spec['description'], spec['title']].find((value) => typeof value === 'string');
    return { svg: await view.toSVG(), name: typeof name === 'string' ? name : null };
  } catch (error) {
    throw error instanceof Refusal ? error : new Refusal('invalid');
  } finally {
    view.finalize();
  }
}
