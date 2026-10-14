// SPDX-License-Identifier: MIT
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EventPage, MessageListing } from './api-types';
import {
  ChatFeed,
  chatEntries,
  messageState,
  PAGE,
  pendingMessages,
  sendMessage,
  stoppedText,
  thinking,
  type ChatMessage,
  type RunEvent,
} from './chat';

const message = (
  number: number,
  state: ChatMessage['state'],
  run: string | null = null,
): ChatMessage => ({
  message_id: `m${number}`,
  number,
  text: `text ${number}`,
  author: 'user',
  created_at: '2026-10-14T12:00:00Z',
  state,
  run,
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
    expect(view.run?.events.map((item) => item.seq)).toEqual([1, 2]);
    expect(thinking(view.run)).toBe(true);
    state.events.get('r1')!.push(event(3, 'assistant', { text: 'Hi', tool_calls: [] }));
    view = await feed.read();
    expect(asked.filter((item) => item.startsWith('events'))).toEqual([
      'events r1 0',
      'events r1 2',
    ]);
    expect(view.run?.events.map((item) => item.seq)).toEqual([1, 2, 3]);
    expect(thinking(view.run)).toBe(false);
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
    expect(view.run).toMatchObject({ run: 'r2', state: 'interrupted' });
    expect(view.service).toBe(true);
  });

  it('moves to a new run once it holds the session', async () => {
    const { state, feed } = server();
    state.lease = 'r1';
    await feed.read();
    state.lease = 'r2';
    state.events.set('r2', [event(1, 'started')]);
    const view = await feed.read();
    expect(view.run).toMatchObject({ run: 'r2', events: [event(1, 'started')] });
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
    expect(assistant).toEqual({ kind: 'assistant', seq: 1, text: '', calls: [] });
    expect(stoppedText({ stopped: 'error', failure: 'model' })).toBe(
      'The run failed. Failure: model.',
    );
    expect(stoppedText({ stopped: 'constructor' })).toBe('The run stopped (constructor).');
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
      run,
    );
    expect(listed.map((item) => item.message_id)).toEqual(['m2', 'm3']);
    const held = { ...message(1, 'held', 'r1') };
    expect(pendingMessages([held], run)).toEqual([held]); // a held one stays, with Resume
  });

  it("says what each message's state means", () => {
    expect(messageState(message(1, 'delivered', 'r1'))).toBe('Delivered to r1');
    expect(messageState({ ...message(1, 'refused'), reason: 'budget' })).toBe(
      "Refused: the project's model budget is spent",
    );
    expect(messageState(message(1, 'held', 'r1'))).toMatch(/^Held/);
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
      return { message: message(1, 'queued'), run: null };
    };
    await expect(sendMessage('p1', 's1', 'Hello', send)).rejects.toThrow();
    fail = false;
    await sendMessage('p1', 's1', 'Hello', send);
    const [first, second] = sent as { message_id: string; text: string }[];
    expect(Object.keys(first!).sort()).toEqual(['message_id', 'text']);
    expect(second).toEqual(first);
    await sendMessage('p1', 's1', 'Hello', send);
    expect((sent[2] as { message_id: string }).message_id).not.toBe(first!.message_id);
  });
});

describe('the chat panel', () => {
  it('renders no text as HTML: the Markdown component is its only renderer', () => {
    const views = import.meta.glob('../views/ChatPanel.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    const source = String(Object.values(views)[0] ?? '');
    expect(source).toContain('<Markdown');
    expect(source).not.toContain('{@html');
  });
});
