// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ChatWaiting from '../views/ChatWaiting.svelte';
import type { EnvironmentState, EventPage, MessageListing } from './api-types';
import { ApiError } from './http';
import {
  ChatFeed,
  forgetMessage,
  MAX_MESSAGE_BYTES,
  HELD_EVERY,
  INTERRUPTED_POLLS,
  MAX_RUNS,
  messageBytes,
  chatEntries,
  messageState,
  PAGE,
  pendingMessages,
  sendMessage,
  startingText,
  stoppedText,
  tokensText,
  thinking,
  type ChatMessage,
  type RunEvent,
} from './chat';

/**
 * A message as the server lists it, seen by run `seen`: `run` names the run that delivered
 * it, and `offered` the one that last offered it, so a held or queued message has no `run`.
 */
const message = (
  number: number,
  state: ChatMessage['state'],
  seen: string | null = null,
): ChatMessage => ({
  message_id: `m${number}`,
  number,
  text: `text ${number}`,
  author: 'user',
  created_at: '2026-10-14T12:00:00Z',
  state,
  run: state === 'delivered' ? seen : null,
  offered: state === 'refused' ? null : seen,
  reason: null,
});
const event = (
  seq: number,
  kind: RunEvent['kind'],
  data: Record<string, unknown> = {},
): RunEvent => ({
  seq,
  at: '2026-10-14T12:00:00Z',
  kind,
  data,
});

/** A fake server: the messages and each run's events, and every read it was asked. */
function server() {
  const state = {
    messages: [] as ChatMessage[],
    lease: null as string | null,
    service: true,
    environment: null as EnvironmentState | null,
    events: new Map<string, RunEvent[]>(),
    runState: new Map<string, EventPage['state']>(),
  };
  const asked: string[] = [];
  const reads = {
    messages: async (after: number): Promise<MessageListing> => {
      asked.push(`messages ${after}`);
      return {
        messages: state.messages.filter((item) => item.number > after).slice(0, PAGE),
        run: state.lease ? { run: state.lease, started_at: '', renewed_at: '' } : null,
        service: state.service,
        environment: state.environment,
      };
    },
    events: async (run: string, after: number): Promise<EventPage> => {
      asked.push(`events ${run} ${after}`);
      return {
        state: state.runState.get(run) ?? 'active',
        events: (state.events.get(run) ?? []).filter((item) => item.seq > after).slice(0, PAGE),
      };
    },
  };
  return { state, asked, feed: new ChatFeed(reads) };
}

