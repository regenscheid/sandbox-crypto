// SPDX-License-Identifier: MIT
/**
 * A run's record as the views show it: the conversation in order, each tool call with
 * its arguments, its record and the result the model read (orchestrator, loop.py and
 * tools.py, write them). Nothing here interprets text as markup; the views show tool
 * arguments and results as plain text.
 */
import type { Json, ModelUsage, RunRecord, ToolRecord } from './api';
import { formatCost } from './format';

/** A preview's immutable full-record reference; legacy records have none. */
export function storedRecord(record: ToolRecord | undefined): {
  project: string;
  message: string;
} | null {
  const stored = record?.['_record_storage'];
  if (
    !isObject(stored) ||
    stored['format'] !== 'tool-record-v1' ||
    typeof stored['project'] !== 'string' ||
    typeof stored['message'] !== 'string' ||
    !/^msg-[0-9a-f]{16}$/.test(stored['message'])
  )
    return null;
  return { project: stored['project'], message: stored['message'] };
}

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

/**
 * A run's model calls and tokens in one line, such as "3 model calls (model-a), 1,200
 * input and 340 output tokens"; null for a record with no usage (older records). Counts
 * the gateway left out are said to be missing rather than counted as zero, and so are
 * costs; a cost the gateway did not report but priced from its model info says so.
 */
export function usageLine(usage: readonly ModelUsage[] | undefined): string | null {
  if (!usage || usage.length === 0) return null;
  const total = (key: 'input_tokens' | 'output_tokens') =>
    usage.reduce((sum, call) => sum + (call[key] ?? 0), 0).toLocaleString('en-US');
  const models = [...new Set(usage.map((call) => call.model).filter((name) => name !== null))];
  const missing = usage.some((call) => call.input_tokens === null || call.output_tokens === null);
  const calls = `${usage.length} model ${usage.length === 1 ? 'call' : 'calls'}`;
  const priced = usage.filter((call) => typeof call.cost === 'number');
  const spent = priced.reduce((sum, call) => sum + (call.cost ?? 0), 0);
  const listed = priced.filter((call) => call.cost_source === 'model_info').length;
  const cost =
    priced.length === 0
      ? ''
      : `, ${formatCost(spent)}` +
        (priced.length < usage.length ? ` for the ${priced.length} calls with a known cost` : '') +
        (listed === 0
          ? ''
          : listed === priced.length
            ? ", priced from the gateway's model info"
            : `, ${listed} of them priced from the gateway's model info`);
  return (
    `${calls}${models.length ? ` (${models.join(', ')})` : ''}, ` +
    `${total('input_tokens')} input and ${total('output_tokens')} output tokens` +
    (missing ? ', some counts not reported by the gateway' : '') +
    cost
  );
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
