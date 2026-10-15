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
  Requested,
  RunEvent,
  RunLease,
} from './api-types';
import { formatCost } from './format';
import { ApiError, get, post } from './http';
import { forgetWrite, guardedWrite } from './mutation';

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
  post: (project: string, session: string, message: { message_id: string; text: string }) =>
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
  send: (message: { message_id: string; text: string }) => Promise<MessageAnswer> = (message) =>
    chatApi.post(project, session, message),
): Promise<MessageAnswer> {
  return guardedWrite('chat-message', `${project}:${session}`, { text }, (change) =>
    send({ message_id: change.operation_id, text: change.text }),
  );
}

/**
 * Give up a message whose sending failed: the user changed the draft. The same text
 * written later, such as a second "yes", is then a new message, not a retry of this one.
 */
export function forgetMessage(project: string, session: string, text: string): Promise<void> {
  return forgetWrite('chat-message', `${project}:${session}`, { text });
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
}

/** What one poll needs from the server, for one session. */
export interface ChatReads {
  messages(after: number, signal?: AbortSignal): Promise<MessageListing>;
  events(run: string, after: number, signal?: AbortSignal): Promise<EventPage>;
}

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
  #service = false;
  #environment: EnvironmentState | null = null;
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
   * their first message, then the run holding the session. A run kept keeps its events;
   * one older than the window is not shown again.
   */
  #follow(): void {
    const named: string[] = [];
    for (const message of [...this.#messages.values()].sort((a, b) => a.number - b.number)) {
      const run = message.run ?? message.offered;
      if (run !== null && !named.includes(run)) named.push(run);
    }
    if (this.#lease && !named.includes(this.#lease.run)) named.push(this.#lease.run);
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
    };
  }
}

/** One line of the transcript, from a run's events. */
export type ChatEntry =
  | { kind: 'started'; seq: number; text: string }
  | { kind: 'user'; seq: number; text: string }
  | {
      kind: 'assistant';
      seq: number;
      text: string;
      calls: { name: string; arguments: string }[];
      /** This model call's tokens, as the gateway reported them; null when not recorded. */
      usage: string | null;
    }
  | { kind: 'tool'; seq: number; name: string; outcome: string; result: string }
  | { kind: 'note'; seq: number; text: string; problem: boolean };

const text = (value: unknown): string => (typeof value === 'string' ? value : '');
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const record = (value: unknown): Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

const STOPPED: Record<string, string> = {
  answered: 'The run answered.',
  turn_limit: 'The run stopped at its turn limit.',
  context_limit: 'The run stopped at its context limit.',
  budget:
    'The run stopped: the project has spent its model budget for the last 24 hours. What waits for the session waits for the budget.',
  run_budget: "The run stopped: it used its share of the project's daily model budget.",
  gateway_budget:
    'The run stopped: the model gateway refused the call because its spending budget is used up. What waits for the session waits until the gateway allows calls again.',
  user_message: 'The run ended its wait for a message.',
  cancelled: 'The run was cancelled. Its background jobs keep running.',
  interrupted:
    'The run was interrupted: its process ended. Its messages are held until you resume.',
  error: 'The run failed.',
};

/** What a `stopped` event says. */
export function stoppedText(data: Record<string, unknown>): string {
  const stopped = text(data['stopped']);
  const failure = text(data['failure']);
  const said = Object.hasOwn(STOPPED, stopped)
    ? STOPPED[stopped]!
    : `The run stopped (${stopped}).`;
  return failure ? `${said} Failure: ${failure}.` : said;
}

/** A tool call's outcome in a few words: its state, error kind and exit code. */
function outcomeText(value: unknown): string {
  const outcome = record(value);
  const parts = [text(outcome['state']) || 'no state recorded'];
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
 * The transcript of a run's events, in order. A `model` event (a model call began) shows
 * nothing itself; `thinking` says whether one is in progress.
 */
export function chatEntries(events: readonly RunEvent[]): ChatEntry[] {
  const entries: ChatEntry[] = [];
  for (const { seq, kind, data } of events) {
    if (kind === 'started') {
      const answering = list(data['message_ids']).length;
      entries.push({
        kind: 'started',
        seq,
        text: `Run ${text(data['run_id'])} started${answering ? `, answering ${answering} ${answering === 1 ? 'message' : 'messages'}` : ''}.`,
      });
    } else if (kind === 'user') entries.push({ kind: 'user', seq, text: text(data['text']) });
    else if (kind === 'assistant') {
      const calls = list(data['tool_calls']).map((call) => ({
        name: text(record(call)['name']),
        arguments: text(record(call)['arguments']),
      }));
      entries.push({
        kind: 'assistant',
        seq,
        text: text(data['text']),
        calls,
        usage: tokensText(data['usage']),
      });
    } else if (kind === 'tool')
      entries.push({
        kind: 'tool',
        seq,
        name: text(data['name']) || 'tool',
        outcome: outcomeText(data['outcome']),
        result: text(data['result']),
      });
    else if (kind === 'notice') {
      const ids = list(data['ids']).map(String).join(', ');
      entries.push({
        kind: 'note',
        seq,
        text: `Notices from ${text(data['source'])}: ${ids}.`,
        problem: false,
      });
    } else if (kind === 'waiting') {
      const jobs = list(data['job_ids']).map(String);
      entries.push({
        kind: 'note',
        seq,
        text: jobs.length
          ? `Waiting for ${data['mode'] === 'all' ? 'all of' : 'the first of'} jobs ${jobs.join(', ')}.`
          : 'The wait for jobs ended.',
        problem: false,
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
        ? "Refused: the project's model budget is spent"
        : `Refused${message.reason ? `: ${message.reason}` : ''}`;
  }
}