describe("a session's chat, polled", () => {
  it('reads every message once, then again only from the oldest that may still change', async () => {
    const { state, asked, feed } = server();
    state.messages = [message(1, 'delivered', 'r1'), message(2, 'queued'), message(3, 'refused')];
    state.lease = 'r1';
    await feed.read();
    state.messages[1] = message(2, 'delivered', 'r1');
    const view = await feed.read();
    expect(asked.filter((item) => item.startsWith('messages'))).toEqual([
      'messages 0',
      'messages 1',
    ]);
    expect(view.messages.map((item) => item.state)).toEqual(['delivered', 'delivered', 'refused']);
    await feed.read();
    expect(asked.at(-2)).toBe('messages 3'); // all settled: only newer ones
  });

  it("carries the worker's word on the environment while a message waits for its run", async () => {
    const { state, feed } = server();
    state.messages = [message(1, 'queued')];
    state.environment = 'creating';
    expect((await feed.read()).environment).toBe('creating');
    state.environment = null; // the server asks no more once a run holds the session
    state.lease = 'r1';
    expect((await feed.read()).environment).toBeNull();
  });

  it('keeps the environment the first page gave, when a later page has none', async () => {
    const pages = [
      {
        messages: Array.from({ length: PAGE }, (_, i) => message(i + 1, 'queued')),
        env: 'creating',
      },
      { messages: [message(PAGE + 1, 'queued')], env: null },
    ] as const;
    const feed = new ChatFeed({
      messages: async (after) => {
        const page = after === 0 ? pages[0] : pages[1];
        return { messages: [...page.messages], run: null, service: true, environment: page.env };
      },
      events: async () => ({ state: 'active', events: [] }),
    });
    expect((await feed.read()).environment).toBe('creating');
  });

  it('reads past a full page of messages', async () => {
    const { state, feed } = server();
    state.messages = Array.from({ length: PAGE + 3 }, (_, index) => message(index + 1, 'refused'));
    const view = await feed.read();
    expect(view.messages).toHaveLength(PAGE + 3);
  });

  it("follows the run holding the session, by its events' cursor", async () => {
    const { state, asked, feed } = server();
    state.lease = 'r1';
    state.events.set('r1', [event(1, 'started'), event(2, 'model')]);
    let view = await feed.read();
    expect(view.runs.at(-1)?.events.map((item) => item.seq)).toEqual([1, 2]);
    expect(thinking(view.runs.at(-1))).toBe(true);
    state.events.get('r1')!.push(event(3, 'assistant', { text: 'Hi', tool_calls: [] }));
    view = await feed.read();
    expect(asked.filter((item) => item.startsWith('events'))).toEqual([
      'events r1 0',
      'events r1 2',
    ]);
    expect(view.runs.at(-1)?.events.map((item) => item.seq)).toEqual([1, 2, 3]);
    expect(thinking(view.runs.at(-1))).toBe(false);
  });

  it('asks no more of a run that ended once its events are read', async () => {
    const { state, asked, feed } = server();
    state.lease = 'r1';
    state.events.set('r1', [event(1, 'started'), event(2, 'stopped', { stopped: 'answered' })]);
    state.runState.set('r1', 'ended');
    await feed.read();
    state.lease = null;
    await feed.read();
    expect(asked.filter((item) => item.startsWith('events'))).toEqual(['events r1 0']);
  });

  it('shows the newest run a message names when no run holds the session', async () => {
    const { state, feed } = server();
    state.messages = [
      message(1, 'delivered', 'r1'),
      message(2, 'held', 'r2'),
      message(3, 'queued'),
    ];
    state.runState.set('r2', 'interrupted');
    const view = await feed.read();
    expect(view.runs.map((run) => run.run)).toEqual(['r1', 'r2']);
    expect(view.runs.at(-1)).toMatchObject({ run: 'r2', state: 'interrupted' });
    expect(view.service).toBe(true);
  });

  it('reads again a run the session names before the run is known (404), and goes on', async () => {
    const { state, asked, feed } = server();
    state.lease = 'r1';
    const reads = (feed as unknown as { reads: { events: unknown } }).reads;
    const answer = reads.events as (run: string, after: number) => Promise<EventPage>;
    let known = false;
    reads.events = async (run: string, after: number) => {
      if (!known) throw new ApiError(404, 'not_found', 'no run r1');
      return answer(run, after);
    };
    const first = await feed.read();
    expect(first.runs.at(-1)).toMatchObject({ run: 'r1', events: [] });
    known = true;
    state.events.set('r1', [event(1, 'started')]);
    const second = await feed.read();
    expect(second.runs.at(-1)?.events).toEqual([event(1, 'started')]);
    expect(asked.filter((item) => item.startsWith('events'))).toEqual(['events r1 0']);
  });

  it('keeps showing a run the user cancelled, whose message is now held', async () => {
    const { state, feed } = server();
    state.messages = [message(1, 'delivered', 'r1'), message(2, 'queued', 'r2')];
    state.events.set('r1', [event(1, 'started'), event(2, 'stopped', { stopped: 'answered' })]);
    state.runState.set('r1', 'ended');
    state.lease = 'r2';
    state.events.set('r2', [event(1, 'started'), event(2, 'model', { turn: 1 })]);
    await feed.read();
    state.messages = [message(1, 'delivered', 'r1'), message(2, 'held', 'r2')];
    state.events.get('r2')!.push(event(3, 'stopped', { stopped: 'cancelled' }));
    state.runState.set('r2', 'ended');
    state.lease = null;
    const view = await feed.read();
    expect(view.runs.map((run) => run.run)).toEqual(['r1', 'r2']);
    expect(view.runs.at(-1)?.events.at(-1)?.kind).toBe('stopped');
  });

  it("reads a run's last answer before the next run is shown below it", async () => {
    const { state, feed } = server();
    state.messages = [message(1, 'queued', 'r1')];
    state.lease = 'r1';
    state.events.set('r1', [event(1, 'started'), event(2, 'user', { message_id: 'm1' })]);
    await feed.read();
    // Between two polls r1 answers and ends, and the next message starts r2.
    state.events
      .get('r1')!
      .push(
        event(3, 'assistant', { text: 'answer 1' }),
        event(4, 'stopped', { stopped: 'answered' }),
      );
    state.runState.set('r1', 'ended');
    state.messages = [message(1, 'delivered', 'r1'), message(2, 'queued', 'r2')];
    state.lease = 'r2';
    state.events.set('r2', [event(1, 'started')]);
    const view = await feed.read();
    expect(view.runs.map((run) => [run.run, run.events.length])).toEqual([
      ['r1', 4],
      ['r2', 1],
    ]);
  });

  it(`shows at most ${MAX_RUNS} runs, the newest`, async () => {
    const { state, feed } = server();
    state.messages = Array.from({ length: MAX_RUNS + 2 }, (_, index) =>
      message(index + 1, 'delivered', `r${index + 1}`),
    );
    const view = await feed.read();
    expect(view.runs.map((run) => run.run)).toEqual(
      Array.from({ length: MAX_RUNS }, (_, index) => `r${index + 3}`),
    );
  });

  it('stops asking, a few polls later, of a run that lost its lease without ending', async () => {
    const { state, asked, feed } = server();
    state.messages = [message(1, 'queued', 'r1')];
    state.lease = 'r1';
    state.events.set('r1', [event(1, 'started')]);
    await feed.read();
    state.lease = null;
    state.runState.set('r1', 'interrupted');
    for (let poll = 0; poll < INTERRUPTED_POLLS + 2; poll++) await feed.read();
    expect(asked.filter((item) => item.startsWith('events'))).toHaveLength(1 + INTERRUPTED_POLLS);
  });

  it('does not keep reading held messages, until asked to after a resume or a message', async () => {
    const { state, asked, feed } = server();
    state.messages = [message(1, 'held', 'r1'), message(2, 'delivered', 'r2')];
    state.runState.set('r1', 'ended');
    state.runState.set('r2', 'ended');
    await feed.read();
    await feed.read();
    feed.recheck();
    await feed.read();
    expect(asked.filter((item) => item.startsWith('messages'))).toEqual([
      'messages 0',
      'messages 2',
      'messages 0',
    ]);
  });

  it('shows the same newest runs on every poll, without reading older ones again', async () => {
    const { state, asked, feed } = server();
    for (let n = 1; n <= MAX_RUNS + 2; n++) {
      state.messages.push(message(n, 'delivered', `r${n}`));
      state.events.set(`r${n}`, [
        event(1, 'started'),
        event(2, 'stopped', { stopped: 'answered' }),
      ]);
      state.runState.set(`r${n}`, 'ended');
    }
    state.lease = null;
    const newest = Array.from({ length: MAX_RUNS }, (_, index) => `r${index + 3}`);
    for (let poll = 0; poll < 4; poll++)
      expect((await feed.read()).runs.map((run) => run.run)).toEqual(newest);
    // Each shown run was read once, to its end; the two older ones never.
    expect(asked.filter((item) => item.startsWith('events')).sort()).toEqual(
      newest.map((run) => `events ${run} 0`).sort(),
    );
  });

  it('keeps the active run in the window, with its Cancel and thinking, beside older runs', async () => {
    const { state, feed } = server();
    for (let n = 1; n <= MAX_RUNS + 1; n++) {
      state.messages.push(message(n, 'delivered', `r${n}`));
      state.runState.set(`r${n}`, 'ended');
    }
    state.messages.push(message(MAX_RUNS + 2, 'queued', 'live'));
    state.lease = 'live';
    state.events.set('live', [event(1, 'started'), event(2, 'model', { turn: 1 })]);
    for (let poll = 0; poll < 3; poll++) {
      const view = await feed.read();
      expect(view.runs.at(-1)?.run).toBe('live');
      expect(thinking(view.runs.at(-1))).toBe(true);
    }
  });

  it('after a reload, shows a run that was cancelled before answering, from what it was offered', async () => {
    const { state, feed } = server();
    state.messages = [message(1, 'delivered', 'r1'), message(2, 'held', 'r2')];
    state.events.set('r1', [event(1, 'started'), event(2, 'stopped', { stopped: 'answered' })]);
    state.events.set('r2', [event(1, 'started'), event(2, 'stopped', { stopped: 'cancelled' })]);
    state.runState.set('r1', 'ended');
    state.runState.set('r2', 'ended');
    const view = await feed.read(); // a fresh feed, as after a reload
    expect(view.runs.map((run) => run.run)).toEqual(['r1', 'r2']);
  });

  it("reads the stopped event the service writes after a dead run's lease expires", async () => {
    const { state, feed } = server();
    state.messages = [message(1, 'queued', 'r1')];
    state.lease = 'r1';
    state.events.set('r1', [event(1, 'started'), event(2, 'model')]);
    await feed.read();
    state.lease = null;
    state.runState.set('r1', 'interrupted');
    await feed.read();
    state.events.get('r1')!.push(event(3, 'stopped', { stopped: 'interrupted' }));
    state.runState.set('r1', 'ended');
    state.messages = [message(1, 'held', 'r1')];
    const view = await feed.read();
    expect(view.runs[0]?.events.at(-1)?.kind).toBe('stopped');
  });

  it('sees, after a while, a held message another tab resumed', async () => {
    const { state, feed } = server();
    state.messages = [message(1, 'held', 'r1')];
    state.runState.set('r1', 'ended');
    await feed.read();
    state.messages = [message(1, 'queued', 'r1')]; // resumed elsewhere
    let view = await feed.read();
    for (let poll = 2; poll <= HELD_EVERY; poll++) view = await feed.read();
    expect(view.messages[0]?.state).toBe('queued');
  });

  it('moves to a new run once it holds the session', async () => {
    const { state, feed } = server();
    state.lease = 'r1';
    await feed.read();
    state.lease = 'r2';
    state.events.set('r2', [event(1, 'started')]);
    const view = await feed.read();
    expect(view.runs.at(-1)).toMatchObject({ run: 'r2', events: [event(1, 'started')] });
  });
});

