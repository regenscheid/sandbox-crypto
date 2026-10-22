// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { RunEvent } from './api-types';
import { chatEntries } from './chat';
import {
  callSummary,
  clock,
  dayAndTime,
  foldTranscript,
  stepText,
  workedFor,
  workSummary,
  type WorkStep,
} from './conversation';
import { activeSubagents, sessionStatus, subagentText } from './sessionStatus';
import { attachmentMessage, splitAttachments } from './uploads';

const event = (seq: number, minute: number, kind: RunEvent['kind'], data = {}): RunEvent => ({
  seq,
  at: `2026-10-21T17:${String(minute).padStart(2, '0')}:00Z`,
  kind,
  data,
  pins: [],
});
const call = (id: string, name: string, args: object) => ({
  id,
  name,
  arguments: JSON.stringify(args),
});

describe('the folded transcript', () => {
  const events = [
    event(1, 0, 'started', { run_id: 'r1', message_ids: ['m1'] }),
    event(2, 0, 'user', { message_id: 'm1', text: 'Resume the research.' }),
    event(3, 1, 'notice', { source: 'jobs', ids: ['j1'] }),
    event(4, 2, 'assistant', {
      text: 'Reading the plan.',
      tool_calls: [call('c1', 'plan_read', {}), call('c2', 'shell', { command: 'sage r37.sage' })],
    }),
    event(5, 3, 'tool', {
      call_id: 'c2',
      name: 'shell',
      result: 'ok',
      outcome: { state: 'succeeded' },
    }),
    event(6, 4, 'tool', { call_id: 'c1', name: 'plan_read', result: 'plan', outcome: {} }),
    event(7, 5, 'waiting', { delegation_ids: ['d1'] }),
    event(8, 15, 'assistant', {
      text: 'Please upload the archive.',
      tool_calls: [call('c3', 'ask_user', { question: 'Upload?' })],
    }),
    event(9, 20, 'assistant', { text: 'Done: one new path.', tool_calls: [] }),
    event(10, 20, 'stopped', { stopped: 'answered' }),
  ];
  const items = foldTranscript(chatEntries(events), events, new Set(['c3']));

  it('keeps the messages and folds the steps between them, leaving the bookkeeping out', () => {
    expect(items.map((item) => item.kind)).toEqual(['user', 'work', 'reply', 'reply']);
    const work = items[1] as Extract<(typeof items)[number], { kind: 'work' }>;
    expect(work.steps.map((step) => step.seq)).toEqual([4, 8]);
    expect(workedFor(work.from, work.to)).toBe('Worked for 13 min');
    expect(workSummary(work.steps)).toBe('2 steps · plan_read, shell, ask_user');
  });

  it('pairs each result with its call by ID, whatever their order', () => {
    const work = items[1] as Extract<(typeof items)[number], { kind: 'work' }>;
    const [read, shell] = work.steps[0]!.calls;
    expect(read).toMatchObject({ name: 'plan_read', done: true, result: 'plan' });
    expect(shell).toMatchObject({ name: 'shell', done: true, outcome: 'succeeded', result: 'ok' });
    expect(work.steps[1]!.calls[0]!.done).toBe(false);
  });

  it('ends a group at the step that asked a question, and shows its words as a reply', () => {
    expect(items[2]).toMatchObject({ kind: 'reply', seq: 8, text: 'Please upload the archive.' });
    expect((items[1] as { steps: WorkStep[] }).steps[1]!.text).toBe('');
  });

  it('says when a run stopped with a problem', () => {
    const failed = [event(1, 0, 'stopped', { stopped: 'error', failure: 'gateway' })];
    expect(foldTranscript(chatEntries(failed), failed)).toMatchObject([
      { kind: 'note', problem: true },
    ]);
  });

  it('describes a step by its words, or by what its first call does', () => {
    const step = (text: string, args: object): WorkStep => ({
      seq: 1,
      at: '',
      text,
      calls: [{ ...call('c', 'shell', args), done: false, outcome: '', result: '' }],
    });
    expect(stepText(step('Ran the verifier.\nMore.', {}))).toBe('Ran the verifier.');
    expect(stepText(step('', { command: 'sage x.sage\nsecond' }))).toBe('shell: sage x.sage');
    expect(stepText(step('', {}))).toBe('Called shell');
    expect(callSummary({ name: 'x', arguments: 'not json' })).toBe('not json');
  });
});

describe('the session status', () => {
  const base = {
    closed: false,
    service: true,
    running: false,
    questions: 0,
    held: false,
    recovery: null,
    subagents: 0,
  };
  it('says Working, with subagents only when some run', () => {
    expect(sessionStatus({ ...base, running: true })).toEqual({
      tone: 'working',
      label: 'Working',
      detail: null,
    });
    expect(sessionStatus({ ...base, running: true, subagents: 2 }).detail).toBe(
      '2 active subagents',
    );
    expect(subagentText(1)).toBe('1 active subagent');
  });
  it('puts what the user must do first, and stopped work after running work', () => {
    expect(sessionStatus({ ...base, running: true, questions: 1 }).label).toBe('Needs your answer');
    expect(sessionStatus({ ...base, recovery: { unfinished: 2, active: false } }).tone).toBe(
      'stopped',
    );
    expect(sessionStatus({ ...base, recovery: { unfinished: 2, active: true } }).tone).toBe(
      'working',
    );
    expect(sessionStatus({ ...base, service: false }).label).toBe('Offline');
    expect(sessionStatus({ ...base, closed: true, service: false }).label).toBe('Closed');
    expect(sessionStatus(base).label).toBe('Idle');
  });
  it('counts the delegated runs that run now', () => {
    const run = (role: 'main' | 'delegated', state: 'running' | 'ended') =>
      ({ role, state }) as never;
    expect(
      activeSubagents({
        runs: [run('main', 'running'), run('delegated', 'running'), run('delegated', 'ended')],
      }),
    ).toBe(1);
  });
});

describe('attached files in a message', () => {
  const file = {
    upload_id: 'upload-1',
    project: 'p',
    session: 's',
    filename: 'snova-research.tar.gz',
    media_type: 'application/gzip',
    sha256: 'a'.repeat(64),
    size: 14_200_000,
    created_at: '',
    author: 'user',
    path: 'uploads/snova-research.tar.gz',
  } as never;
  it('reads back the manifest a message carries, as files', () => {
    const split = splitAttachments(attachmentMessage('Here it is.', [file]));
    expect(split.text).toBe('Here it is.');
    expect(split.files).toEqual([
      {
        filename: 'snova-research.tar.gz',
        path: 'uploads/snova-research.tar.gz',
        size: 14_200_000,
        sha256: 'a'.repeat(64),
      },
    ]);
  });
  it('leaves a text whole when its manifest does not read back', () => {
    const text =
      'Look:\n\nAttached files (sandbox copies; original bytes retained):\n```json\n[{}]\n```';
    expect(splitAttachments(text)).toEqual({ text, files: [] });
    expect(splitAttachments('plain')).toEqual({ text: 'plain', files: [] });
  });
});

describe('times in lists', () => {
  it('gives the clock time today and the day before then', () => {
    const now = new Date(2026, 9, 21, 15, 0).getTime();
    const today = new Date(2026, 9, 21, 9, 30).toISOString();
    const earlier = new Date(2026, 9, 17, 17, 26).toISOString();
    expect(dayAndTime(today, now)).toBe(clock(today));
    expect(dayAndTime(earlier, now)).toMatch(/Oct 17, /);
    expect(dayAndTime(new Date(2025, 0, 2).toISOString(), now)).toMatch(/2025/);
  });
});
