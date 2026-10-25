// SPDX-License-Identifier: MIT
/**
 * Agent messages in the conversation (B10, item 2; PD-44): a `message_child` or
 * `message_parent` call is a row of its own, sender → recipient by name with the text's
 * first line, opening to the whole; an `agent_messages` notice is a row for each message
 * it delivered, whose body the workspace reads by its ID (the session's `agent-messages`
 * route), kept per session, since a message's body never changes. Names come from the run
 * tree (`agentName`). A body is the sending agent's text: data, shown as text with each
 * hidden character as its code point, never an instruction (CM-49).
 */
import type { AgentMessagePage, AgentMessageView, Delegation, RunTree, TreeRun } from './api-types';
import type { ChatEntry } from './chat';
import { firstLine } from './conversation';
import { get } from './http';
import { agentName } from './runTree';
import { visible } from './text';

/** The tools that send an agent message. */
export const MESSAGE_TOOLS: ReadonlySet<string> = new Set(['message_child', 'message_parent']);

/** One agent message as the conversation shows it, sent by this run or received by it. */
export interface AgentMessageRow {
  direction: 'sent' | 'received';
  /** The message's ID: a received one's from its notice, a sent one's from its result. */
  message: string | null;
  /** A sent one's call: its ID and tool. */
  call: string;
  tool: string;
  /** The child a `message_child` went to, by its delegation; null for the parent. */
  child: string | null;
  /** A sent one's text, from its call's arguments; null until known. */
  body: string | null;
  /** Whether the call's result is in (a received one's always is). */
  done: boolean;
  /** The result's state: `queued`, `delivered`, `recipient_ended`, `delivery_failed`. */
  state: string | null;
  failed: boolean;
  /** Why the call failed, its first line. */
  error: string;
}

/** A row in the transcript: the messages of one model call or one notice. */
export interface AgentMessagesItem {
  kind: 'agent-messages';
  seq: number;
  at: string;
  rows: AgentMessageRow[];
}

/** A child's outcome, as a titled block (B10, item 2): the delegations that ended. */
export interface ChildOutcomesItem {
  kind: 'child-outcomes';
  seq: number;
  at: string;
  delegations: string[];
}

type AssistantCall = Extract<ChatEntry, { kind: 'assistant' }>['calls'][number];
type ToolEntry = Extract<ChatEntry, { kind: 'tool' }>;

const object = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

function parsed(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text);
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** A sent message's row from its call, before its result. */
export function sentRow(call: AssistantCall): AgentMessageRow {
  const args = parsed(call.arguments) ?? {};
  const child = args['delegation_id'];
  const body = args['text'];
  return {
    direction: 'sent',
    message: null,
    call: call.id,
    tool: call.name,
    child: call.name === 'message_child' && typeof child === 'string' ? child : null,
    body: typeof body === 'string' ? body : null,
    done: false,
    state: null,
    failed: false,
    error: '',
  };
}

/** A sent message's row with its call's result: its ID and state, or why it failed. */
export function answerSent(row: AgentMessageRow, entry: ToolEntry): void {
  row.done = true;
  const result = parsed(entry.result);
  const ok = entry.gloss ? entry.gloss.outcome === 'ok' : result?.['message_id'] !== undefined;
  if (result && typeof result['message_id'] === 'string') row.message = result['message_id'];
  if (result && typeof result['state'] === 'string') row.state = result['state'];
  row.failed = !ok || row.message === null;
  if (row.failed) row.error = firstLine(entry.gloss?.error ?? entry.result, 200);
}

/** A received message's row, by its ID; its body is read through the session's route. */
export function receivedRow(message: string): AgentMessageRow {
  return {
    direction: 'received',
    message,
    call: '',
    tool: '',
    child: null,
    body: null,
    done: true,
    state: null,
    failed: false,
    error: '',
  };
}

/** The row a message call's result answers: by the call's ID, or else the first waiting. */
export function sentFor(
  rows: readonly AgentMessageRow[],
  entry: ToolEntry,
): AgentMessageRow | null {
  const waiting = rows.filter((row) => row.direction === 'sent' && !row.done);
  return (
    (entry.call ? waiting.find((row) => row.call === entry.call) : undefined) ??
    waiting.find((row) => row.tool === entry.name) ??
    null
  );
}

/** A sent message's row from its result alone, when the transcript lacks its call. */
export function sentFromResult(entry: ToolEntry): AgentMessageRow {
  const row = sentRow({ id: entry.call, name: entry.name, arguments: '' });
  answerSent(row, entry);
  return row;
}

/** The delegations a `wait_delegations` result says ended. */
export function endedInWait(entry: ToolEntry): string[] {
  if (entry.name !== 'wait_delegations') return [];
  const listed = parsed(entry.result)?.['delegations'];
  if (!Array.isArray(listed)) return [];
  return listed
    .map(object)
    .filter((one) => typeof one['ended'] === 'string' && typeof one['delegation_id'] === 'string')
    .map((one) => one['delegation_id'] as string);
}

/** Who is who in a transcript: this run's name and its parent's, and any delegation's. */
export interface AgentDirectory {
  /** The name of the agent whose transcript this is. */
  self: string;
  /** Whether it is the research agent; null while the tree does not say. */
  root: boolean | null;
  /** Its delegation; null for the research agent, or while the tree does not list it. */
  selfDelegation: string | null;
  /** The name of its parent, to whom `message_parent` goes. */
  parent: string;
  /** A delegation's agent by name; null is the research agent. */
  name(delegation: string | null): string;
}