describe('the transcript of a run', () => {
  it('shows what each event says, as text', () => {
    const entries = chatEntries([
      event(1, 'started', { run_id: 'r1', message_ids: ['m1'] }),
      event(2, 'user', { message_id: 'm1', text: '<b>What</b> is $x$?' }),
      event(3, 'model', { turn: 1 }),
      event(4, 'assistant', {
        text: 'Let me check.',
        tool_calls: [{ id: 'c1', name: 'python', arguments: '{"code": "1"}' }],
        usage: { model: 'model-a', input_tokens: 1200, output_tokens: 34 },
      }),
      event(5, 'tool', {
        name: 'python',
        result: '1',
        outcome: { state: 'succeeded', error: null, exit_code: 0 },
      }),
      event(6, 'notice', { source: 'jobs', ids: ['j1'] }),
      event(7, 'waiting', { job_ids: ['j1'], mode: 'all' }),
      event(8, 'waiting', { job_ids: null, mode: null }),
      event(9, 'stopped', { stopped: 'answered', failure: null }),
    ]);
    expect(entries).toEqual([
      { kind: 'started', seq: 1, text: 'Run r1 started, answering 1 message.' },
      { kind: 'user', seq: 2, text: '<b>What</b> is $x$?' },
      {
        kind: 'assistant',
        seq: 4,
        text: 'Let me check.',
        calls: [{ name: 'python', arguments: '{"code": "1"}' }],
        usage: 'model-a: 1,200 input and 34 output tokens',
      },
      { kind: 'tool', seq: 5, name: 'python', outcome: 'succeeded, exit code 0', result: '1' },
      { kind: 'note', seq: 6, text: 'Notices from jobs: j1.', problem: false },
      { kind: 'note', seq: 7, text: 'Waiting for all of jobs j1.', problem: false },
      { kind: 'note', seq: 8, text: 'The wait for jobs ended.', problem: false },
      { kind: 'note', seq: 9, text: 'The run answered.', problem: false },
    ]);
  });

  it('takes no field of the wrong type, and says how a run stopped', () => {
    const [assistant] = chatEntries([event(1, 'assistant', { text: 5, tool_calls: 'x' })]);
    expect(assistant).toEqual({ kind: 'assistant', seq: 1, text: '', calls: [], usage: null });
    expect(tokensText({ model: 'm', input_tokens: 5, output_tokens: 1, cost: 0.003 })).toBe(
      'm: 5 input and 1 output tokens, $0.0030',
    );
    expect(tokensText({ model: null, input_tokens: 5, output_tokens: null })).toBe(
      '5 input and unreported output tokens',
    );
    expect(stoppedText({ stopped: 'error', failure: 'model' })).toBe(
      'The run failed. Failure: model.',
    );
    expect(stoppedText({ stopped: 'constructor' })).toBe('The run stopped (constructor).');
    expect(stoppedText({ stopped: 'budget' })).toBe(
      'The run stopped: the project reached its daily cost limit. What waits for the session waits until the limit allows another run.',
    );
    expect(stoppedText({ stopped: 'gateway_budget' })).toBe(
      'The run stopped: the model gateway refused the call because its spending budget is used up. What waits for the session waits until the gateway allows calls again.',
    );
    expect(stoppedText({ stopped: 'run_budget' })).toBe(
      "The run stopped at its own limit: its part of the project's daily cost limit, or a delegated run's tokens.",
    );
    const [stopped] = chatEntries([event(1, 'stopped', { stopped: 'interrupted' })]);
    expect(stopped).toMatchObject({ problem: true });
  });

  it('lists a queued message once: in the transcript when the run has it, else beside it', () => {
    const run = {
      run: 'r1',
      state: 'active' as const,
      events: [event(1, 'user', { message_id: 'm1', text: 'text 1' })],
    };
    const listed = pendingMessages(
      [
        message(1, 'queued'),
        message(2, 'queued'),
        message(3, 'held', 'r0'),
        message(4, 'delivered', 'r0'),
      ],
      [run],
    );
    expect(listed.map((item) => item.message_id)).toEqual(['m2', 'm3']);
    const held = { ...message(1, 'held', 'r1') };
    expect(pendingMessages([held], [run])).toEqual([held]); // a held one stays, with Resume
  });

  it('says what a queued message waits on only while the sandbox is being made', () => {
    const queued = [message(1, 'queued')];
    const idle = { lease: null, environment: null } as const;
    expect(startingText({ ...idle, environment: 'creating' }, queued)).toMatch(
      /^Starting the environment/,
    );
    // Nothing is said when the worker does not say, or for a state that takes no time.
    expect(startingText(idle, queued)).toBeNull();
    for (const environment of ['ready', 'stopped', 'deleted'] as const)
      expect(startingText({ ...idle, environment }, queued)).toBeNull();
    // Nor when no message waits for a run (held and refused ones do not), or a run has begun.
    const settled = [
      message(2, 'held', 'r0'),
      message(3, 'refused'),
      message(4, 'delivered', 'r0'),
    ];
    expect(startingText({ ...idle, environment: 'creating' }, settled)).toBeNull();
    const lease = { run: 'r1', started_at: '', renewed_at: '' };
    expect(startingText({ lease, environment: 'creating' }, queued)).toBeNull();
  });

  it("says what each message's state means", () => {
    expect(messageState(message(1, 'delivered', 'r1'))).toBe('Delivered to r1');
    expect(messageState({ ...message(1, 'refused'), reason: 'budget' })).toBe(
      'Refused: the project reached its daily cost limit',
    );
    expect(messageState(message(1, 'held', 'r1'))).toBe(
      'Held: its run was cancelled or interrupted before answering it. Resume, or write again, to queue it once more',
    );
    expect(messageState({ ...message(1, 'held', 'r1'), reason: '5 runs failed in a row' })).toBe(
      'Held: 5 runs failed in a row. Resume, or write again, to queue it once more',
    );
  });
});

