// SPDX-License-Identifier: MIT
/**
 * A run's transcript as the conversation shows it: the user's messages and the agent's
 * replies, with the steps between them folded into one "worked for" group each. A step, or
 * batch, is the consecutive tool calls of one model call, between two model turns, with
 * each call's result as it arrives; each call is a row (B10, item 1): the call's purpose,
 * the agent's words, or else its tool and main argument, and its result's gloss. Background
 * work the run waits on or hears of is one summary row each (B10, item 5). The platform's
 * other bookkeeping (a run started, folds, a run that answered) stays out of the
 * conversation; the activity log has it. A run that stopped with a problem says so.
 */
import {
  answerSent,
  endedInWait,
  MESSAGE_TOOLS,
  receivedRow,
  sentFor,
  sentFromResult,
  sentRow,
  type AgentMessageRow,
  type AgentMessagesItem,
  type ChildOutcomesItem,
} from './agentMessages';
import type { RunEvent } from './api-types';
import type { BackgroundSignal, ChatEntry, Gloss } from './chat';
import type { FindingNotice } from './findingRows';
import { formatFileSize } from './format';
import { visible } from './text';

/** One tool call of a step, and its result once the run has it. */
export interface StepCall {
  id: string;
  name: string;
  arguments: string;
  /** The call's purpose, the agent's words (CM-49), when it named one. */
  purpose?: string;
  /** Whether the result is in. */
  done: boolean;
  outcome: string;
  result: string;
  /** The result at a glance, from the server; absent in events from before glosses. */
  gloss?: Gloss;
}

/** One model call that called tools: when, what it said, and its calls. */
export interface WorkStep {
  seq: number;
  at: string;
  text: string;
  calls: StepCall[];
}

/** A batch: the consecutive tool calls between two model turns, which is one step. */
export type Batch = WorkStep;

export type Pins = Extract<ChatEntry, { kind: 'user' }>['pins'];

/** What background work a summary row is about. */
export type BackgroundKind = BackgroundSignal['what'];

/**
 * Background work in one line (B10, item 5): a wait on jobs or delegated runs, from its
 * start (`from`) to its end (`to`, once `ended`), or notices of work that ended without a
 * wait (`waited` false). `finished` are the IDs known to have ended; `states` what the
 * wait's result said of each.
 */
export interface BackgroundItem {
  kind: 'background';
  seq: number;
  what: BackgroundKind;
  waited: boolean;
  mode: string | null;
  from: string;
  to: string;
  ended: boolean;
  ids: string[];
  finished: string[];
  states: Record<string, string>;
}

/**
 * One item of the transcript, told apart by its `kind`; a new kind of row (an agent's
 * message, a child's outcome, a finding) is a new member of the union.
 */
export type TranscriptItem =
  | { kind: 'user'; seq: number; at: string; text: string; pins: Pins; message: string | null }
  | { kind: 'reply'; seq: number; at: string; text: string; pins: Pins }
  | { kind: 'work'; seq: number; from: string; to: string; steps: WorkStep[] }
  | BackgroundItem
  | { kind: 'note'; seq: number; text: string; problem: boolean; message?: string }
  | FindingsItem
  | AgentMessagesItem
  | ChildOutcomesItem;

