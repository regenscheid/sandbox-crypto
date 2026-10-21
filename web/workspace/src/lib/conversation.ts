// SPDX-License-Identifier: MIT
/**
 * A run's transcript as the conversation shows it: the user's messages and the agent's
 * replies, with the steps between them folded into one "worked for" group each. A step is
 * one model call that called tools, with each call's result as it arrives. The platform's
 * own bookkeeping (a run started, notices, waits, folds, a run that answered) stays out of
 * the conversation; the activity log has it. A run that stopped with a problem says so.
 */
import type { RunEvent } from './api-types';
import type { ChatEntry } from './chat';
import { visible } from './text';

/** One tool call of a step, and its result once the run has it. */
export interface StepCall {
  id: string;
  name: string;
  arguments: string;
  /** Whether the result is in. */
  done: boolean;
  outcome: string;
  result: string;
}

/** One model call that called tools: when, what it said, and its calls. */
export interface WorkStep {
  seq: number;
  at: string;
  text: string;
  calls: StepCall[];
}

export type Pins = Extract<ChatEntry, { kind: 'user' }>['pins'];

export type TranscriptItem =
  | { kind: 'user'; seq: number; at: string; text: string; pins: Pins }
  | { kind: 'reply'; seq: number; at: string; text: string; pins: Pins }
  | { kind: 'work'; seq: number; from: string; to: string; steps: WorkStep[] }
  | { kind: 'note'; seq: number; text: string; problem: boolean; message?: string };

/**
 * The transcript's items, in order. `asked` holds the IDs of the tool calls that asked the
 * user a question: a group ends at the step that asked, so that the question shows right
 * after it, and that step's text is shown as a reply rather than folded away.
 */
export function foldTranscript(
  entries: readonly ChatEntry[],
  events: readonly Pick<RunEvent, 'seq' | 'at'>[],
  asked: ReadonlySet<string> = new Set(),
): TranscriptItem[] {
  const times = new Map(events.map((event) => [event.seq, event.at]));
  const at = (seq: number) => times.get(seq) ?? '';
  const items: TranscriptItem[] = [];
  let work: Extract<TranscriptItem, { kind: 'work' }> | null = null;
  const flush = () => {
    if (work !== null) items.push(work);
    work = null;
  };
  const group = (seq: number) =>
    (work ??= { kind: 'work', seq, from: at(seq), to: at(seq), steps: [] });
  for (const entry of entries) {
    if (entry.kind === 'user') {
      flush();
      items.push({
        kind: 'user',
        seq: entry.seq,
        at: at(entry.seq),
        text: entry.text,
        pins: entry.pins,
      });
    } else if (entry.kind === 'assistant') {
      if (entry.calls.length === 0) {
        if (!entry.text.trim()) continue;
        flush();
        items.push({
          kind: 'reply',
          seq: entry.seq,
          at: at(entry.seq),
          text: entry.text,
          pins: entry.pins,
        });
        continue;
      }
      const asking = entry.calls.some((call) => asked.has(call.id));
      const current = group(entry.seq);
      current.steps.push({
        seq: entry.seq,
        at: at(entry.seq),
        text: asking ? '' : entry.text,
        calls: entry.calls.map((call) => ({ ...call, done: false, outcome: '', result: '' })),
      });
      current.to = at(entry.seq);
      if (asking) {
        flush();
        if (entry.text.trim())
          items.push({
            kind: 'reply',
            seq: entry.seq,
            at: at(entry.seq),
            text: entry.text,
            pins: entry.pins,
          });
      }
    } else if (entry.kind === 'tool') {
      const call = pending(work, entry);
      if (call) Object.assign(call, { done: true, outcome: entry.outcome, result: entry.result });
      else
        group(entry.seq).steps.push({
          seq: entry.seq,
          at: at(entry.seq),
          text: '',
          calls: [
            {
              id: entry.call,
              name: entry.name,
              arguments: '',
              done: true,
              outcome: entry.outcome,
              result: entry.result,
            },
          ],
        });
      group(entry.seq).to = at(entry.seq);
    } else if (entry.kind === 'note' && entry.problem) {
      flush();
      items.push(entry);
    }
  }
  flush();
  return items;
}

/** The call a result answers: by its ID, or else the first waiting call of its name. */
function pending(
  work: Extract<TranscriptItem, { kind: 'work' }> | null,
  entry: Extract<ChatEntry, { kind: 'tool' }>,
): StepCall | undefined {
  if (work === null) return undefined;
  const calls = work.steps.flatMap((step) => step.calls).filter((call) => !call.done);
  return (
    (entry.call ? calls.find((call) => call.id === entry.call) : undefined) ??
    calls.find((call) => call.name === entry.name)
  );
}

