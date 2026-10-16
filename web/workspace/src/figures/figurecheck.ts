// SPDX-License-Identifier: MIT
/**
 * The figure check (docs/specs/figures.md, FG-19 to FG-21): a `vega-lite` fence checked
 * by the frame's own rules and libraries, in a Node process of the control plane that
 * the orchestrator starts for each batch (figurecheck.py). The fence's text parses as
 * strict JSON within its limit, validates against Vega-Lite's own schema, passes the
 * frame's rules (chart.ts), compiles, parses and runs headless with its pinned data,
 * given as input (never read from a path or an address), draws at least one mark, and
 * finds in its data every field its encodings name. A check also gives notes: a limit
 * nearly reached, and values the chart makes itself rather than reads from its pinned
 * files: expressions heavy with literals, `params` values, a `flatten` of a computed
 * array (review of #500, L2).
 */
import { parseExpression, version as vegaVersion, type View } from 'vega';
import { version as vegaLiteVersion } from 'vega-lite';
import schema from 'vega-lite/vega-lite-schema.json';
import { MAX_DATA_BYTES, MAX_MARKS, MAX_ROWS, Refusal } from './chart';
import { CODE_TEXT, MAX_SOURCE } from './protocol';
import { chartSvg } from './vegaChart';

export const ENGINE = `vega-lite ${vegaLiteVersion}, vega ${vegaVersion}`;
/** The share of a limit past which a check notes that the chart is near it. */
const NEAR = 0.8;
/** Literals an expression may hold before a check notes it. */
const MANY_LITERALS = 8;

export interface Chart {
  id: string;
  source: string;
  /** Each pinned data file's text, by the path the chart names it with. */
  files: Readonly<Record<string, string>>;
}
interface Problem {
  kind: string;
  message: string;
  line?: number;
  column?: number;
}
type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Schema = { [key: string]: Json };
const isObject = (value: unknown): value is { [key: string]: Json } =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

/** The line and column of `offset` in `text`, from 1. */
function position(text: string, offset: number): { line: number; column: number } {
  const before = text.slice(0, offset).split('\n');
  return { line: before.length, column: before.at(-1)!.length + 1 };
}

