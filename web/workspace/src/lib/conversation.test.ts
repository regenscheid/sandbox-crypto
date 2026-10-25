// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import {
  agentDirectory,
  MessageBodies,
  messageLine,
  receivedRow,
  sentRow,
  type AgentMessagesItem,
  type ChildOutcomesItem,
} from './agentMessages';
import type { AgentMessagePage, AgentMessageView, RunEvent, RunTree } from './api-types';
import { chatEntries, type Gloss } from './chat';
import {
  backgroundList,
  backgroundText,
  batchHeader,
  callSummary,
  clock,
  dayAndTime,
  foldTranscript,
  glossText,
  liveWork,
  MAIN_ARGUMENT,
  mainArgument,
  rowLabel,
  workedFor,
  workSummary,
  type BackgroundItem,
  type StepCall,
  type WorkStep,
} from './conversation';
import { agentHref, parseRoute } from './router';
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
  const work = items[2] as Extract<(typeof items)[number], { kind: 'work' }>;

  it('keeps the messages and folds the steps between them, with background work after', () => {
    expect(items.map((item) => item.kind)).toEqual([
      'user',
      'background',
      'work',
      'background',
      'reply',
      'reply',
    ]);
    expect(work.steps.map((step) => step.seq)).toEqual([4, 8]);
    expect(workedFor(work.from, work.to)).toBe('Worked for 13 min');
    expect(workSummary(work.steps)).toBe('Ran 1 command · 3 steps');
  });

  it('pairs each result with its call by ID, whatever their order', () => {
    const [read, shell] = work.steps[0]!.calls;
    expect(read).toMatchObject({ name: 'plan_read', done: true, result: 'plan' });
    expect(shell).toMatchObject({ name: 'shell', done: true, outcome: 'succeeded', result: 'ok' });
    expect(work.steps[1]!.calls[0]!.done).toBe(false);
  });

  it('ends a group at the step that asked a question, and shows its words as a reply', () => {
    expect(items[4]).toMatchObject({ kind: 'reply', seq: 8, text: 'Please upload the archive.' });
    expect(work.steps[1]!.text).toBe('');
  });

  it('says when a run stopped with a problem', () => {
    const failed = [event(1, 0, 'stopped', { stopped: 'error', failure: 'gateway' })];
    expect(foldTranscript(chatEntries(failed), failed)).toMatchObject([
      { kind: 'note', problem: true },
    ]);
  });

  it('describes a call by its most telling argument', () => {
    expect(callSummary(call('c', 'connector', { command: 'sage x.sage\nsecond' }))).toBe(
      'sage x.sage',
    );
    expect(callSummary({ name: 'x', arguments: 'not json' })).toBe('not json');
  });
});

const gloss = (outcome: string, more: Partial<Gloss> = {}): Gloss => ({
  outcome,
  exit_code: null,
  lines: null,
  bytes: null,
  files: [],
  more_files: 0,
  error: null,
  ...more,
});
const done = (name: string, args: object, more: Partial<StepCall> = {}): StepCall => ({
  ...call('c', name, args),
  done: true,
  outcome: '',
  result: '',
  ...more,
});