const SUMMARY_FIELDS = [
  'description',
  'summary',
  'task',
  'query',
  'question',
  'command',
  'path',
  'paths',
  'url',
  'entry',
  'handle',
  'name',
  'code',
  'script',
];

/** What a call does, in a short line from its arguments: their most telling field. */
export function callSummary(call: Pick<StepCall, 'name' | 'arguments'>): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(call.arguments);
  } catch {
    return firstLine(call.arguments);
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return '';
  const fields = parsed as Record<string, unknown>;
  for (const key of SUMMARY_FIELDS) {
    const value = fields[key];
    if (typeof value === 'string' && value.trim()) return firstLine(value);
    if (Array.isArray(value) && value.length && value.every((item) => typeof item === 'string'))
      return firstLine(value.join(', '));
  }
  return '';
}

/** A text's first non-empty line, at most `limit` characters, with what is unseen shown. */
export function firstLine(value: string, limit = 140): string {
  const line = value.split('\n').find((part) => part.trim()) ?? '';
  const trimmed = line.trim();
  return visible(trimmed.length > limit ? `${trimmed.slice(0, limit - 1)}…` : trimmed);
}

/** A step's line: its own words, or what its first call does. */
export function stepText(step: WorkStep): string {
  if (step.text.trim()) return firstLine(step.text, 200);
  const first = step.calls[0];
  if (!first) return '';
  const summary = callSummary(first);
  const more = step.calls.length > 1 ? ` and ${step.calls.length - 1} more` : '';
  return summary && !more
    ? `${visible(first.name)}: ${summary}`
    : `Called ${visible(first.name)}${more}`;
}

/** A step's tools: the one it called, or how many. */
export function stepTools(step: WorkStep): string {
  return step.calls.length === 1 ? visible(step.calls[0]!.name) : `${step.calls.length} calls`;
}

/** How long a group worked, in words: "Worked for 13 min". */
export function workedFor(from: string, to: string): string {
  const seconds = Math.round((Date.parse(to) - Date.parse(from)) / 1000);
  if (!Number.isFinite(seconds) || seconds < 60) return 'Worked for less than a minute';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `Worked for ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `Worked for ${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
}

/** A group's steps and its most used tools: "9 steps · shell ×6, write_file ×3". */
export function workSummary(steps: readonly WorkStep[]): string {
  const counts = new Map<string, number>();
  for (const call of steps.flatMap((step) => step.calls))
    counts.set(call.name, (counts.get(call.name) ?? 0) + 1);
  const top = [...counts]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([name, count]) => (count > 1 ? `${visible(name)} ×${count}` : visible(name)));
  const count = `${steps.length} ${steps.length === 1 ? 'step' : 'steps'}`;
  return top.length ? `${count} · ${top.join(', ')}` : count;
}

/** A clock time, such as 17:22, for the transcript's lines. */
export function clock(iso: string): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return '';
  return time.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** Minutes and seconds since a time, such as 1:52. */
export function elapsed(from: string, now: number): string {
  const seconds = Math.max(0, Math.floor((now - Date.parse(from)) / 1000));
  if (!Number.isFinite(seconds)) return '';
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
    : `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

/**
 * The call a held permission request is for, as the run's transcript has it: the latest
 * call of ``tool`` with no result yet, its arguments and its step's words. The proxy keeps
 * only a digest of the arguments, so the page shows them from here; null when the
 * transcript has no such call.
 */
export function heldCall(
  events: readonly RunEvent[],
  tool: string,
): { arguments: string; text: string } | null {
  const answered = new Set(
    events
      .filter((event) => event.kind === 'tool')
      .map((event) => String(event.data['call_id'] ?? '')),
  );
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index]!;
    if (event.kind !== 'assistant') continue;
    const calls = Array.isArray(event.data['tool_calls']) ? event.data['tool_calls'] : [];
    for (const raw of calls) {
      const call = (raw ?? {}) as Record<string, unknown>;
      if (call['name'] !== tool || answered.has(String(call['id'] ?? ''))) continue;
      return {
        arguments: typeof call['arguments'] === 'string' ? call['arguments'] : '',
        text: typeof event.data['text'] === 'string' ? event.data['text'] : '',
      };
    }
  }
  return null;
}
