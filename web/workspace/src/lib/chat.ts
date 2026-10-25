// SPDX-License-Identifier: MIT
/**
 * Chat with a session's main agent (docs/specs/run-service.md; the workspace's chat.py):
 * the session's messages and their states, the run that holds the session, and that run's
 * events as it writes them (RS-14), read by cursor. Every text here is the agent's or the
 * user's and is shown as text or through the Markdown renderer, never as HTML.
 */
import type {
  ChatMessage,
  EnvironmentState,
  EventPage,
  MessageAnswer,
  MessageListing,
  Question,
  QuestionListing,
  Requested,
  RunEvent,
  RunLease,
  RunEntry,
} from './api-types';
import { formatCost } from './format';
import { ApiError, get, post } from './http';
import { forgetWrite, guardedWrite } from './mutation';
import { visible } from './text';

export type { ChatMessage, RunEvent, RunLease };

/** The most messages or events one read returns (the server's `PAGE`). */
export const PAGE = 200;
/** The most pages one poll reads of messages, and of events, so a poll stays short. */
const PAGES_PER_READ = 5;

const part = encodeURIComponent;
const at = (project: string, session: string) =>
  `/projects/${part(project)}/sessions/${part(session)}`;

export const chatApi = {
  messages: (project: string, session: string, after: number, signal?: AbortSignal) =>
    get<MessageListing>(`${at(project, session)}/messages?after=${after}`, { signal }),
  post: (project: string, session: string, message: OutgoingMessage) =>
    post<MessageAnswer>(`${at(project, session)}/messages`, message),
  events: (project: string, session: string, run: string, after: number, signal?: AbortSignal) =>
    get<EventPage>(
      `${at(project, session)}/runs/${part(run)}/events?after=${after}&limit=${PAGE}`,
      { signal },
    ),
  resume: (project: string, session: string) =>
    post<Requested>(`${at(project, session)}/resume`, {}),
  cancel: (project: string, session: string, run: string) =>
    post<Requested>(`${at(project, session)}/runs/${part(run)}/cancel`, {}),
};

/**
 * Send a message through the shared save pattern (mutation.ts): its operation ID is the
 * message's ID, kept until a result, so sending the same text again after any failure
 * writes the message once (the server answers the one already written).
 */
export function sendMessage(
  project: string,
  session: string,
  text: string,
  send: (message: OutgoingMessage) => Promise<MessageAnswer> = (message) =>
    chatApi.post(project, session, message),
  answers: string | null = null,
  attachments: string[] = [],
): Promise<MessageAnswer> {
  const body = attachments.length ? { text, attachments } : { text };
  return guardedWrite('chat-message', `${project}:${session}`, body, (change) =>
    send({
      message_id: change.operation_id,
      text: change.text,
      question_id: answers,
      ...(attachments.length ? { attachments } : {}),
    }),
  );
}

/**
 * A message as the page sends it: `question_id` is the question the page showed it would
 * answer, or null when it showed none; the server refuses it when that is not so now.
 */
export interface OutgoingMessage {
  message_id: string;
  text: string;
  question_id: string | null;
  attachments?: string[];
}

/**
 * Give up a message whose sending failed: the user changed the draft. The same text
 * written later, such as a second "yes", is then a new message, not a retry of this one.
 */
export function forgetMessage(
  project: string,
  session: string,
  text: string,
  attachments: string[] = [],
): Promise<void> {
  return forgetWrite(
    'chat-message',
    `${project}:${session}`,
    attachments.length ? { text, attachments } : { text },
  );
}

/** The most a message may hold, in UTF-8 bytes (RS-4). */
export const MAX_MESSAGE_BYTES = 65536;

/** A message's size as the server counts it: UTF-8 bytes. */
export function messageBytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** A run the panel shows: its state and the events read so far. */
export interface WatchedRun {
  run: string;
  state: EventPage['state'];
  events: RunEvent[];
}