describe('batches and their rows (B10, item 1)', () => {
  it('gives a batch a header generated from its tools, counting failures', () => {
    const calls = [
      done('shell', {}, { gloss: gloss('ok') }),
      done('python', {}),
      done('sage', {}),
      done('start_job', {}, { gloss: gloss('started') }),
      done('read_file', {}),
      done('read_file', {}),
      done('pdf_pages', {}, { gloss: gloss('failed', { error: 'no such page' }) }),
    ];
    expect(batchHeader(calls)).toBe('Ran 4 commands, read 3 files · 7 steps · 1 failed');
    expect(
      batchHeader([
        done('edit_file', {}),
        done('grep', {}),
        done('glob', {}),
        done('ledger_change', {}),
        done('document_patch', {}),
        done('delegate', {}),
        done('ask_user', {}),
      ]),
    ).toBe(
      'Edited 1 file, ran 2 searches, made 1 ledger change, made 1 document change, delegated 1 task · 7 steps',
    );
    // Without a gloss, an older event's outcome words tell a failure.
    expect(batchHeader([done('plan_read', {}, { outcome: 'error not_found' })])).toBe(
      '1 step · 1 failed',
    );
  });

  it('sums every batch of a group in its summary', () => {
    const step = (calls: StepCall[]): WorkStep => ({ seq: 1, at: '', text: '', calls });
    expect(
      workSummary([step([done('shell', {}), done('shell', {})]), step([done('write_file', {})])]),
    ).toBe('Ran 2 commands, edited 1 file · 3 steps');
  });

  it("labels a row with the call's purpose, the agent's words, shown as text", () => {
    expect(
      rowLabel({ ...call('c', 'shell', { script: 'ls' }), purpose: 'Listing the data' }),
    ).toEqual({ says: true, text: 'Listing the data' });
    expect(
      rowLabel({ ...call('c', 'shell', {}), purpose: 'Reading <b>x</b>\u202e here' }).text,
    ).toBe('Reading <b>x</b>⟦U+202E⟧ here');
  });

  it('labels a row without a purpose with its tool and main argument, picked per tool', () => {
    expect(rowLabel(call('c', 'shell', { script: 'sage r37.sage\nmore' }))).toEqual({
      says: false,
      tool: 'shell',
      text: 'sage r37.sage',
    });
    const cases: [string, object, string][] = [
      ['python', { code: 'print(1)' }, 'print(1)'],
      [
        'start_job',
        { argv: ['sage', 'x.sage'], purpose: 'Long run', expected_progress: 'p' },
        'sage, x.sage',
      ],
      ['read_file', { path: 'spec/law.md', offset: 0 }, 'spec/law.md'],
      ['write_file', { path: 'out.csv', content: 'a,b' }, 'out.csv'],
      ['edit_file', { path: 'a.py', old_string: 'x', new_string: 'y' }, 'a.py'],
      ['grep', { pattern: 'twist', path: 'src' }, 'twist'],
      ['glob', { pattern: '**/*.sage' }, '**/*.sage'],
      ['history_search', { query: 'field size' }, 'field size'],
      ['ledger_read', { entry: 'C-12' }, 'C-12'],
      ['ledger_change', { reason: 'Recorded the bound', changes: [] }, 'Recorded the bound'],
      ['document_patch', { document: 'notes', base_revision: 2 }, 'notes'],
      ['ask_user', { question: 'Upload the archive?' }, 'Upload the archive?'],
      ['delegate', { task: 'Check the twist order' }, 'Check the twist order'],
      ['delegate', { check: { entry: 'C-3', revision: 1 } }, 'C-3'],
      ['wait_jobs', { job_ids: ['j1', 'j2'] }, 'j1, j2'],
      ['message_child', { delegation_id: 'd1', text: 'Stop' }, 'd1'],
      ['connector_search', { query: 'SNOVA' }, 'SNOVA'],
    ];
    for (const [name, args, expected] of cases)
      expect(mainArgument(call('c', name, args)), name).toBe(expected);
    // Every tool the table names picks from its own arguments.
    expect(Object.keys(MAIN_ARGUMENT)).toContain('shell');
    expect(mainArgument({ name: 'shell', arguments: 'not json' })).toBe('not json');
  });

  it('glosses a result from the server, or says what it can without one', () => {
    expect(
      glossText(
        done('shell', {}, { gloss: gloss('ok', { lines: 212, bytes: 9000, files: ['a', 'b'] }) }),
      ),
    ).toBe('ok · 212 lines · 2 files');
    expect(
      glossText(
        done(
          'shell',
          {},
          { gloss: gloss('failed', { exit_code: 1, lines: 1, error: 'No such file' }) },
        ),
      ),
    ).toBe('failed · exit 1 · 1 line · "No such file"');
    expect(glossText(done('plan_read', {}, { gloss: gloss('ok', { bytes: 3174 }) }))).toBe(
      'ok · 3.1 KiB',
    );
    expect(
      glossText(
        done(
          'write_file',
          {},
          { gloss: gloss('ok', { bytes: 40, files: ['t.txt'], more_files: 11 }) },
        ),
      ),
    ).toBe('ok · 40 B · 12 files');
    expect(glossText(done('shell', {}, { outcome: 'succeeded, exit code 0' }))).toBe(
      'succeeded, exit code 0',
    );
    expect(glossText(done('plan_read', {}))).toBe('done');
    const running = { ...done('shell', {}), done: false };
    expect(glossText(running, '0:42')).toBe('running 0:42');
    expect(glossText(running)).toBe('no result recorded');
  });

  it("copies a result's purpose and gloss onto its row", () => {
    const events = [
      event(1, 0, 'assistant', {
        text: '',
        tool_calls: [call('c1', 'shell', { script: 'ls' })],
      }),
      event(2, 0, 'tool', {
        call_id: 'c1',
        name: 'shell',
        result: 'a\nb',
        outcome: { state: 'succeeded', error: null, exit_code: 0 },
        purpose: 'Listing the inputs',
        gloss: {
          outcome: 'ok',
          exit_code: 0,
          lines: 2,
          bytes: 3,
          files: [],
          more_files: 0,
          error: null,
        },
      }),
    ];
    const [work] = foldTranscript(chatEntries(events), events);
    const row = (work as { steps: WorkStep[] }).steps[0]!.calls[0]!;
    expect(row).toMatchObject({ purpose: 'Listing the inputs', done: true });
    expect(glossText(row)).toBe('ok · 2 lines');
  });
});