/** The findings a review or math notice offered the agent, one row each (B10, item 4). */
export interface FindingsItem {
  kind: 'findings';
  seq: number;
  at: string;
  findings: FindingNotice[];
}

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
  /** The background rows of the open group, shown right after it. */
  let later: (BackgroundItem | AgentMessagesItem | ChildOutcomesItem)[] = [];
  /** Every background row so far, the latest last, for its wait's end and notices. */
  const background: BackgroundItem[] = [];
  const flush = () => {
    if (work !== null) items.push(work, ...later);
    work = null;
    later = [];
  };
  const group = (seq: number) =>
    (work ??= { kind: 'work', seq, from: at(seq), to: at(seq), steps: [] });
  const show = (item: BackgroundItem) => {
    background.push(item);
    if (work !== null) later.push(item);
    else items.push(item);
  };
  /** A wait whose end the run never wrote has ended once the model is called again. */
  const endWaits = (seq: number) => {
    for (const item of background)
      if (item.waited && !item.ended) Object.assign(item, { ended: true, to: at(seq) });
  };
  /** The agent messages this run sent so far, for their calls' results (B10, item 2). */
  const sent: AgentMessageRow[] = [];
  /** The delegations whose outcome block is shown, each once. */
  const outcomes = new Set<string>();
  /**
   * A message or an outcome block: after the open group while one of its calls waits for
   * its result, else in order, ending the group, so that the conversation keeps its order.
   */
  const place = (item: AgentMessagesItem | ChildOutcomesItem) => {
    const open = work?.steps.some((step) => step.calls.some((call) => !call.done));
    if (open) later.push(item);
    else {
      flush();
      items.push(item);
    }
  };
  const outcome = (seq: number, ids: readonly string[]) => {
    const fresh = [...new Set(ids)].filter((id) => !outcomes.has(id));
    for (const id of fresh) outcomes.add(id);
    if (fresh.length) place({ kind: 'child-outcomes', seq, at: at(seq), delegations: fresh });
  };
  let signalled = false;
  for (const entry of entries) {
    if (entry.kind === 'note' && entry.agentMessages) {
      place({
        kind: 'agent-messages',
        seq: entry.seq,
        at: at(entry.seq),
        rows: entry.agentMessages.map(receivedRow),
      });
      signalled = false;
      continue;
    }
    if (entry.kind === 'note' && entry.background) {
      const found = entry.background;
      if (found.event === 'notice' && found.what === 'delegations') {
        // A delegation that ended is its outcome block alone: the block is shown (now, or
        // earlier), so no background row counts it too. A wait on it still marks it
        // finished on the wait's row (B10).
        outcome(entry.seq, found.ids);
        const rows = background.length;
        signal(entry.seq, at(entry.seq), found, background, show, signalled, (id) =>
          outcomes.has(id),
        );
        // The outcome block comes between this notice and the next, unless a row was made.
        signalled = background.length > rows;
        continue;
      }
      signal(entry.seq, at(entry.seq), found, background, show, signalled);
      signalled = true;
      continue;
    }
    if (entry.kind === 'tool' && MESSAGE_TOOLS.has(entry.name)) {
      const row = sentFor(sent, entry);
      if (row) answerSent(row, entry);
      else {
        const made = sentFromResult(entry);
        sent.push(made);
        place({ kind: 'agent-messages', seq: entry.seq, at: at(entry.seq), rows: [made] });
      }
      signalled = false;
      continue;
    }
    if (entry.kind === 'assistant' && entry.calls.some((call) => MESSAGE_TOOLS.has(call.name))) {
      const rows = entry.calls.filter((call) => MESSAGE_TOOLS.has(call.name)).map(sentRow);
      sent.push(...rows);
      const messages: AgentMessagesItem = {
        kind: 'agent-messages',
        seq: entry.seq,
        at: at(entry.seq),
        rows,
      };
      const rest = entry.calls.filter((call) => !MESSAGE_TOOLS.has(call.name));
      if (rest.length === 0) {
        // A model call that only sent messages: its words, then its messages, in order.
        endWaits(entry.seq);
        flush();
        if (entry.text.trim())
          items.push({
            kind: 'reply',
            seq: entry.seq,
            at: at(entry.seq),
            text: entry.text,
            pins: entry.pins,
          });
        items.push(messages);
        signalled = false;
        continue;
      }
      // Its other calls are a step of the group as before; its messages follow the group.
      endWaits(entry.seq);
      const asking = rest.some((call) => asked.has(call.id));
      const current = group(entry.seq);
      current.steps.push({
        seq: entry.seq,
        at: at(entry.seq),
        text: asking ? '' : entry.text,
        calls: rest.map((call) => ({ ...call, done: false, outcome: '', result: '' })),
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
        items.push(messages);
      } else later.push(messages);
      signalled = false;
      continue;
    }
    signalled = false;
    if (entry.kind === 'note' && entry.findings) {
      // The reviewer's findings, offered between two model calls: rows of their own.
      flush();
      items.push({ kind: 'findings', seq: entry.seq, at: at(entry.seq), findings: entry.findings });
      continue;
    }
    if (entry.kind === 'assistant') endWaits(entry.seq);
    if (entry.kind === 'user') {
      flush();
      items.push({
        kind: 'user',
        seq: entry.seq,
        at: at(entry.seq),
        text: entry.text,
        pins: entry.pins,
        message: entry.message,
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
      const result = {
        done: true,
        outcome: entry.outcome,
        result: entry.result,
        ...(entry.gloss ? { gloss: entry.gloss } : {}),
      };
      if (call) {
        Object.assign(call, result);
        if (call.purpose === undefined && entry.purpose) call.purpose = entry.purpose;
      } else
        group(entry.seq).steps.push({
          seq: entry.seq,
          at: at(entry.seq),
          text: '',
          calls: [
            {
              id: entry.call,
              name: entry.name,
              arguments: '',
              ...(entry.purpose ? { purpose: entry.purpose } : {}),
              ...result,
            },
          ],
        });
      group(entry.seq).to = at(entry.seq);
      waited(entry, background);
      outcome(entry.seq, endedInWait(entry));
    } else if (entry.kind === 'note' && entry.problem) {
      flush();
      items.push(entry);
    }
  }
  flush();
  return items;
}

/**
 * The index of the work group a live run is on: the last item, or the last work group with
 * only background rows and children's outcomes after it, which show beside what runs now;
 * -1 when there is none.
 */
export function liveWork(items: readonly TranscriptItem[]): number {
  for (let index = items.length - 1; index >= 0; index--) {
    const kind = items[index]!.kind;
    if (kind === 'work') return index;
    // A child's outcome that came while the run waited stays beside the group, as its
    // wait's row does.
    if (kind !== 'background' && kind !== 'child-outcomes') return -1;
  }
  return -1;
}

/** A wait's start or end, or a notice, as its background row has it. */
function signal(
  seq: number,
  at: string,
  found: BackgroundSignal,
  background: BackgroundItem[],
  show: (item: BackgroundItem) => void,
  /** Whether the entry before was a signal too, so that notices may share a row. */
  adjacent: boolean,
  /** IDs a notice's own row leaves out, since another row shows them (an outcome block). */
  shownElsewhere: (id: string) => boolean = () => false,
): void {
  if (found.event === 'wait') {
    const open = background.findLast(
      (item) => item.waited && !item.ended && item.what === found.what,
    );
    if (found.ids === null) {
      if (open) Object.assign(open, { ended: true, to: at });
      return;
    }
    show({
      kind: 'background',
      seq,
      what: found.what,
      waited: true,
      mode: found.mode,
      from: at,
      to: at,
      ended: false,
      ids: [...new Set(found.ids)],
      finished: [],
      states: {},
    });
    return;
  }
  // A notice of work a wait is on marks it finished there; other notices are a row of
  // their own, the latest one of their kind taking in the next while nothing comes between.
  const unclaimed: string[] = [];
  for (const id of found.ids) {
    const item = background.findLast(
      (candidate) =>
        candidate.waited && candidate.what === found.what && candidate.ids.includes(id),
    );
    if (item && found.what !== 'kernels') {
      if (!item.finished.includes(id)) item.finished.push(id);
    } else if (!shownElsewhere(id)) unclaimed.push(id);
  }
  if (!unclaimed.length) return;
  const last = background.at(-1);
  if (adjacent && last && !last.waited && last.what === found.what) {
    for (const id of unclaimed) if (!last.ids.includes(id)) last.ids.push(id);
    last.finished = found.what === 'kernels' ? [] : [...last.ids];
    last.to = at;
    return;
  }
  show({
    kind: 'background',
    seq,
    what: found.what,
    waited: false,
    mode: null,
    from: at,
    to: at,
    ended: true,
    ids: [...new Set(unclaimed)],
    finished: found.what === 'kernels' ? [] : [...unclaimed],
    states: {},
  });
}

const WAITS: Record<string, BackgroundKind> = {
  wait_jobs: 'jobs',
  wait_delegations: 'delegations',
};

/** What a wait's result says of each job or delegated run it waited on. */
function waited(entry: Extract<ChatEntry, { kind: 'tool' }>, background: BackgroundItem[]): void {
  const what = WAITS[entry.name];
  if (!what) return;
  const item = background.findLast((candidate) => candidate.waited && candidate.what === what);
  if (!item) return;
  let parsed: unknown;
  try {
    parsed = JSON.parse(entry.result);
  } catch {
    return; // a result cut to its head and tail: the notices alone say what finished
  }
  const listed = (parsed as Record<string, unknown> | null)?.[what];
  if (!Array.isArray(listed)) return;
  const key = what === 'jobs' ? 'job_id' : 'delegation_id';
  for (const one of listed) {
    const found = (one ?? {}) as Record<string, unknown>;
    const id = found[key];
    // A job says its state; a delegated run that ended says when, and how it stopped.
    const state =
      found['state'] ??
      (typeof found['ended'] === 'string' ? (found['stopped'] ?? 'ended') : undefined);
    if (typeof id !== 'string' || !item.ids.includes(id) || typeof state !== 'string') continue;
    item.states[id] = state;
    if (!RUNNING.has(state) && !item.finished.includes(id)) item.finished.push(id);
  }
}

const RUNNING = new Set(['queued', 'running', 'cancelling']);

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

/** How long a group worked, in words: "Worked for 13 min". */
export function workedFor(from: string, to: string): string {
  const seconds = Math.round((Date.parse(to) - Date.parse(from)) / 1000);
  if (!Number.isFinite(seconds) || seconds < 60) return 'Worked for less than a minute';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `Worked for ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `Worked for ${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
}

/** What a batch's calls did, by kind, for its generated header. */
type Doing = 'command' | 'read' | 'edit' | 'search' | 'ledger' | 'document' | 'delegation';

/** Each harness tool's kind, for the batch header; a tool not named here counts as a step. */
const DOING: Record<string, Doing> = {
  shell: 'command',
  python: 'command',
  sage: 'command',
  start_job: 'command',
  read_file: 'read',
  view_image: 'read',
  pdf_info: 'read',
  pdf_pages: 'read',
  pdf_render: 'read',
  write_file: 'edit',
  edit_file: 'edit',
  glob: 'search',
  grep: 'search',
  list_files: 'search',
  find_artifacts: 'search',
  history_search: 'search',
  pdf_search: 'search',
  ledger_change: 'ledger',
  ledger_category: 'ledger',
  ledger_import: 'ledger',
  ledger_proposals: 'ledger',
  memory_propose: 'ledger',
  document_create: 'document',
  document_patch: 'document',
  document_delete: 'document',
  document_comment: 'document',
  document_comment_edit: 'document',
  document_reply: 'document',
  document_resolve: 'document',
  delegate: 'delegation',
};

const plural = (count: number, one: string, many = `${one}s`) =>
  `${count} ${count === 1 ? one : many}`;

const DOING_WORDS: [Doing, (count: number) => string][] = [
  ['command', (n) => `ran ${plural(n, 'command')}`],
  ['read', (n) => `read ${plural(n, 'file')}`],
  ['edit', (n) => `edited ${plural(n, 'file')}`],
  ['search', (n) => `ran ${plural(n, 'search', 'searches')}`],
  ['ledger', (n) => `made ${plural(n, 'ledger change')}`],
  ['document', (n) => `made ${plural(n, 'document change')}`],
  ['delegation', (n) => `delegated ${plural(n, 'task')}`],
];

/** Whether a call failed, by its gloss, or else by its outcome's words. */
export function callFailed(call: Pick<StepCall, 'done' | 'outcome' | 'gloss'>): boolean {
  if (!call.done) return false;
  if (call.gloss) return !['ok', 'started'].includes(call.gloss.outcome);
  return /fail|error|timed?\s?out|refused|cancel/i.test(call.outcome);
}

/**
 * The generated header of calls, from their tools' names: "Ran 4 commands, read 3 files ·
 * 7 steps · 1 failed". A step here is one call.
 */
export function batchHeader(calls: readonly StepCall[]): string {
  const counts = new Map<Doing, number>();
  for (const call of calls) {
    const doing = DOING[call.name];
    if (doing) counts.set(doing, (counts.get(doing) ?? 0) + 1);
  }
  const said = DOING_WORDS.filter(([doing]) => counts.has(doing)).map(([doing, words]) =>
    words(counts.get(doing)!),
  );
  const failed = calls.filter(callFailed).length;
  const parts = [
    ...(said.length ? [said.join(', ').replace(/^./, (first) => first.toUpperCase())] : []),
    plural(calls.length, 'step'),
    ...(failed ? [`${failed} failed`] : []),
  ];
  return parts.join(' · ');
}

/** A group's summary over all its batches: "Ran 6 commands, edited 3 files · 9 steps". */
export function workSummary(steps: readonly WorkStep[]): string {
  return batchHeader(steps.flatMap((step) => step.calls));
}

/**
 * Each tool's main argument, which a row without a purpose shows beside the tool's name:
 * the command, the path, the pattern, the entry, the question. A tool not named here, a
 * connector's among them, shows its arguments' most telling field (`callSummary`).
 */
export const MAIN_ARGUMENT: Record<string, readonly string[]> = {
  shell: ['script'],
  python: ['code'],
  sage: ['code'],
  start_job: ['script', 'argv'],
  read_file: ['path'],
  write_file: ['path'],
  edit_file: ['path'],
  list_files: ['path'],
  view_image: ['path', 'artifact_id'],
  publish: ['path'],
  pdf_info: ['path'],
  pdf_pages: ['path'],
  pdf_render: ['path'],
  pdf_search: ['query'],
  glob: ['pattern'],
  grep: ['pattern'],
  find_artifacts: ['path', 'contains'],
  history_search: ['query'],
  history_read: ['message', 'file'],
  ledger_read: ['entry'],
  ledger_history: ['entry'],
  ledger_work: ['entry'],
  ledger_change: ['reason'],
  ledger_category: ['category'],
  ledger_import: ['path'],
  ledger_list: ['type', 'status'],
  memory_propose: ['entry', 'reason'],
  document_create: ['name'],
  document_read: ['document'],
  document_patch: ['document'],
  document_delete: ['document'],
  document_comment: ['document'],
  document_comments: ['document'],
  document_reply: ['document'],
  document_resolve: ['document'],
  show_outline: ['document'],
  ask_user: ['question'],
  delegate: ['task', 'check'],
  message_child: ['delegation_id'],
  message_parent: ['text'],
  wait_jobs: ['job_ids'],
  job_status: ['job_id'],
  cancel_job: ['job_id'],
  wait_delegations: ['delegation_ids'],
  delegation_status: ['delegation_id'],
  cancel_delegation: ['delegation_id'],
  plan_progress: ['step_id'],
  plan_create: ['reason'],
  plan_revise: ['reason'],
  plan_finish: ['reason'],
  skills: ['query', 'skill', 'names', 'action'],
  admit_hosts: ['hosts'],
  review_finding: ['statement'],
  review_respond: ['finding'],
};

/** A call's main argument, by `MAIN_ARGUMENT`, in a short line with what is unseen shown. */
export function mainArgument(call: Pick<StepCall, 'name' | 'arguments'>): string {
  const keys = MAIN_ARGUMENT[call.name];
  if (!keys) return callSummary(call);
  let parsed: unknown;
  try {
    parsed = JSON.parse(call.arguments);
  } catch {
    return firstLine(call.arguments);
  }
  const fields = (parsed ?? {}) as Record<string, unknown>;
  for (const key of keys) {
    const value = fields[key];
    if (typeof value === 'string' && value.trim()) return firstLine(value);
    if (Array.isArray(value) && value.length && value.every((item) => typeof item === 'string'))
      return firstLine(value.join(', '));
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      // A check's brief names its entry.
      const entry =
        (value as Record<string, unknown>)['entry'] ?? (value as Record<string, unknown>)['handle'];
      if (typeof entry === 'string' && entry.trim()) return firstLine(entry);
    }
  }
  return '';
}

/**
 * A row's left side: the call's purpose, the agent's own words, shown as text and framed
 * as the agent's (CM-49); or, without one, the tool's name and its main argument.
 */
export function rowLabel(
  call: Pick<StepCall, 'name' | 'arguments' | 'purpose'>,
): { says: true; text: string } | { says: false; tool: string; text: string } {
  if (call.purpose?.trim()) return { says: true, text: firstLine(call.purpose, 200) };
  return { says: false, tool: visible(call.name), text: mainArgument(call) };
}

/**
 * A row's right side, the result's gloss: "ok · 212 lines · 2 files", "failed · exit 1 ·
 * "No such file"", or "running 0:42" while the call runs in a live view.
 */
export function glossText(
  call: Pick<StepCall, 'done' | 'outcome' | 'gloss'>,
  running: string | null = null,
): string {
  if (!call.done) return running === null ? 'no result recorded' : `running ${running}`;
  const gloss = call.gloss;
  if (!gloss) return call.outcome ? visible(call.outcome) : 'done';
  const parts = [visible(gloss.outcome)];
  if (gloss.exit_code !== null && gloss.exit_code !== 0) parts.push(`exit ${gloss.exit_code}`);
  if (gloss.lines !== null) parts.push(plural(gloss.lines, 'line'));
  else if (gloss.bytes !== null)
    parts.push(gloss.bytes < 1024 ? `${gloss.bytes} B` : formatFileSize(gloss.bytes));
  const files = gloss.files.length + gloss.more_files;
  if (files) parts.push(plural(files, 'file'));
  if (gloss.error) parts.push(`"${firstLine(gloss.error, 80)}"`);
  return parts.join(' · ');
}

/** A span of seconds in words: "16 s", "4 min 2 s", "1 h 5 min". */
function span(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole} s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes} min${whole % 60 ? ` ${whole % 60} s` : ''}`;
  return `${Math.floor(minutes / 60)} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
}