export interface ChatView {
  /** The session's messages read so far, oldest first. */
  messages: ChatMessage[];
  /** The run that holds the session now, if one does. */
  lease: RunLease | null;
  /** Whether the run service is running; without it, messages wait. */
  service: boolean;
  /**
   * The worker's record of the session's environment, which the server reads only while a
   * message waits with no run holding the session: what the message waits on. Null when
   * it is not asked, or the worker does not say.
   */
  environment: EnvironmentState | null;
  /** The runs shown, oldest first; the last is the current one (RS-16). */
  runs: WatchedRun[];
  /** The session's latest questions to the user, open and closed, oldest first. */
  questions: Question[];
}

/** What one poll needs from the server, for one session. */
export interface ChatReads {
  messages(after: number, signal?: AbortSignal): Promise<MessageListing>;
  events(run: string, after: number, signal?: AbortSignal): Promise<EventPage>;
  /** The session's latest questions to the user, or with `open` its open ones only;
   * without it, the view has none. */
  questions?(open: boolean, signal?: AbortSignal): Promise<QuestionListing>;
  /** The session's runs; its main ones are shown too, so a run no message names, such as
   * a resumed or continued one, stays in the conversation after the next one starts. */
  runs?(signal?: AbortSignal): Promise<readonly RunEntry[]>;
}

/** Polls between readings of the session's runs, besides when the lease changes. */
const RUNS_EVERY = 4;

/** Who writes the session's automatic continuations of an open plan (auto_continuation.py). */
export const CONTINUATION_AUTHOR = 'research-supervisor';

/** The continuation's own words (auto_continuation.py), which a resumed run repeats without
 * naming its message. */
const CONTINUATION_TEXT =
  /^The active research plan \S+ version \d+ remains open after the previous root answered\./;

/**
 * Whether a run's input is the platform's automatic continuation rather than the user's:
 * a message its author wrote, or one in its words. The chat folds it to "Continuation".
 */
export function isContinuation(
  text: string,
  message: string | null,
  continuations: ReadonlySet<string>,
): boolean {
  return (message !== null && continuations.has(message)) || CONTINUATION_TEXT.test(text.trim());
}

/** A run name's start time, as the service writes it: ``20261022T014900864922Z-run-…``. */
const STARTED = /^\d{8}T\d{6}\d*Z-/;

/** The most runs the panel shows; earlier ones are in the session's run list. */
export const MAX_RUNS = 5;

/** Every this many polls, held messages are read again (another tab may resume them). */
export const HELD_EVERY = 10;
/** Polls a run that lost its lease without ending is still read, for its `stopped` event. */
export const INTERRUPTED_POLLS = 3;

/** A message no run will change by itself: delivered, refused, or held until resumed. */
const settled = (message: ChatMessage) => message.state !== 'queued';

/**
 * A session's chat as one poll reads it (`Poller`, poller.ts). Each read adds what is new:
 * messages from the oldest queued one, and the events of every run shown that may still
 * write some. The runs shown are those the messages name (the run that last offered each,
 * so a cancelled or interrupted run stays) and the one holding the session, oldest first,
 * at most `MAX_RUNS`: a run that ends between two polls is read to its end before the next
 * one is shown below it. A run is not asked again once it has ended and its events are
 * read, or once it no longer holds the session without having ended (interrupted).
 */
export class ChatFeed {
  #messages = new Map<string, ChatMessage>();
  #runs = new Map<string, WatchedRun>();
  /** Runs whose events are all read and that will write no more. */
  #finished = new Set<string>();
  /** Polls left for a run that lost its lease without ending: the service writes its
   * `stopped` event once it reaps the run. */
  #grace = new Map<string, number>();
  #lease: RunLease | null = null;
  /** The session's main runs, as last read (``ChatReads.runs``). */
  #roots: string[] = [];
  #service = false;
  #environment: EnvironmentState | null = null;
  #questions: Question[] = [];
  /** The open questions at the last whole read, by ID; null before the first. */
  #openSeen: string | null = null;
  /** Read held messages again: a resume, or a new message, queues them. */
  #recheck = false;
  #polls = 0;

