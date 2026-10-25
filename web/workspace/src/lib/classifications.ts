// SPDX-License-Identifier: MIT
/**
 * The classification review's pure parts (docs/specs/workspace-browsers.md, CL-1 to CL-4):
 * rules in words, the editor's tool values and their checks, what an edit widens, and the
 * edited draft written back as the classification file's YAML.
 *
 * The YAML is rewritten, not regenerated: a tool block the user did not change is kept
 * line for line, and in a changed one only `class`, `reason`, `significant` and `rules`
 * are written again, in the form `classify-draft` writes them; every other line (the
 * header, `sha256`, fields this view does not know, comments) stays as the draft has it.
 * The draft's DRAFT comment is dropped. The worker checks the result as the loader
 * checks a file; the checks here only catch what would certainly fail.
 */
import type { ClassifiedTool, DraftTool, ToolRule } from './api-types';

export type EffectClass = ClassifiedTool['effect'];
export type PinState = ClassifiedTool['pin_state'];
export type SignificantKind = 'url' | 'host' | 'recipient' | 'title' | 'size' | 'query';

export const EFFECT_CLASSES: readonly EffectClass[] = [
  'read',
  'write',
  'communicate',
  'browse',
  'admin',
  'unclassified',
];
export const SIGNIFICANT_KINDS: readonly SignificantKind[] = [
  'url',
  'host',
  'recipient',
  'title',
  'size',
  'query',
];
export const MAX_REASON = 500;
const MAX_TOOL_REASON = 200;
const MAX_LINE = 160;
const ARGUMENT = /^[A-Za-z0-9_$-]{1,64}(?:\.[A-Za-z0-9_$-]{1,64}){0,7}$/;

/**
 * A rule's condition in words: `actions` present, or `profile.saveChanges` = true. A
 * rule's `equals` is JSON text (`true` and `"true"` differ), shown as it is.
 */
export function condition(rule: ToolRule): string {
  return rule.present ? `${rule.argument} present` : `${rule.argument} = ${rule.equals ?? 'null'}`;
}

/** A rule in words: "`actions` present → browse". */
export function ruleWords(rule: ToolRule): string {
  return `${condition(rule)} → ${rule.effect}`;
}

/** Significant arguments in words: "url: url, title: name". */
export function significantWords(significant: Record<string, string> | null): string {
  const entries = Object.entries(significant ?? {});
  return entries.length ? entries.map(([kind, path]) => `${kind}: ${path}`).join(', ') : '';
}

/** One tool as the editor holds it. */
export interface EditedTool {
  name: string;
  effect: EffectClass;
  reason: string;
  rules: ToolRule[];
  significant: { kind: SignificantKind; path: string }[];
}

export function editable(tool: ClassifiedTool): EditedTool {
  return {
    name: tool.name,
    effect: tool.effect,
    reason: tool.reason ?? '',
    rules: tool.rules.map((rule) => ({ ...rule })),
    significant: Object.entries(tool.significant ?? {}).map(([kind, path]) => ({
      kind: kind as SignificantKind,
      path: path ?? '',
    })),
  };
}

function key(tool: EditedTool): string {
  return JSON.stringify([
    tool.effect,
    tool.reason,
    tool.rules.map((rule) => [rule.argument, rule.present, rule.equals, rule.effect]),
    tool.significant.map((item) => [item.kind, item.path]),
  ]);
}

/** Whether the editor's values differ from the draft's for this tool. */
export function changed(tool: EditedTool, draft: ClassifiedTool): boolean {
  return key(tool) !== key(editable(draft));
}

/** A rule value (JSON text) the loader would refuse, in words; empty when it takes it. */
function valueProblem(text: string | null): string {
  if (text === null) return 'needs a value';
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return 'has a value that is not JSON';
  }
  if (typeof value === 'string')
    return /["\\]/.test(value) || value.length > 256
      ? 'has a quote or backslash, or is too long'
      : '';
  return typeof value === 'boolean' || typeof value === 'number' ? '' : 'needs a value';
}

/** What would make the loader refuse the tool's entry, in words; empty when none. */
export function problems(tool: EditedTool): string[] {
  const found: string[] = [];
  if (!tool.reason.trim()) found.push('a reason is required');
  if (tool.reason.length > MAX_TOOL_REASON)
    found.push(`a reason is at most ${MAX_TOOL_REASON} characters`);
  if (/[\r\n]/.test(tool.reason)) found.push('a reason is one line');
  for (const rule of tool.rules) {
    if (!ARGUMENT.test(rule.argument))
      found.push(`"${rule.argument}" is not an argument path (names joined by ".")`);
    if (!rule.present) {
      const wrong = valueProblem(rule.equals);
      if (wrong) found.push(`the rule on ${rule.argument} ${wrong}`);
    }
  }
  const kinds = tool.significant.map((item) => item.kind);
  if (new Set(kinds).size !== kinds.length) found.push('each significant kind once');
  for (const item of tool.significant)
    if (!ARGUMENT.test(item.path)) found.push(`"${item.path}" is not an argument path`);
  return found;
}

/**
 * Whether the edited tool would let a call pass without approval where the classification
 * in force escalates it: a `read` class where the current one is not, or a rule the
 * current classification has, to a class other than `read`, that the edit drops. Without
 * an edit, the worker's mark stands. An estimate: the worker answers the widenings.
 */
export function widens(
  tool: EditedTool,
  draft: DraftTool,
  current: ClassifiedTool | undefined,
): boolean {
  if (!changed(tool, draft)) return draft.widening;
  const before = draft.current_effect ?? current?.effect ?? 'unclassified';
  if (tool.effect === 'read' && before !== 'read') return true;
  return (current?.rules ?? []).some(
    (rule) =>
      rule.effect !== 'read' &&
      !tool.rules.some((mine) => condition(mine) === condition(rule) && mine.effect !== 'read'),
  );
}