describe('background work in one row (B10, item 5)', () => {
  const at = (minute: number, second = 0) =>
    Date.parse(
      `2026-10-21T17:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}Z`,
    );
  const timed = (seq: number, second: number, kind: RunEvent['kind'], data = {}): RunEvent => ({
    ...event(seq, 0, kind, data),
    at: new Date(at(0, second)).toISOString(),
  });
  const background = (events: RunEvent[]) =>
    foldTranscript(chatEntries(events), events).filter(
      (item): item is BackgroundItem => item.kind === 'background',
    );

  it('summarizes a finished wait from its notices and its result', () => {
    const events = [
      timed(1, 0, 'assistant', {
        text: '',
        tool_calls: [call('c1', 'wait_jobs', { job_ids: ['j1', 'j2'] })],
      }),
      timed(2, 1, 'waiting', { job_ids: ['j1', 'j2'], mode: 'all' }),
      timed(3, 10, 'notice', { source: 'jobs', ids: ['j1'] }),
      timed(4, 17, 'waiting', { job_ids: null, mode: null }),
      timed(5, 17, 'tool', {
        call_id: 'c1',
        name: 'wait_jobs',
        result: JSON.stringify({
          reason: 'ended',
          jobs: [
            { job_id: 'j1', state: 'succeeded' },
            { job_id: 'j2', state: 'failed' },
          ],
        }),
        outcome: {},
      }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    expect(items.map((item) => item.kind)).toEqual(['work', 'background']);
    const [row] = background(events);
    expect(backgroundText(row!, at(5), false)).toBe('2 finished · 16 s');
    expect(backgroundList(row!, false)).toEqual([
      { id: 'j1', state: 'succeeded' },
      { id: 'j2', state: 'failed' },
    ]);
  });

  it('says what still runs while the wait goes on, and for how long so far', () => {
    const events = [
      timed(1, 0, 'assistant', {
        text: '',
        tool_calls: [call('c1', 'wait_delegations', { delegation_ids: ['d1', 'd2'] })],
      }),
      timed(2, 0, 'waiting', { delegation_ids: ['d1', 'd2'], mode: 'all' }),
      timed(3, 5, 'notice', { source: 'delegations', ids: ['d2'] }),
    ];
    const [row] = background(events);
    expect(backgroundText(row!, at(0, 42), true)).toBe('1 finished, 1 running · 42 s so far');
    expect(backgroundList(row!, true)).toEqual([
      { id: 'd1', state: 'running' },
      { id: 'd2', state: 'finished' },
    ]);
    // The live card stays on the group the wait is in, with the row after it.
    const items = foldTranscript(chatEntries(events), events);
    expect(liveWork(items)).toBe(0);
    // Ended without its end event, a wait ends when the model is called again.
    const later = [...events, timed(4, 30, 'assistant', { text: 'Done.', tool_calls: [] })];
    expect(backgroundText(background(later)[0]!, at(5), false)).toBe(
      '1 finished, 1 not finished · 30 s',
    );
  });

  it('gathers notices of work that ended without a wait, and of stale kernels', () => {
    const events = [
      timed(1, 0, 'notice', { source: 'jobs', ids: ['j1'] }),
      timed(2, 1, 'notice', { source: 'jobs', ids: ['j2'] }),
      timed(3, 2, 'notice', { source: 'kernels', ids: ['k1'] }),
      timed(4, 3, 'notice', { source: 'reviews', ids: ['r1'] }),
    ];
    const rows = background(events);
    expect(rows.map((row) => backgroundText(row, at(1), false))).toEqual([
      '2 finished',
      '1 kernel started before the current packages',
    ]);
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

describe('agent messages and children’s outcomes (B10, item 2)', () => {
  const ok = (lines = 1) => ({
    outcome: 'ok',
    exit_code: null,
    lines,
    bytes: null,
    files: [],
    more_files: 0,
    error: null,
  });
  const sentResult = (id: string, state = 'queued') =>
    JSON.stringify({ message_id: id, state, recipient: 'dlg-a', delivered_run: null });

  it('makes a message call its own row, sender to recipient, not a tool row', () => {
    const events = [
      event(1, 0, 'assistant', {
        text: 'Correcting the child.',
        tool_calls: [
          call('c1', 'message_child', {
            delegation_id: 'dlg-a',
            text: 'Use q = 2^8.\nThen rerun.',
          }),
        ],
      }),
      event(2, 1, 'tool', {
        call_id: 'c1',
        name: 'message_child',
        result: sentResult('agm-1'),
        outcome: {},
        gloss: ok(),
      }),
      event(3, 2, 'assistant', { text: 'Done.', tool_calls: [] }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    // A model call that only sent messages: its words, then the message, in order.
    expect(items.map((item) => item.kind)).toEqual(['reply', 'agent-messages', 'reply']);
    const row = (items[1] as AgentMessagesItem).rows[0]!;
    expect(row).toMatchObject({
      direction: 'sent',
      tool: 'message_child',
      child: 'dlg-a',
      body: 'Use q = 2^8.\nThen rerun.',
      message: 'agm-1',
      state: 'queued',
      done: true,
      failed: false,
    });
    expect(items.some((item) => item.kind === 'work')).toBe(false);
  });

  it('puts a message sent beside other calls after their group, and keeps them a step', () => {
    const events = [
      event(1, 0, 'assistant', {
        text: '',
        tool_calls: [
          call('c1', 'shell', { script: 'ls' }),
          call('c2', 'message_parent', { text: 'Found a counterexample.' }),
        ],
      }),
      event(2, 1, 'tool', { call_id: 'c2', name: 'message_parent', result: 'not json' }),
      event(3, 1, 'tool', { call_id: 'c1', name: 'shell', result: 'a', outcome: {} }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    expect(items.map((item) => item.kind)).toEqual(['work', 'agent-messages']);
    const work = items[0] as Extract<(typeof items)[number], { kind: 'work' }>;
    expect(work.steps[0]!.calls.map((one) => one.name)).toEqual(['shell']);
    const row = (items[1] as AgentMessagesItem).rows[0]!;
    // A result that does not say it was saved is a failure, with its first line.
    expect(row).toMatchObject({ child: null, failed: true, error: 'not json' });
  });

  it('makes each message a notice delivered a row of its own, by its ID', () => {
    const events = [
      event(1, 0, 'notice', { source: 'agent_messages', ids: ['agm-1', 'agm-2'] }),
      event(2, 1, 'assistant', { text: 'Noted.', tool_calls: [] }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    expect(items.map((item) => item.kind)).toEqual(['agent-messages', 'reply']);
    expect((items[0] as AgentMessagesItem).rows.map((row) => [row.direction, row.message])).toEqual(
      [
        ['received', 'agm-1'],
        ['received', 'agm-2'],
      ],
    );
  });

  it("shows a child's outcome once, from its notice or its wait's result", () => {
    const events = [
      event(1, 0, 'assistant', {
        text: '',
        tool_calls: [call('c1', 'wait_delegations', { delegation_ids: ['dlg-a', 'dlg-b'] })],
      }),
      event(2, 0, 'waiting', { delegation_ids: ['dlg-a', 'dlg-b'], mode: 'all' }),
      event(3, 5, 'tool', {
        call_id: 'c1',
        name: 'wait_delegations',
        result: JSON.stringify({
          reason: 'ended',
          delegations: [
            { delegation_id: 'dlg-a', ended: 'returned', stopped: 'answered' },
            { delegation_id: 'dlg-b', ended: null },
          ],
        }),
        outcome: {},
      }),
      event(4, 6, 'waiting', { delegation_ids: [] }),
      event(5, 7, 'notice', { source: 'delegations', ids: ['dlg-a', 'dlg-b'] }),
      event(6, 8, 'assistant', { text: 'Both are back.', tool_calls: [] }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    expect(items.map((item) => item.kind)).toEqual([
      'work',
      'background',
      'child-outcomes',
      'child-outcomes',
      'reply',
    ]);
    expect((items[2] as ChildOutcomesItem).delegations).toEqual(['dlg-a']);
    expect((items[3] as ChildOutcomesItem).delegations).toEqual(['dlg-b']);
  });

  it('shows a delegation that ended without a wait as its outcome block alone', () => {
    const events = [
      event(1, 0, 'assistant', { text: 'Delegating.', tool_calls: [] }),
      event(2, 3, 'notice', { source: 'delegations', ids: ['dlg-a'] }),
      event(3, 4, 'notice', { source: 'jobs', ids: ['j1'] }),
      event(4, 5, 'notice', { source: 'delegations', ids: ['dlg-a', 'dlg-b'] }),
      event(5, 6, 'assistant', { text: 'Both are back.', tool_calls: [] }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    // No "Subagent · 1 finished" row beside the blocks; a job's notice keeps its row, and
    // a delegation named again is not shown twice.
    expect(items.map((item) => item.kind)).toEqual([
      'reply',
      'child-outcomes',
      'background',
      'child-outcomes',
      'reply',
    ]);
    expect((items[1] as ChildOutcomesItem).delegations).toEqual(['dlg-a']);
    expect((items[2] as BackgroundItem).what).toBe('jobs');
    expect((items[3] as ChildOutcomesItem).delegations).toEqual(['dlg-b']);
  });

  it('keeps the background row of a wait on delegations beside their outcome blocks', () => {
    const events = [
      event(1, 0, 'assistant', {
        text: '',
        tool_calls: [call('c1', 'wait_delegations', { delegation_ids: ['dlg-a'] })],
      }),
      event(2, 0, 'waiting', { delegation_ids: ['dlg-a'], mode: 'all' }),
      event(3, 2, 'notice', { source: 'delegations', ids: ['dlg-a'] }),
      event(4, 3, 'notice', { source: 'jobs', ids: ['j1'] }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    expect(items.map((item) => item.kind)).toEqual([
      'work',
      'background',
      'child-outcomes',
      'background',
    ]);
    const wait = items[1] as BackgroundItem;
    expect([wait.what, wait.waited, wait.finished]).toEqual(['delegations', true, ['dlg-a']]);
    expect((items[3] as BackgroundItem).what).toBe('jobs');
  });

  it('keeps the live group on the wait while a child that ended shows after it', () => {
    const events = [
      event(1, 0, 'assistant', {
        text: '',
        tool_calls: [call('c1', 'wait_delegations', { delegation_ids: ['dlg-a', 'dlg-b'] })],
      }),
      event(2, 0, 'waiting', { delegation_ids: ['dlg-a', 'dlg-b'], mode: 'all' }),
      event(3, 2, 'notice', { source: 'delegations', ids: ['dlg-a'] }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    expect(items.map((item) => item.kind)).toEqual(['work', 'background', 'child-outcomes']);
    expect(liveWork(items)).toBe(0);
  });

  const tree = {
    runs: [
      { name: 'root', role: 'main', parent: null, delegation: null },
      { name: 'child', role: 'delegated', parent: 'root', delegation: 'dlg-a' },
      { name: 'grandchild', role: 'delegated', parent: 'child', delegation: 'dlg-c' },
    ],
    delegations: [
      { delegation_id: 'dlg-a', kind: 'task', brief: { task: 'Count the points\nof the curve.' } },
      { delegation_id: 'dlg-b', kind: 'check', brief: { check: { handle: 'R-3‮' } } },
      { delegation_id: 'dlg-c', kind: 'task', brief: { task: 'Factor N.' } },
    ],
    delegations_total: 3,
  } as unknown as RunTree;
  const view = (fields: Partial<AgentMessageView>): AgentMessageView => ({
    message_id: 'agm-1',
    sender_run: 'child',
    sender_delegation: 'dlg-a',
    recipient_delegation: null,
    body: 'Found it.\nDetails follow.',
    created_at: '2026-10-25T10:00:00Z',
    delivered_run: null,
    references: [],
    ...fields,
  });

  it('names the agents from the run tree, a check by its entry with hidden characters shown', () => {
    const root = agentDirectory(tree, 'root', 'main');
    expect(root.self).toBe('Research agent');
    expect(root.name('dlg-a')).toBe('Count the points');
    expect(root.name('dlg-b')).toBe('Check of R-3⟦U+202E⟧');
    expect(root.name('dlg-x')).toBe('Subagent dlg-x');
    const child = agentDirectory(tree, 'grandchild');
    expect([child.self, child.parent]).toEqual(['Factor N.', 'Count the points']);
    // Before the tree lists it, a run is named by the role it is shown in.
    expect(agentDirectory(null, 'root', 'main').self).toBe('Research agent');
    expect(agentDirectory(null, 'other').self).toBe('This agent');
  });

  it('says who sent what to whom, and how it stands', () => {
    const root = agentDirectory(tree, 'root', 'main');
    const sent = sentRow({
      id: 'c1',
      name: 'message_child',
      arguments: JSON.stringify({ delegation_id: 'dlg-a', text: 'Use q = 2^8.' }),
    });
    expect(messageLine(sent, root, null)).toEqual({
      from: 'Research agent',
      to: 'Count the points',
      preview: 'Use q = 2^8.',
      state: 'sending',
      undelivered: false,
    });
    const received = receivedRow('agm-1');
    // While its body is read, a received message names no sender yet.
    expect(messageLine(received, root, null)).toMatchObject({ from: 'An agent', preview: '' });
    expect(messageLine(received, root, view({}))).toMatchObject({
      from: 'Count the points',
      to: 'Research agent',
      preview: 'Found it.',
      state: 'received',
    });
    // The root's own message comes back when its child ended before seeing it.
    const back = view({
      sender_run: 'root',
      sender_delegation: null,
      recipient_delegation: 'dlg-a',
    });
    expect(messageLine(received, root, back)).toMatchObject({
      from: 'Research agent',
      to: 'Count the points',
      undelivered: true,
    });
    const child = agentDirectory(tree, 'child');
    expect(
      messageLine(
        received,
        child,
        view({ sender_run: 'root', sender_delegation: null, recipient_delegation: 'dlg-a' }),
      ),
    ).toMatchObject({ from: 'Research agent', to: 'Count the points', undelivered: false });
  });

  it('reads each message once, page after page, and again only for one it has not seen', async () => {
    const pages: Record<string, AgentMessagePage> = {
      '': { messages: [view({ message_id: 'agm-1' })], next_after: 'agm-1', has_more: true },
      'agm-1': { messages: [view({ message_id: 'agm-2' })], next_after: 'agm-2', has_more: false },
      'agm-2': { messages: [], next_after: 'agm-2', has_more: false },
    };
    const asked: (string | null)[] = [];
    const bodies = new MessageBodies(async (after) => {
      asked.push(after);
      return pages[after ?? '']!;
    });
    const [one, two] = await Promise.all([bodies.get('agm-2'), bodies.get('agm-1')]);
    expect([one?.message_id, two?.message_id]).toEqual(['agm-2', 'agm-1']);
    expect(asked).toEqual([null, 'agm-1']);
    expect(bodies.known('agm-1')?.body).toBe('Found it.\nDetails follow.');
    expect(await bodies.get('agm-1')).not.toBeNull();
    expect(asked).toHaveLength(2);
    // One it has not seen is looked for after the last one read.
    expect(await bodies.get('agm-9')).toBeNull();
    expect(asked).toEqual([null, 'agm-1', 'agm-2']);
  });
});

describe("a delegation card's link", () => {
  it('opens the session with the delegation chosen in its Agents panel', () => {
    expect(agentHref('p 1', 's1', 'dlg-a/b')).toBe('#/projects/p%201/sessions/s1/agents/dlg-a%2Fb');
    expect(parseRoute(agentHref('p 1', 's1', 'dlg-a/b'))).toEqual({
      view: 'agent',
      project: 'p 1',
      session: 's1',
      delegation: 'dlg-a/b',
    });
    expect(parseRoute('#/projects/p1/sessions/s1/agents/')).toEqual({ view: 'missing' });
  });
});
