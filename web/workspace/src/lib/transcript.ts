// SPDX-License-Identifier: MIT
/**
 * A run's record as the views show it: the conversation in order, each tool call with
 * its arguments, its record and the result the model read (orchestrator, loop.py and
 * tools.py, write them). Nothing here interprets text as markup; the views show tool
 * arguments and results as plain text.
 */
import type { Json, RunRecord, ToolRecord } from './api';

export interface ToolStep {
  name: string;
  /** The arguments as the model wrote them: JSON text, perhaps not valid. */
  arguments: string;
  record: ToolRecord | undefined;
  /** What the model read back. */
  result: string | undefined;
}

export type Entry =
  | { kind: 'system'; text: string }
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; tools: ToolStep[]; final: boolean };

/** The run's messages in order, with each tool call paired with its record and result. */
export function entries(run: RunRecord): Entry[] {
  const results = new Map<string, string[]>();
  for (const message of run.messages) {
    if (message.role === 'tool' && message.tool_call_id !== undefined) {
      const queue = results.get(message.tool_call_id) ?? [];
      queue.push(message.content ?? '');
      results.set(message.tool_call_id, queue);
    }
  }
  const found: Entry[] = [];
  let records = 0;
  for (const message of run.messages) {
    if (message.role === 'system' || message.role === 'user') {
      found.push({ kind: message.role, text: message.content ?? '' });
    } else if (message.role === 'assistant') {
      const tools = (message.tool_calls ?? []).map((call) => ({
        name: call.function.name,
        arguments: call.function.arguments,
        // The harness keeps one record per call, in the order the calls were made.
        record: run.tools[records++],
        result: results.get(call.id)?.shift(),
      }));
      found.push({ kind: 'assistant', text: message.content ?? '', tools, final: false });
    }
  }
  const last = found.at(-1);
  if (run.stopped === 'answered' && last?.kind === 'assistant' && last.tools.length === 0) {
    last.final = true;
  }
  return found;
}

export interface Field {
  name: string;
  text: string;
  /** Code or other text with lines of its own, shown as a block. */
  block: boolean;
}

/** A tool call's arguments, field by field; the raw text when it is not a JSON object. */
export function argumentFields(text: string): Field[] {
  const parsed = parseObject(text);
  if (parsed === null) return [{ name: 'arguments', text, block: true }];
  return Object.entries(parsed).map(([name, value]) => {
    const shown = typeof value === 'string' ? value : JSON.stringify(value);
    const block = name === 'code' || name === 'script' || shown.includes('\n');
    return { name, text: shown, block };
  });
}

export interface ResultView {
  /** A short summary, such as `succeeded, exit 0` or `error: not_found`. */
  summary: string;
  tone: 'ok' | 'failed' | 'neutral';
  error: { kind: string; message: string; traceback?: string } | null;
  /** Output and other long text, in order: stdout, stderr, outputs, file content. */
  streams: Field[];
  notices: string[];
  /** Where and how the call ran, in short lines: sandbox, isolation tier, attempt. */
  facts: string[];
  /** Whatever else the result held, as JSON. */
  rest: string | null;
  /** The result itself, when it was not a JSON object. */
  text: string | null;
}

const FAILED_STATES = new Set(['failed', 'timed_out', 'cancelled', 'lost']);
const STREAM_KEYS = ['stdout', 'stderr', 'content'];
/** Notes the harness writes for the model, shown as they are. */
const NOTE_KEYS = ['replayed', 'state_lost', 'environment_changed'];

/** Where a call ran (the harness's `ran_on`): the tier is the one in effect (D43). */
function ranOn(where: { [key: string]: Json }): string {
  const sandbox = typeof where['sandbox'] === 'string' ? `sandbox ${where['sandbox']}` : null;
  const host = typeof where['host'] === 'string' ? `host ${where['host']}` : null;
  const tier = where['isolation_tier'];
  const isolation = typeof tier === 'number' ? `isolation tier ${tier}` : 'isolation unmeasured';
  return `ran on ${[sandbox, host].filter((part) => part !== null).join(', ')}, ${isolation}`;
}

/** What the model read back from a tool call, arranged for reading. */
export function describeResult(result: string | undefined, record?: ToolRecord): ResultView {
  const view: ResultView = {
    summary: record?.error ? `error: ${record.error}` : 'done',
    tone: record?.error ? 'failed' : 'neutral',
    error: null,
    streams: [],
    notices: [],
    facts: [],
    rest: null,
    text: null,
  };
  if (result === undefined) {
    view.summary = 'no result recorded';
    return view;
  }
  const parsed = parseObject(result);
  if (parsed === null) {
    view.text = result;
    return view;
  }
  const rest: Record<string, Json> = { ...parsed };
  const error = parsed['error'];
  if (isObject(error)) {
    const kind = String(error['kind'] ?? 'error');
    view.error = {
      kind,
      message: String(error['message'] ?? ''),
      traceback: typeof error['traceback'] === 'string' ? error['traceback'] : undefined,
    };
    view.summary = `error: ${kind}`;
    view.tone = 'failed';
    delete rest['error'];
  }
  const state = parsed['state'];
  if (typeof state === 'string') {
    const exit = parsed['exit_code'];
    const exited = typeof exit === 'number';
    view.summary = exited ? `${state}, exit ${exit}` : state;
    if (view.error === null) {
      view.tone = FAILED_STATES.has(state) || (exited && exit !== 0) ? 'failed' : 'ok';
    }
    delete rest['state'];
    delete rest['exit_code'];
  }
  for (const key of STREAM_KEYS) {
    const value = parsed[key];
    if (typeof value === 'string') {
      view.streams.push({ name: key, text: value, block: true });
      delete rest[key];
    }
  }
  const outputs = parsed['outputs'];
  if (Array.isArray(outputs)) {
    outputs.forEach((output, index) => {
      view.streams.push({ name: `output ${index + 1}`, text: String(output), block: true });
    });
    delete rest['outputs'];
  }
  const notices = parsed['platform_notices'];
  if (Array.isArray(notices)) {
    view.notices = notices.map(String);
    delete rest['platform_notices'];
  }
  for (const key of NOTE_KEYS) {
    const value = parsed[key];
    if (typeof value === 'string') {
      view.notices.push(value);
      delete rest[key];
    }
  }
  const where = parsed['ran_on'];
  if (isObject(where)) {
    view.facts.push(ranOn(where));
    delete rest['ran_on'];
  }
  const kernel = parsed['kernel_generation'];
  if (typeof kernel === 'number') {
    view.facts.push(`kernel generation ${kernel}`);
    delete rest['kernel_generation'];
  }
  const attempt = parsed['attempt_id'];
  if (typeof attempt === 'string') {
    view.facts.push(`attempt ${attempt}`);
    delete rest['attempt_id'];
  }
  if (Object.keys(rest).length > 0) view.rest = JSON.stringify(rest, null, 2);
  return view;
}

function isObject(value: Json | undefined): value is { [key: string]: Json } {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseObject(text: string): { [key: string]: Json } | null {
  try {
    const parsed = JSON.parse(text) as Json;
    return isObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}
