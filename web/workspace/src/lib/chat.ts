// SPDX-License-Identifier: MIT
/**
 * Chat with a session's main agent (docs/specs/run-service.md; the workspace's chat.py):
 * the session's messages and their states, the run that holds the session, and that run's
 * events as it writes them (RS-14), read by cursor. Every text here is the agent's or the
 * user's and is shown as text or through the Markdown renderer, never as HTML.
 */
import type {
  ChatMessage,
  EventPage,
  MessageAnswer,
  MessageListing,
  Requested,
  RunEvent,
  RunLease,
} from './api-types';
import { get, post } from './http';
import { guardedWrite } from './mutation';

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

/** The run shown: the one holding the session, or the last one the messages name. */
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
  run: WatchedRun | null;
}

/** What one poll needs from the server, for one session. */
export interface ChatReads {
  messages(after: number, signal?: AbortSignal): Promise<MessageListing>;
  events(run: string, after: number, signal?: AbortSignal): Promise<EventPage>;
}

const settled = (message: ChatMessage) =>
  message.state === 'delivered' || message.state === 'refused';

/**
 * A session's chat as one poll reads it (`Poller`, poller.ts). Each read adds what is new:
 * messages from the oldest one not yet settled (a queued or held message can still change
 * state), and the watched run's events after the last one read. A run that has ended and
 * whose events are all read is not asked again.
 */
export class ChatFeed {
  #messages = new Map<string, ChatMessage>();
  #run: WatchedRun | null = null;
  /** The watched run has ended and all its events are read. */
  #finished = false;
  #lease: RunLease | null = null;
  #service = false;

  constructor(private readonly reads: ChatReads) {}

  async read(signal?: AbortSignal): Promise<ChatView> {
    let after = this.#messageCursor();
    for (let page = 0; page < PAGES_PER_READ; page++) {
      const listing = await this.reads.messages(after, signal);
      this.#lease = listing.run;
      this.#service = listing.service;
      for (const message of listing.messages) this.#messages.set(message.message_id, message);
      if (listing.messages.length < PAGE) break;
      after = listing.messages.at(-1)!.number;
    }
    // The run holding the session; else the newest a message names, which may have run
    // and ended between two polls; else the one already shown.
    const wanted = this.#lease?.run ?? this.#lastRun() ?? this.#run?.run ?? null;
    if (wanted !== null && wanted !== this.#run?.run) {
      this.#run = { run: wanted, state: 'active', events: [] };
      this.#finished = false;
    }
    const run = this.#run;
    for (let page = 0; run && !this.#finished && page < PAGES_PER_READ; page++) {
      const found = await this.reads.events(run.run, run.events.at(-1)?.seq ?? 0, signal);
      run.state = found.state;
      run.events = [...run.events, ...found.events];
      if (found.events.length < PAGE) {
        this.#finished = found.state === 'ended';
        break;
      }
    }
    return this.#view();
  }

  /** From the oldest message that may still change; every message is read once at first. */
  #messageCursor(): number {
    let last = 0;
    let open: number | null = null;
    for (const message of this.#messages.values()) {
      last = Math.max(last, message.number);
      if (!settled(message)) open = Math.min(open ?? message.number, message.number);
    }
    return open === null ? last : open - 1;
  }

  /** The run of the newest message that names one: the last conversation, after a reload. */
  #lastRun(): string | null {
    let newest: ChatMessage | null = null;
    for (const message of this.#messages.values())
      if (message.run && (!newest || message.number > newest.number)) newest = message;
    return newest?.run ?? null;
  }

  #view(): ChatView {
    const messages = [...this.#messages.values()].sort((a, b) => a.number - b.number);
    const run = this.#run && { ...this.#run, events: [...this.#run.events] };
    return { messages, lease: this.#lease, service: this.#service, run };
  }
}

/** One line of the transcript, from a run's events. */
export type ChatEntry =
  | { kind: 'started'; seq: number; text: string }
  | { kind: 'user'; seq: number; text: string }
  | { kind: 'assistant'; seq: number; text: string; calls: { name: string; arguments: string }[] }
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
      entries.push({ kind: 'assistant', seq, text: text(data['text']), calls });
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
export function thinking(run: WatchedRun | null): boolean {
  return run?.state === 'active' && run.events.at(-1)?.kind === 'model';
}

/**
 * The messages to list beside the transcript: those no run has delivered, except a queued
 * one the run shown already has in its transcript (it is marked delivered only once the
 * run's record is saved, RS-9). Held and refused messages are always listed.
 */
export function pendingMessages(
  messages: readonly ChatMessage[],
  run: WatchedRun | null,
): ChatMessage[] {
  const shown = new Set(
    (run?.events ?? [])
      .filter((event) => event.kind === 'user')
      .map((event) => event.data['message_id']),
  );
  return messages.filter(
    (message) =>
      message.state !== 'delivered' &&
      !(message.state === 'queued' && shown.has(message.message_id)),
  );
}

/** What a message's state says. */
export function messageState(message: ChatMessage): string {
  switch (message.state) {
    case 'queued':
      return 'Queued';
    case 'delivered':
      return `Delivered to ${message.run ?? 'a run'}`;
    case 'held':
      return 'Held: the run that saw it was interrupted';
    case 'refused':
      return message.reason === 'budget'
        ? "Refused: the project's model budget is spent"
        : `Refused${message.reason ? `: ${message.reason}` : ''}`;
  }
}