/** What a background row is about, in a few words. */
export function backgroundWhat(item: Pick<BackgroundItem, 'what' | 'ids'>): string {
  if (item.what === 'jobs') return item.ids.length === 1 ? 'Background job' : 'Background jobs';
  if (item.what === 'delegations') return item.ids.length === 1 ? 'Subagent' : 'Subagents';
  return item.ids.length === 1 ? 'Kernel' : 'Kernels';
}

/**
 * A background row's line: "2 finished · 16 s" for a wait that ended, "1 running · 42 s so
 * far" for one in progress (`live`), or how many notices said what without a wait.
 */
export function backgroundText(item: BackgroundItem, now: number, live: boolean): string {
  if (item.what === 'kernels')
    return `${plural(item.ids.length, 'kernel')} started before the current packages`;
  const finished = item.ids.filter((id) => item.finished.includes(id)).length;
  const rest = item.ids.length - finished;
  const going = !item.ended && live;
  const parts = [
    ...(finished ? [`${finished} finished`] : []),
    ...(rest ? [going ? `${rest} running` : `${rest} not finished`] : []),
  ];
  const counted = parts.join(', ') || '0 finished';
  if (!item.waited) return counted;
  const seconds = ((going ? now : Date.parse(item.to)) - Date.parse(item.from)) / 1000;
  if (!Number.isFinite(seconds)) return counted;
  return `${counted} · ${span(seconds)}${going ? ' so far' : ''}`;
}

/** Each ID of a background row with its state, for the row opened. */
export function backgroundList(
  item: BackgroundItem,
  live: boolean,
): { id: string; state: string }[] {
  return item.ids.map((id) => ({
    id: visible(id),
    state: visible(
      item.states[id] ??
        (item.what === 'kernels'
          ? 'on older packages'
          : item.finished.includes(id)
            ? 'finished'
            : !item.ended && live
              ? 'running'
              : 'not finished'),
    ),
  }));
}

/** A clock time, such as 17:22, for the transcript's lines. */
export function clock(iso: string): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return '';
  return time.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** A time for a list: the clock time today, with the day before then (`Oct 17, 5:26 PM`). */
export function dayAndTime(iso: string, now: number = Date.now()): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return '';
  if (time.toDateString() === new Date(now).toDateString()) return clock(iso);
  const day = time.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(time.getFullYear() === new Date(now).getFullYear() ? {} : { year: 'numeric' }),
  });
  return `${day}, ${clock(iso)}`;
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
 * call of ``tool`` with no result yet, its arguments and its step's words. The middleware
 * keeps only a digest of the arguments, so the page shows them from here; null when the
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