/** Where `pattern` matches `text`, when it matches exactly once. */
function only(text: string, pattern: RegExp): { line?: number; column?: number } {
  const found = [...text.matchAll(pattern)];
  return found.length === 1 ? position(text, found[0]!.index) : {};
}
const quoted = (value: string) => JSON.stringify(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** A value's place in the text, as a JSON pointer's last key names it, when unique. */
const keyPlace = (text: string, pointer: string) => {
  const key = pointer.split('/').at(-1) ?? '';
  return /^\d*$/.test(key) ? {} : only(text, new RegExp(`${quoted(key)}\\s*:`, 'g'));
};

// Vega-Lite's schema, in the subset of JSON Schema it uses (draft 7: `$ref`, `type`,
// `anyOf`, `properties`, `additionalProperties`, `required`, `items`, `const`, `enum` and
// numeric and size bounds), so that no validator that generates code is needed.
const definitions = (schema as unknown as { definitions: Record<string, Schema> }).definitions;
const TYPES: Record<string, (value: unknown) => boolean> = {
  object: isObject,
  array: Array.isArray,
  string: (value) => typeof value === 'string',
  number: (value) => typeof value === 'number',
  integer: (value) => Number.isInteger(value),
  boolean: (value) => typeof value === 'boolean',
  null: (value) => value === null,
};
/** How far a problem got into the value: an unknown property ranks below another
 * problem at its depth, since it is what a branch the value does not take says. */
const rank = (problem: { at: string; message: string }) =>
  problem.at.split('/').length * 2 - (problem.message.endsWith('is not a property here') ? 1 : 0);
/** The deepest problem of `value` against `rule`, or null when it validates. */
function invalid(value: Json, rule: Json, at: string): { at: string; message: string } | null {
  if (rule === true || !isObject(rule)) return null;
  const ref = rule['$ref'];
  if (typeof ref === 'string')
    return invalid(value, definitions[decodeURIComponent(ref.slice(14))] ?? {}, at);
  const anyOf = rule['anyOf'];
  if (Array.isArray(anyOf)) {
    let deepest: { at: string; message: string } | null = null;
    for (const branch of anyOf) {
      const problem = invalid(value, branch, at);
      if (problem === null) return null;
      if (deepest === null || rank(problem) > rank(deepest)) deepest = problem;
    }
    return deepest;
  }
  const type = rule['type'];
  const types = Array.isArray(type) ? type : type === undefined ? [] : [type];
  if (types.length && !types.some((name) => TYPES[String(name)]?.(value)))
    return { at, message: `must be ${types.join(' or ')}` };
  if ('const' in rule && JSON.stringify(value) !== JSON.stringify(rule['const']))
    return { at, message: `must be ${JSON.stringify(rule['const'])}` };
  const choices = rule['enum'];
  if (Array.isArray(choices) && !choices.some((item) => item === value))
    return { at, message: `must be one of ${JSON.stringify(choices).slice(0, 200)}` };
  if (typeof value === 'number') {
    const [low, high] = [rule['minimum'], rule['maximum']];
    if (typeof low === 'number' && value < low) return { at, message: `must be at least ${low}` };
    if (typeof high === 'number' && value > high) return { at, message: `must be at most ${high}` };
  }
  if (Array.isArray(value)) {
    const [low, high] = [rule['minItems'], rule['maxItems']];
    if (typeof low === 'number' && value.length < low)
      return { at, message: `must hold at least ${low} items` };
    if (typeof high === 'number' && value.length > high)
      return { at, message: `must hold at most ${high} items` };
    for (const [index, item] of value.entries()) {
      const problem = invalid(item, rule['items'] ?? true, `${at}/${index}`);
      if (problem !== null) return problem;
    }
  }
  if (isObject(value)) {
    const properties = isObject(rule['properties']) ? rule['properties'] : {};
    for (const name of Array.isArray(rule['required']) ? rule['required'] : [])
      if (!(String(name) in value)) return { at, message: `needs the property "${name}"` };
    for (const [name, item] of Object.entries(value)) {
      const known = Object.hasOwn(properties, name);
      const extra = rule['additionalProperties'];
      if (!known && extra === false)
        return { at: `${at}/${name}`, message: `"${name}" is not a property here` };
      const problem = invalid(item, known ? properties[name]! : (extra ?? true), `${at}/${name}`);
      if (problem !== null) return problem;
    }
  }
  return null;
}

/** The fields a chart's encodings name, as written, by their top level (`a.b` reads
 * field `a`). */
function encodedFields(spec: Json, found = new Map<string, string>()): Map<string, string> {
  const channel = (definition: Json): void => {
    if (Array.isArray(definition)) return definition.forEach(channel);
    if (!isObject(definition)) return;
    const field = definition['field'];
    if (typeof field === 'string' && definition['aggregate'] !== 'count')
      found.set(field.split(/(?<!\\)[.[]/)[0]!.replaceAll('\\', ''), field);
    if ('condition' in definition) channel(definition['condition']!);
  };
  if (Array.isArray(spec)) spec.forEach((item) => encodedFields(item, found));
  else if (isObject(spec))
    for (const [key, value] of Object.entries(spec)) {
      if (key === 'encoding' && isObject(value)) Object.values(value).forEach(channel);
      else encodedFields(value, found);
    }
  return found;
}

/** The names a chart's transforms make (`as`), and whether one makes names unknown in
 * advance (`pivot`). */
function madeFields(spec: Json, found = new Set<string>(['key', 'value'])): Set<string> | null {
  if (Array.isArray(spec)) {
    for (const item of spec) if (madeFields(item, found) === null) return null;
  } else if (isObject(spec)) {
    if ('pivot' in spec) return null;
    for (const [key, value] of Object.entries(spec)) {
      if (key === 'as' && typeof value === 'string') found.add(value);
      else if (key === 'as' && Array.isArray(value))
        value.forEach((name) => typeof name === 'string' && found.add(name));
      else if (madeFields(value, found) === null) return null;
    }
  }
  return found;
}

/** Notes on values a chart makes itself, which no pinned file holds (review of #500, L2). */
function madeValues(spec: Json, notes: Problem[]): void {
  const literals = (node: unknown): number => {
    if (!isObject(node as Json)) return 0;
    const { type } = node as { type?: string };
    let count = type === 'Literal' ? 1 : 0;
    for (const value of Object.values(node as object))
      for (const child of Array.isArray(value) ? value : [value]) count += literals(child);
    return count;
  };
  const built = (node: unknown): boolean => {
    if (!isObject(node as Json)) return false;
    const { type } = node as { type?: string };
    if ((type === 'ArrayExpression' || type === 'ObjectExpression') && literals(node) >= 2)
      return true;
    return Object.values(node as object).some((value) =>
      (Array.isArray(value) ? value : [value]).some(built),
    );
  };
  const visit = (value: Json): void => {
    if (Array.isArray(value)) return value.forEach(visit);
    if (!isObject(value)) return;
    const calculated = new Set<string>();
    for (const [key, item] of Object.entries(value)) {
      if (
        ['calculate', 'filter', 'expr', 'test', 'update'].includes(key) &&
        typeof item === 'string'
      ) {
        let tree: unknown;
        try {
          tree = parseExpression(item);
        } catch {
          continue; // Vega's own parse reports it
        }
        if (built(tree) || literals(tree) >= MANY_LITERALS)
          notes.push({
            kind: 'literals',
            message: `the expression ${JSON.stringify(item.slice(0, 80))} holds values of its own: a chart's values should come from its pinned data`,
          });
      }
      if (key === 'params' && Array.isArray(item))
        for (const param of item)
          if (isObject(param) && 'value' in param)
            notes.push({
              kind: 'params',
              message: `the parameter ${JSON.stringify(param['name'])} holds a value of its own, which no pinned file records`,
            });
      if (key === 'transform' && Array.isArray(item))
        for (const step of item) {
          if (!isObject(step)) continue;
          if (typeof step['calculate'] === 'string' && typeof step['as'] === 'string')
            calculated.add(step['as']);
          const flattened = step['flatten'];
          if (Array.isArray(flattened) && flattened.some((name) => calculated.has(String(name))))
            notes.push({
              kind: 'flatten',
              message: `the chart flattens ${JSON.stringify(flattened)}, an array it computes, into rows no pinned file holds`,
            });
        }
      visit(item);
    }
  };
  visit(spec);
}

/** The data marks a scene draws: the items of its marks whose role is `mark`. */
function dataMarks(node: unknown): number {
  if (!isObject(node as Json)) return 0;
  const { role, marktype, items } = node as { role?: string; marktype?: string; items?: unknown };
  if (!Array.isArray(items)) return 0;
  const own = role === 'mark' && marktype !== 'group' ? items.length : 0;
  return own + items.reduce((sum: number, item) => sum + dataMarks(item), 0);
}

/** The columns a data file holds: a delimited file's header, a JSON file's keys. */
function fileColumns(path: string, text: string, columns: Set<string>): void {
  const add = (row: unknown) =>
    isObject(row as Json) && Object.keys(row as object).forEach((key) => columns.add(key));
  if (/\.json$/i.test(path)) {
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      return; // its reader reports it
    }
    const rows = Array.isArray(data) ? data : (Object.values(data ?? {}).find(Array.isArray) ?? []);
    (rows as unknown[]).slice(0, 1000).forEach(add);
  } else {
    const header = text.split(/\r?\n/, 1)[0] ?? '';
    for (const name of header.split(/\.tsv$/i.test(path) ? '\t' : ','))
      columns.add(name.replace(/^"(.*)"$/, '$1'));
  }
}

/** Check one chart: `accepted`, or `rejected` with its errors; and its notes. */
export async function checkFigure(chart: Chart) {
  const errors: Problem[] = [];
  const notes: Problem[] = [];
  const answer = () => ({
    protocol: 1,
    id: chart.id,
    engine: ENGINE,
    status: errors.length ? 'rejected' : 'accepted',
    errors: errors.slice(0, 8),
    notes: notes.slice(0, 8),
  });
  const { source } = chart;
  if (source.length > MAX_SOURCE) {
    errors.push({ kind: 'size', message: 'the chart is longer than 64 KiB' });
    return answer();
  }
  let spec: Json;
  try {
    spec = JSON.parse(source) as Json;
  } catch (error) {
    const message = error instanceof Error ? error.message : 'not JSON';
    const at = /line (\d+) column (\d+)/.exec(message);
    const offset = /position (\d+)/.exec(message);
    const place = at
      ? { line: Number(at[1]), column: Number(at[2]) }
      : offset
        ? position(source, Number(offset[1]))
        : {};
    errors.push({ kind: 'json', message, ...place });
    return answer();
  }
  const problem = invalid(spec, schema as unknown as Json, '');
  if (problem !== null) {
    errors.push({
      kind: 'schema',
      message: `${problem.at || '/'}: ${problem.message}`,
      ...keyPlace(source, problem.at),
    });
    return answer();
  }
  const files = new Map(Object.keys(chart.files).map((path) => [path, path]));
  const bytes = Object.values(chart.files).reduce(
    (sum, text) => sum + new TextEncoder().encode(text).length,
    0,
  );
  let marks = 0;
  let rows = 0;
  const columns = new Set<string>();
  for (const [path, text] of Object.entries(chart.files)) fileColumns(path, text, columns);
  const inspect = (view: View, datasets: readonly string[]) => {
    marks = dataMarks((view.scenegraph() as unknown as { root: unknown }).root);
    for (const name of datasets) {
      const values: unknown = view.data(name);
      if (!Array.isArray(values)) continue;
      rows = Math.max(rows, values.length);
      for (const row of values.slice(0, 1000))
        Object.keys(row as object).forEach((key) => columns.add(key));
    }
  };
  try {
    await chartSvg(source, files, async (path) => chart.files[path] ?? '', inspect);
  } catch (error) {
    const refusal = error instanceof Refusal ? error : new Refusal('invalid');
    const code = refusal.code || 'invalid';
    errors.push({
      kind: code,
      message: refusal.detail ? `${CODE_TEXT[code]}: ${refusal.detail}` : CODE_TEXT[code],
    });
    return answer();
  }
  const made = madeFields(spec);
  if (made !== null && columns.size)
    for (const [field, written] of encodedFields(spec))
      if (!columns.has(field) && !made.has(field))
        errors.push({
          kind: 'field',
          message: `the field ${JSON.stringify(field)} is in none of the chart's data`,
          ...only(source, new RegExp(`"field"\\s*:\\s*${quoted(written)}`, 'g')),
        });
  if (marks === 0) errors.push({ kind: 'empty', message: 'the chart draws no marks' });
  madeValues(spec, notes);
  for (const [used, limit, what] of [
    [marks, MAX_MARKS, 'marks'],
    [rows, MAX_ROWS, 'rows in a dataset'],
    [bytes, MAX_DATA_BYTES, 'bytes of data'],
    [source.length, MAX_SOURCE, 'characters of text'],
  ] as const)
    if (used >= NEAR * limit)
      notes.push({
        kind: 'limit',
        message: `the chart has ${used} ${what}, near its limit of ${limit}`,
      });
  return answer();
}