  constructor(private readonly reads: ChatReads) {}

  /** Read held messages again at the next poll (after Resume, or a message sent). */
  recheck(): void {
    this.#recheck = true;
  }

  async read(signal?: AbortSignal): Promise<ChatView> {
    // Held messages are read again now and then too: another tab may have resumed them.
    if (++this.#polls % HELD_EVERY === 0) this.#recheck = true;
    let after = this.#messageCursor();
    this.#recheck = false;
    // The server answers the environment for the page that holds a queued message.
    let environment: EnvironmentState | null = null;
    const leased = this.#lease?.run ?? null;
    for (let page = 0; page < PAGES_PER_READ; page++) {
      const listing = await this.reads.messages(after, signal);
      this.#lease = listing.run;
      this.#service = listing.service;
      environment ??= listing.environment;
      for (const message of listing.messages) this.#messages.set(message.message_id, message);
      if (listing.messages.length < PAGE) break;
      after = listing.messages.at(-1)!.number;
    }
    this.#environment = environment;
    if (this.reads.questions) await this.#readQuestions(this.reads.questions, signal);
    if (
      this.reads.runs &&
      (this.#polls % RUNS_EVERY === 1 || (this.#lease?.run ?? null) !== leased)
    ) {
      // Only more runs to show: a failed or odd answer keeps the last list, and never
      // stops the conversation, which the messages and the lease carry on their own.
      try {
        const runs: unknown = await this.reads.runs(signal);
        if (Array.isArray(runs))
          this.#roots = (runs as RunEntry[])
            .filter((run) => run?.role === 'main' && run.parent === null)
            .map((run) => run.name);
      } catch (caught) {
        if (signal?.aborted) throw caught;
      }
    }
    this.#follow();
    for (const run of this.#runs.values()) {
      if (this.#finished.has(run.run)) continue;
      for (let page = 0; page < PAGES_PER_READ; page++) {
        let found: EventPage;
        try {
          found = await this.reads.events(run.run, run.events.at(-1)?.seq ?? 0, signal);
        } catch (caught) {
          // A run the session names before it has written anything is not known yet:
          // read again at the next poll, rather than end the whole chat as missing.
          if (caught instanceof ApiError && caught.status === 404) break;
          throw caught;
        }
        run.state = found.state;
        run.events = [...run.events, ...found.events];
        if (found.events.length < PAGE) {
          this.#settle(run);
          break;
        }
      }
    }
    return this.#view();
  }

  /**
   * The open questions each poll; the latest whole, closed ones too, only at the first
   * read and when the open ones changed, as when one closed, so a closed question shows
   * how it closed.
   */
  async #readQuestions(
    read: NonNullable<ChatReads['questions']>,
    signal?: AbortSignal,
  ): Promise<void> {
    const ids = (questions: Question[]) =>
      questions
        .filter((question) => question.state === 'open')
        .map((question) => question.question_id)
        .join(' ');
    const open = (await read(true, signal)).questions;
    if (this.#openSeen !== null && ids(open) === this.#openSeen) {
      const fresh = new Map(open.map((question) => [question.question_id, question]));
      this.#questions = this.#questions.map(
        (question) => fresh.get(question.question_id) ?? question,
      );
      return;
    }
    this.#questions = (await read(false, signal)).questions;
    this.#openSeen = ids(this.#questions);
  }

  /** Whether a run caught up with will write more: not once ended, nor some polls after it
   * lost its lease without ending. */
  #settle(run: WatchedRun): void {
    if (run.state === 'ended') this.#finished.add(run.run);
    else if (run.state === 'interrupted' && this.#lease?.run !== run.run) {
      const left = (this.#grace.get(run.run) ?? INTERRUPTED_POLLS) - 1;
      this.#grace.set(run.run, left);
      if (left <= 0) this.#finished.add(run.run);
    } else this.#grace.delete(run.run);
  }

  /**
   * The runs to show, rebuilt each poll: the newest `MAX_RUNS` of those the messages name
   * (the run that delivered each, or else the one that last offered it), in the order of
   * their first message, the session's main runs, and the run holding the session. Run
   * names begin with their start time, so when all do, they are shown in that order. A run
   * kept keeps its events; one older than the window is not shown again.
   */
  #follow(): void {
    const named: string[] = [];
    for (const message of [...this.#messages.values()].sort((a, b) => a.number - b.number)) {
      const run = message.run ?? message.offered;
      if (run !== null && !named.includes(run)) named.push(run);
    }
    for (const run of this.#roots) if (!named.includes(run)) named.push(run);
    if (this.#lease && !named.includes(this.#lease.run)) named.push(this.#lease.run);
    if (named.every((run) => STARTED.test(run))) named.sort();
    const shown = new Map<string, WatchedRun>();
    for (const run of named.slice(-MAX_RUNS))
      shown.set(run, this.#runs.get(run) ?? { run, state: 'active', events: [] });
    for (const run of this.#runs.keys())
      if (!shown.has(run)) {
        this.#finished.delete(run);
        this.#grace.delete(run);
      }
    this.#runs = shown;
  }

  /** From the oldest queued message (or held one, when asked to recheck them). */
  #messageCursor(): number {
    let last = 0;
    let open: number | null = null;
    for (const message of this.#messages.values()) {
      last = Math.max(last, message.number);
      const changing = !settled(message) || (this.#recheck && message.state === 'held');
      if (changing) open = Math.min(open ?? message.number, message.number);
    }
    return open === null ? last : open - 1;
  }

  #view(): ChatView {
    const messages = [...this.#messages.values()].sort((a, b) => a.number - b.number);
    const runs = [...this.#runs.values()].map((run) => ({ ...run, events: [...run.events] }));
    return {
      messages,
      lease: this.#lease,
      service: this.#service,
      environment: this.#environment,
      runs,
      questions: [...this.#questions],
    };
  }
}

/** One line of the transcript, from a run's events. */
export type ChatEntry =
  | { kind: 'started'; seq: number; text: string }
  | {
      kind: 'user';
      seq: number;
      text: string;
      pins: Record<string, unknown>[];
      /** The message the run was given, when it names one. */
      message: string | null;
    }
  | {
      kind: 'assistant';
      seq: number;
      text: string;
      /** The text's references to recorded files, as the event pins them (FG-7, F2). */
      pins: Record<string, unknown>[];
      /**
       * The tools this model call called; ``id`` is the call's, which a question names, and
       * ``purpose`` the call's purpose when it named one: the agent's words (CM-49).
       */
      calls: { id: string; name: string; arguments: string; purpose?: string }[];
      /** This model call's tokens, as the gateway reported them; null when not recorded. */
      usage: string | null;
    }
  | {
      kind: 'tool';
      seq: number;
      /** The call this result answers, by its ID; empty when the event does not say. */
      call: string;
      name: string;
      outcome: string;
      result: string;
      /** The call's purpose, the agent's words, when the event carries it. */
      purpose?: string;
      /** The result at a glance, as the server computed it from the tool record. */
      gloss?: Gloss;
    }
  | {
      kind: 'note';
      seq: number;
      text: string;
      problem: boolean;
      message?: string;
      /** A wait on background work, or a notice of it, for the transcript's summary row. */
      background?: BackgroundSignal;
      /** The IDs of the agent messages a notice delivered (PD-44); the notice has no text. */
      agentMessages?: string[];
    };

/**
 * A tool result at a glance, as the `tool` event's `gloss` holds it: computed by the
 * server's code from the tool record, never from the model's text (B10, item 1).
 */
export interface Gloss {
  /** `ok`, `failed`, `refused`, `timed out`, `cancelled`, or `started` for a job. */
  outcome: string;
  exit_code: number | null;
  lines: number | null;
  bytes: number | null;
  /** The files the call recorded or published, at most 10, and how many more. */
  files: string[];
  more_files: number;
  /** A failure's first line, at most 200 characters. */
  error: string | null;
}

/** Background work a run waits on or hears of: what it is, and its IDs. */
export type BackgroundSignal =
  | { event: 'wait'; what: 'jobs' | 'delegations'; ids: string[] | null; mode: string | null }
  | { event: 'notice'; what: 'jobs' | 'delegations' | 'kernels'; ids: string[] };

/** The sources of notices about background work, by the run's notice source. */
const BACKGROUND_SOURCES: Record<string, 'jobs' | 'delegations' | 'kernels'> = {
  jobs: 'jobs',
  delegations: 'delegations',
  kernels: 'kernels',
};

const text = (value: unknown): string => (typeof value === 'string' ? value : '');
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const record = (value: unknown): Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

const STOPPED: Record<string, string> = {
  answered: 'The run answered.',
  turn_limit:
    'The run stopped at its turn limit; its last call was asked to report what it found. Send a message to go on.',
  runaway:
    'The run stopped: its runaway guard saw it repeat itself without progress, and its last call was asked to report what it found. Send a message to go on.',
  context_limit: 'The run stopped at its context limit.',
  budget:
    'The run stopped: the project reached its daily cost limit. What waits for the session waits until the limit allows another run.',
  run_budget:
    "The run stopped at its own limit: its part of the project's daily cost limit, or a delegated run's tokens.",
  gateway_budget:
    'The run stopped: the model gateway refused the call because its spending budget is used up. What waits for the session waits until the gateway allows calls again.',
  user_message:
    'The run stopped without an answer: a newer message of yours comes first, and the next run takes it up.',
  plan_unread:
    'The run stopped without an answer: the session plan has open steps, and the run did not read it after it changed. Send a message to go on.',
  cancelled: 'The run was cancelled. Its background jobs keep running.',
  interrupted:
    'The run was interrupted: its process ended. Its messages are held until you resume.',
  error: 'The run failed.',
};

/** What a `stopped` event says. */
export function stoppedText(data: Record<string, unknown>): string {
  const stopped = text(data['stopped']);
  const failure = text(data['failure']);
  // A reason this view has no words for yet, such as one a new release records, is shown
  // as the run recorded it.
  let said = Object.hasOwn(STOPPED, stopped)
    ? STOPPED[stopped]!
    : `The run stopped: ${visible(stopped.replaceAll('_', ' ')) || 'no reason recorded'}.`;
  // A run that folds says why it could not make room (context management, CM-43 to CM-45).
  const reason = text(data['reason']);
  if (stopped === 'context_limit' && reason) {
    const detail = text(data['detail']);
    said += ` Reason: ${visible(reason)}${detail ? `: ${visible(detail)}` : ''}.`;
  }
  // The root's turn limit asks the user; it pauses without a report round (D102).
  if (stopped === 'turn_limit' && reason === 'check_in')
    return `The research paused at its turn limit, ${visible(text(data['detail']))}. Continue or stop it in the check-in below; Resume continues it too.`;
  // What tripped the runaway guard (run service, RS-6f).
  const detail = text(data['detail']);
  if (stopped === 'runaway' && detail) said += ` It saw ${visible(detail)}.`;
  return failure ? `${said} Failure: ${visible(failure)}.` : said;
}

/** A fold of the run's view (context management, CM-46), or an overview of folds, with
 * its level (CM-21), as a line; and the folded region over or back within its budget. */
export function foldText(data: Record<string, unknown>): string {
  const state = text(data['state']);
  const span = list(data['span']).map(String);
  const positions = span.length === 2 ? ` positions ${span[0]} to ${span[1]}` : '';
  const level = typeof data['level'] === 'number' ? data['level'] : 1;
  const overview = level > 1 || list(data['parts']).length > 0;
  const what = overview ? `An overview (level ${level}) of${positions}` : `A fold of${positions}`;
  if (state === 'started') return `${what} started; its narrative is being written.`;
  if (state === 'waiting') return 'The view reached its wall: the run waits for the running fold.';
  if (state === 'waited')
    return `The view reached its wall and waited ${String(data['seconds'])} s for the running fold.`;
  if (state === 'rejected') return `${what} was rejected: ${visible(text(data['reason']))}.`;
  if (state === 'region_over_budget') {
    const budget = typeof data['budget_tokens'] === 'number' ? data['budget_tokens'] : '?';
    return `The folded region is over its budget of ${budget.toLocaleString('en-US')} tokens, with nothing left to merge.`;
  }
  if (state === 'region_within_budget') return 'The folded region is back within its budget.';
  const freed = typeof data['freed'] === 'number' ? data['freed'].toLocaleString('en-US') : '?';
  const how =
    state === 'applied'
      ? 'with its narrative'
      : state === 'code_merge'
        ? 'built by code'
        : 'skeleton-only';
  const reason = text(data['reason']);
  const kept = record(data['proposals'])['kept'];
  const proposed =
    typeof kept === 'number' && kept > 0
      ? ` It proposed ${kept} research-state ${kept === 1 ? 'update' : 'updates'} for the user to accept or dismiss.`
      : '';
  return (
    `${overview ? `Overview (level ${level}) of` : 'Folded'}${positions}, ${how}: about ${freed} tokens freed, ${String(data['stubs'] ?? 0)} results stubbed.` +
    (reason && state !== 'applied'
      ? ` ${visible(reason.replace(/^(skeleton-only|built by code): /, 'The narrative failed: '))}.`
      : '') +
    proposed
  );
}

/**
 * A tool call's outcome in a few words: its state, error kind and exit code. A tool with
 * no state to record (a plan or ledger call, a connector's tool) says nothing, not that
 * it recorded none, which read as a fault in every transcript.
 */
/** A `tool` event's gloss, or null for an event from before glosses or a malformed one. */
function glossOf(value: unknown): Gloss | null {
  const found = record(value);
  if (typeof found['outcome'] !== 'string') return null;
  const count = (field: unknown) =>
    typeof field === 'number' && Number.isInteger(field) && field >= 0 ? field : null;
  return {
    outcome: found['outcome'],
    exit_code: typeof found['exit_code'] === 'number' ? found['exit_code'] : null,
    lines: count(found['lines']),
    bytes: count(found['bytes']),
    files: list(found['files']).filter((path): path is string => typeof path === 'string'),
    more_files: count(found['more_files']) ?? 0,
    error: typeof found['error'] === 'string' ? found['error'] : null,
  };
}

function outcomeText(value: unknown): string {
  const outcome = record(value);
  const parts: string[] = [];
  const state = text(outcome['state']);
  if (state) parts.push(state);
  if (outcome['error']) parts.push(`error ${String(outcome['error'])}`);
  if (typeof outcome['exit_code'] === 'number') parts.push(`exit code ${outcome['exit_code']}`);
  return parts.join(', ');
}

/**
 * One model call's tokens, as an `assistant` event's `usage` keeps them (RS-14): the model
 * and its input and output tokens, saying when the gateway left a count out.
 */
export function tokensText(value: unknown): string | null {
  const usage = record(value);
  if (Object.keys(usage).length === 0) return null;
  const count = (key: string) =>
    typeof usage[key] === 'number' ? (usage[key] as number).toLocaleString('en-US') : 'unreported';
  const model = text(usage['model']);
  const cost = typeof usage['cost'] === 'number' ? `, ${formatCost(usage['cost'])}` : '';
  return `${model ? `${model}: ` : ''}${count('input_tokens')} input and ${count('output_tokens')} output tokens${cost}`;
}

/**
 * Where a run's questions go in its transcript: each after the entry whose model call
 * asked it (the `ask_user` call the question names), so that what came later stays below
 * it. A question whose call is not among the entries, as before its events load, is
 * `unplaced`, and is shown after the transcript.
 */
export function placeQuestions<T extends { tool_call: string }>(
  entries: readonly ChatEntry[],
  questions: readonly T[],
): { after: Map<number, T[]>; unplaced: T[] } {
  const asked = new Map<string, number>();
  for (const entry of entries)
    if (entry.kind === 'assistant')
      for (const call of entry.calls) if (call.id) asked.set(call.id, entry.seq);
  const after = new Map<number, T[]>();
  const unplaced: T[] = [];
  for (const question of questions) {
    const seq = asked.get(question.tool_call);
    if (seq === undefined) unplaced.push(question);
    else after.set(seq, [...(after.get(seq) ?? []), question]);
  }
  return { after, unplaced };
}

/**
 * The transcript of a run's events, in order. A `model` event (a model call began) shows
 * nothing itself; `thinking` says whether one is in progress.
 */
export function chatEntries(events: readonly RunEvent[]): ChatEntry[] {
  const entries: ChatEntry[] = [];
  for (const { seq, kind, data, pins = [] } of events) {
    if (kind === 'started') {
      const answering = list(data['message_ids']).length;
      // Lab 41, B4: a connector server whose listing failed is unavailable, not missing.
      const unavailable = Object.entries(record(data['connectors_unavailable'])).map(
        ([server, why]) => `${server} (${text(why)})`,
      );
      entries.push({
        kind: 'started',
        seq,
        text:
          `Run ${text(data['run_id'])} started${answering ? `, answering ${answering} ${answering === 1 ? 'message' : 'messages'}` : ''}.` +
          (unavailable.length
            ? ` Connector servers unavailable, their listing failed: ${unavailable.join('; ')}. The agent is told, and its first mcp_tools call lists them again.`
            : ''),
      });
    } else if (kind === 'user')
      entries.push({
        kind: 'user',
        seq,
        text: text(data['text']),
        pins,
        message: typeof data['message_id'] === 'string' ? data['message_id'] : null,
      });
    else if (kind === 'assistant') {
      const calls = list(data['tool_calls']).map((call) => {
        const purpose = record(call)['purpose'];
        return {
          id: text(record(call)['id']),
          name: text(record(call)['name']),
          arguments: text(record(call)['arguments']),
          ...(typeof purpose === 'string' && purpose ? { purpose } : {}),
        };
      });
      entries.push({
        kind: 'assistant',
        seq,
        text: text(data['text']),
        pins,
        calls,
        usage: tokensText(data['usage']),
      });
    } else if (kind === 'tool') {
      const purpose = data['purpose'];
      const gloss = glossOf(data['gloss']);
      entries.push({
        kind: 'tool',
        seq,
        call: text(data['call_id']),
        name: text(data['name']) || 'tool',
        outcome: outcomeText(data['outcome']),
        result: text(data['result']),
        ...(typeof purpose === 'string' && purpose ? { purpose } : {}),
        ...(gloss ? { gloss } : {}),
      });
    } else if (kind === 'notice') {
      const ids = list(data['ids']).map(String);
      // A notice for the user, such as a root's soft-cap check-in (D102), says itself.
      const said = text(data['text']);
      const what = BACKGROUND_SOURCES[text(data['source'])];
      entries.push({
        kind: 'note',
        seq,
        text: said || `Notices from ${text(data['source'])}: ${ids.join(', ')}.`,
        problem: false,
        ...(what && ids.length ? { background: { event: 'notice' as const, what, ids } } : {}),
        ...(text(data['source']) === 'agent_messages' && ids.length ? { agentMessages: ids } : {}),
      });
    } else if (kind === 'waiting' && 'permission_request' in data) {
      // A connector call held for the user's answer (D76), or the end of that wait.
      const request = data['permission_request'];
      entries.push({
        kind: 'note',
        seq,
        text:
          typeof request === 'number'
            ? `Waiting for your approval of ${visible(text(data['tool']))} on ${visible(text(data['server']))} (permission request ${request}).`
            : 'The wait for approval ended.',
        problem: false,
      });
    } else if (kind === 'waiting') {
      const jobs = list(data['job_ids']).map(String);
      const children = list(data['delegation_ids']).map(String);
      const what = 'delegation_ids' in data ? 'delegations' : 'jobs';
      const ids = what === 'delegations' ? children : jobs;
      entries.push({
        kind: 'note',
        seq,
        background: {
          event: 'wait',
          what,
          ids: ids.length ? ids : null,
          mode: typeof data['mode'] === 'string' ? data['mode'] : null,
        },
        text:
          'delegation_ids' in data
            ? children.length
              ? `Waiting for delegated work: ${children.join(', ')}.`
              : 'The wait for delegated work ended.'
            : jobs.length
              ? `Waiting for ${data['mode'] === 'all' ? 'all of' : 'the first of'} jobs ${jobs.join(', ')}.`
              : 'The wait for jobs ended.',
        problem: false,
      });
    } else if (kind === 'fold') {
      const fold = text(data['fold']);
      entries.push({
        kind: 'note',
        seq,
        text: foldText(data),
        problem: text(data['state']) === 'region_over_budget',
        // The fold record's text, read whole from the session's archive.
        ...(/^msg-[0-9a-f]{16}$/.test(fold) ? { message: fold } : {}),
      });
    } else if (kind === 'stopped') {
      const stopped = text(data['stopped']);
      entries.push({
        kind: 'note',
        seq,
        text: stoppedText(data),
        problem: stopped !== 'answered',
      });
    }
  }
  return entries;
}

/** The tool a running run waits on the user's approval for, or null. */
export function awaitingApproval(run: WatchedRun | null | undefined): string | null {
  if (run?.state !== 'active') return null;
  for (let index = run.events.length - 1; index >= 0; index--) {
    const event = run.events[index]!;
    if (event.kind !== 'waiting' || !('permission_request' in event.data)) continue;
    return typeof event.data['permission_request'] === 'number' ? text(event.data['tool']) : null;
  }
  return null;
}

/** Whether the run is in a model call now: active, and its last event began one. */
export function thinking(run: WatchedRun | null | undefined): boolean {
  return run?.state === 'active' && run.events.at(-1)?.kind === 'model';
}

/**
 * The messages to list beside the transcript: those no run has delivered, except a queued
 * one the run shown already has in its transcript (it is marked delivered only once the
 * run's record is saved, RS-9). Held and refused messages are always listed.
 */
export function pendingMessages(
  messages: readonly ChatMessage[],
  runs: readonly WatchedRun[],
): ChatMessage[] {
  const shown = new Set(
    runs.flatMap((run) =>
      run.events.filter((event) => event.kind === 'user').map((event) => event.data['message_id']),
    ),
  );
  return messages.filter(
    (message) =>
      message.state !== 'delivered' &&
      !(message.state === 'queued' && shown.has(message.message_id)),
  );
}

/**
 * What a queued message waits on, when the server can say. A project's first run waits for
 * its sandbox to be made, a minute or more, with no run to show yet, so "Queued" alone would
 * look like nothing happened. Null when no message waits for a run, one holds the session, or
 * the environment is in no state that takes time.
 */
export function startingText(
  view: Pick<ChatView, 'lease' | 'environment'>,
  waiting: readonly ChatMessage[],
): string | null {
  if (view.lease !== null || view.environment !== 'creating') return null;
  if (!waiting.some((message) => message.state === 'queued')) return null;
  return 'Starting the environment… The first run in a project waits while its sandbox is made, which can take a minute or more.';
}

/** What a message's state says. */
export function messageState(message: ChatMessage): string {
  switch (message.state) {
    case 'queued':
      return 'Queued';
    case 'delivered':
      return `Delivered to ${message.run ?? 'a run'}`;
    case 'held': {
      // The service gives a reason only when runs keep failing ("5 runs failed in a row").
      const why = message.reason ?? 'its run was cancelled or interrupted before answering it';
      return `Held: ${why}. Resume, or write again, to queue it once more`;
    }
    case 'refused':
      return message.reason === 'budget'
        ? 'Refused: the project reached its daily cost limit'
        : `Refused${message.reason ? `: ${message.reason}` : ''}`;
  }
}