describe('a message to send', () => {
  it('is measured in UTF-8 bytes, as the server counts it', () => {
    expect(messageBytes('é')).toBe(2);
    expect(messageBytes('x'.repeat(MAX_MESSAGE_BYTES))).toBe(MAX_MESSAGE_BYTES);
  });
});

describe('sending a message', () => {
  let storage: Map<string, string>;
  beforeEach(() => {
    storage = new Map();
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('sends exactly the message ID and text, keeping the ID until a result', async () => {
    const sent: unknown[] = [];
    let fail = true;
    const send = async (body: { message_id: string; text: string }) => {
      sent.push(body);
      if (fail) throw new Error('no answer');
      return { message: message(1, 'queued'), question: null, run: null };
    };
    await expect(sendMessage('p1', 's1', 'Hello', send)).rejects.toThrow();
    fail = false;
    await sendMessage('p1', 's1', 'Hello', send);
    const [first, second] = sent as { message_id: string; text: string; question_id: unknown }[];
    expect(Object.keys(first!).sort()).toEqual(['message_id', 'question_id', 'text']);
    expect(first!.question_id).toBeNull(); // no question shown
    expect(second).toEqual(first);
    await sendMessage('p1', 's1', 'Hello', send);
    expect((sent[2] as { message_id: string }).message_id).not.toBe(first!.message_id);
  });

  it('forgets the kept ID of a failed message given up, so the same text later is new', async () => {
    const sent: { message_id: string }[] = [];
    const failing = async (body: { message_id: string; text: string }) => {
      sent.push(body);
      throw new Error('no answer');
    };
    await expect(sendMessage('p1', 's1', 'yes', failing)).rejects.toThrow();
    await forgetMessage('p1', 's1', 'yes');
    await expect(sendMessage('p1', 's1', 'yes', failing)).rejects.toThrow();
    expect(sent[1]!.message_id).not.toBe(sent[0]!.message_id);
  });
});

describe('the chat panel', () => {
  it('renders no text as HTML: the Markdown component is its only renderer', () => {
    const views = import.meta.glob(
      ['../views/ChatPanel.svelte', '../views/ChatRun.svelte', '../views/ChatWaiting.svelte'],
      { query: '?raw', import: 'default', eager: true },
    );
    const source = Object.values(views).map(String).join('\n');
    expect(Object.keys(views)).toHaveLength(3);
    expect(source).toContain('<Markdown');
    expect(source).not.toContain('{@html');
  });
});

describe('the messages waiting for a run', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const html = (messages: ChatMessage[], starting?: string | null) =>
    render(ChatWaiting, { props: { messages, starting } }).body;

  it('says the environment is starting, beside the queued message', () => {
    const queued = { ...message(1, 'queued'), text: 'What is $2^{64} \\bmod p$?' };
    const body = html([queued], startingText({ lease: null, environment: 'creating' }, [queued]));
    expect(body).toContain('role="status"');
    expect(body).toContain('Starting the environment');
    expect(body).toContain('Queued:');
    expect(body).toContain('What is $2^{64}');
  });

  it('says nothing more than the state of each message when the server has no news', () => {
    const body = html([message(1, 'queued')], null);
    expect(body).toContain('Queued:');
    expect(body).not.toContain('role="status"');
    expect(html([])).toBe(html([], null)); // no list for no messages
    expect(html([])).not.toContain('<ul');
  });

  it('shows a message and the reason it was held as text, never as markup', () => {
    const body = html([
      { ...message(1, 'queued'), text: hostile },
      { ...message(2, 'held', 'r0'), reason: hostile },
    ]);
    expect(body).not.toContain('<img');
    expect(body.match(/&lt;img src=x onerror=alert\(1\)/g)).toHaveLength(2); // the text and the reason
  });
});