/** The rules an accepted tool would gain and lose against the classification in force. */
export function ruleChanges(
  tool: EditedTool,
  current: ClassifiedTool | undefined,
): { added: string[]; removed: string[] } {
  const mine = tool.rules.map(ruleWords);
  const theirs = (current?.rules ?? []).map(ruleWords);
  return {
    added: mine.filter((words) => !theirs.includes(words)),
    removed: theirs.filter((words) => !mine.includes(words)),
  };
}

const quoted = (text: string) => `"${text.replace(/["\\#]/g, "'")}"`;

/** A rule's value, JSON text, as a YAML flow scalar: JSON's scalars are YAML's. */
function scalar(value: string | null): string {
  return value ?? 'null';
}

function fields(tool: EditedTool, pad: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  out.set('class', [`${pad}class: ${tool.effect}`]);
  out.set('reason', [`${pad}reason: ${quoted(tool.reason)}`]);
  out.set(
    'significant',
    tool.significant.length
      ? [
          `${pad}significant: {${tool.significant.map((item) => `${item.kind}: ${item.path}`).join(', ')}}`,
        ]
      : [],
  );
  out.set(
    'rules',
    tool.rules.length
      ? [
          `${pad}rules:`,
          ...tool.rules.map(
            (rule) =>
              `${pad}  - {argument: ${rule.argument}, ${rule.present ? 'present: true' : `equals: ${scalar(rule.equals)}`}, class: ${rule.effect}}`,
          ),
        ]
      : [],
  );
  return out;
}

const indent = (line: string) => line.length - line.trimStart().length;
const content = (line: string) => line.trim() !== '' && !line.trimStart().startsWith('#');
const KEY = /^(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([^\s"'#][^:#]*?))\s*:\s*(.*)$/;

function keyName(line: string): { name: string; rest: string } | null {
  const found = KEY.exec(line.trim());
  if (!found) return null;
  const name =
    found[1] !== undefined
      ? (JSON.parse(`"${found[1]}"`) as string)
      : found[2] !== undefined
        ? found[2].replace(/''/g, "'")
        : found[3]!;
  return { name, rest: found[4]!.replace(/\s*#.*$/, '') };
}

/** The draft's YAML with the edited tools written back, or why it cannot be. */
export function rewriteYaml(
  yaml: string,
  edits: Map<string, EditedTool>,
): { yaml: string } | { error: string } {
  let lines = yaml.split('\n');
  const marker = lines.findIndex((line) => /^# DRAFT\b/.test(line));
  if (marker >= 0) {
    let end = marker + 1;
    while (end < lines.length && lines[end]!.startsWith('#')) end++;
    lines = [...lines.slice(0, marker), ...lines.slice(end)];
  }
  if (edits.size === 0) return { yaml: lines.join('\n') };
  const start = lines.findIndex((line) => /^tools:\s*(?:#.*)?$/.test(line));
  if (start < 0) return { error: 'the draft has no tools mapping in block form to edit' };
  let end = start + 1;
  while (end < lines.length && (!content(lines[end]!) || indent(lines[end]!) > 0)) end++;
  const body = lines.slice(start + 1, end);
  const first = body.find(content);
  if (first === undefined) return { error: 'the draft lists no tools' };
  const toolIndent = indent(first);
  // Split the mapping into its tools' blocks; lines before the first key stay as they are.
  const blocks: { name: string | null; lines: string[] }[] = [{ name: null, lines: [] }];
  for (const line of body) {
    if (content(line) && indent(line) === toolIndent) {
      const found = keyName(line);
      if (found === null || found.rest !== '')
        return { error: 'a tool entry is not in block form; accept the draft unedited' };
      blocks.push({ name: found.name, lines: [line] });
    } else if (content(line) && indent(line) < toolIndent) {
      return { error: 'the tools mapping is not indented evenly' };
    } else blocks.at(-1)!.lines.push(line);
  }
  const seen = new Set<string>();
  const out: string[] = [];
  for (const block of blocks) {
    const edit = block.name === null ? undefined : edits.get(block.name);
    if (edit === undefined) {
      out.push(...block.lines);
      continue;
    }
    seen.add(edit.name);
    const [head, ...rest] = block.lines;
    const fieldLine = rest.find(content);
    const pad = ' '.repeat(fieldLine === undefined ? toolIndent + 2 : indent(fieldLine));
    const written = fields(edit, pad);
    out.push(head!);
    // Each field with its nested lines; a known field is replaced where it first stood.
    let skipping = false;
    for (const line of rest) {
      if (content(line) && indent(line) === pad.length) {
        const found = keyName(line);
        const name = found?.name ?? '';
        if (written.has(name)) {
          out.push(...written.get(name)!);
          written.delete(name);
          skipping = true;
          continue;
        }
        skipping = false;
        out.push(line);
      } else if (!skipping || (!content(line) && indent(line) < pad.length)) out.push(line);
    }
    // Fields the tool lacked go after its last line of content, before blank lines.
    let at = out.length;
    while (at > 0 && !content(out[at - 1]!)) at--;
    out.splice(at, 0, ...[...written.values()].flat());
  }
  const missing = [...edits.keys()].filter((name) => !seen.has(name));
  if (missing.length) return { error: `the draft's YAML lacks ${missing.join(', ')}` };
  const result = [...lines.slice(0, start + 1), ...out, ...lines.slice(end)];
  const wide = result.findIndex((line) => line.length > MAX_LINE);
  if (wide >= 0)
    return { error: `line ${wide + 1} would be over ${MAX_LINE} characters; shorten the reason` };
  return { yaml: result.join('\n') };
}