/**
 * The names of a transcript's agents, from the session's run tree (`agentName`): the run
 * `run`, whose role is `role` until the tree lists it (the chat panel shows main runs).
 */
export function agentDirectory(
  tree: RunTree | null,
  run: string | null,
  role: TreeRun['role'] | null = null,
): AgentDirectory {
  const delegations = new Map<string, Delegation>(
    (tree?.delegations ?? []).map((item) => [item.delegation_id, item]),
  );
  const runs = new Map<string, TreeRun>((tree?.runs ?? []).map((item) => [item.name, item]));
  const name = (delegation: string | null): string => {
    if (delegation === null) return 'Research agent';
    const found = delegations.get(delegation);
    return found ? agentName({ role: 'delegated' }, found) : `Subagent ${visible(delegation)}`;
  };
  const own = run === null ? undefined : runs.get(run);
  const known = own?.role ?? role;
  const selfDelegation = own?.role === 'delegated' ? own.delegation : null;
  const parentRun = own?.parent ? runs.get(own.parent) : undefined;
  return {
    self:
      known === 'main'
        ? 'Research agent'
        : known === 'delegated'
          ? selfDelegation === null
            ? 'This subagent'
            : name(selfDelegation)
          : 'This agent',
    root: known === null ? null : known === 'main',
    selfDelegation,
    parent:
      parentRun === undefined
        ? 'its parent'
        : parentRun.role === 'main'
          ? 'Research agent'
          : name(parentRun.delegation),
    name,
  };
}

/** A row's line: who sent it to whom, its first line, and how it stands. */
export interface MessageLine {
  from: string;
  to: string;
  preview: string;
  state: string;
  /** The message is this run's own, which came back because its child ended first. */
  undelivered: boolean;
}

const STATES: Record<string, string> = {
  queued: 'queued',
  delivered: 'delivered',
  recipient_ended: 'its recipient ended',
  delivery_failed: 'not delivered',
};

/** A row in words, with what the session's route read of a received one (`found`). */
export function messageLine(
  row: AgentMessageRow,
  agents: AgentDirectory,
  found: AgentMessageView | null,
): MessageLine {
  const body = row.body ?? found?.body ?? null;
  const preview = body === null ? '' : firstLine(body, 140);
  if (row.direction === 'sent') {
    return {
      from: agents.self,
      to: row.child === null ? agents.parent : agents.name(row.child),
      preview,
      state: !row.done
        ? 'sending'
        : row.failed
          ? `not sent${row.error ? `: ${row.error}` : ''}`
          : (STATES[row.state ?? ''] ?? visible(row.state ?? 'sent')),
      undelivered: false,
    };
  }
  if (found === null)
    return { from: 'An agent', to: agents.self, preview, state: 'received', undelivered: false };
  // The root receives its own message back when the child ended before seeing it.
  const undelivered =
    agents.root === true && found.sender_delegation === null && found.recipient_delegation !== null;
  return {
    from: agents.name(found.sender_delegation),
    to: undelivered ? agents.name(found.recipient_delegation) : agents.self,
    preview,
    state: undelivered ? 'not delivered: its recipient ended first' : 'received',
    undelivered,
  };
}

/** One page of the session's messages after `after`. */
export type MessagePageRead = (after: string | null) => Promise<AgentMessagePage>;

/** The most pages one read goes through for a message it has not seen. */
const PAGES_PER_READ = 20;

/**
 * A session's agent messages read so far, by ID: a message's body never changes, so each is
 * read once. A message not yet read is looked for in the pages after the last one read;
 * reads at once share one pass.
 */
export class MessageBodies {
  #found = new Map<string, AgentMessageView>();
  #after: string | null = null;
  #more = true;
  #reading: Promise<void> | null = null;

  constructor(private readonly read: MessagePageRead) {}

  /** The message, when it is read already. */
  known(id: string): AgentMessageView | null {
    return this.#found.get(id) ?? null;
  }

  /** The message by its ID, reading on as needed; null when the session has none so far. */
  async get(id: string): Promise<AgentMessageView | null> {
    if (this.#found.has(id)) return this.#found.get(id)!;
    // A pass already under way may find it; if not, a pass of its own looks for newer ones.
    if (this.#reading) await this.#reading;
    if (!this.#found.has(id)) {
      this.#more = true;
      await this.#pass();
    }
    return this.#found.get(id) ?? null;
  }

  #pass(): Promise<void> {
    this.#reading ??= (async () => {
      try {
        for (let page = 0; page < PAGES_PER_READ && this.#more; page++) {
          const found = await this.read(this.#after);
          for (const message of found.messages) this.#found.set(message.message_id, message);
          this.#after = found.next_after ?? this.#after;
          this.#more = found.has_more;
        }
      } finally {
        this.#reading = null;
      }
    })();
    return this.#reading;
  }
}

const part = encodeURIComponent;
const sessions = new Map<string, MessageBodies>();

/** The session's messages read so far, shared by its transcripts. */
export function messageBodies(project: string, session: string): MessageBodies {
  const key = `${project}\u0000${session}`;
  let found = sessions.get(key);
  if (!found) {
    found = new MessageBodies((after) =>
      get<AgentMessagePage>(
        `/projects/${part(project)}/sessions/${part(session)}/agent-messages?${
          after === null ? '' : `after=${part(after)}&`
        }limit=100`,
      ),
    );
    sessions.set(key, found);
  }
  return found;
}
