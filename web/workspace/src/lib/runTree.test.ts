// SPDX-License-Identifier: MIT
// A session's run tree as the workspace shows it (PD-42): runs nested under their parents,
// each with its role, state, model and limits; each delegation's brief, read-only, and its
// outcome; agents' text as text with every hidden character as its code point
// (hidden.fixtures.json), and every link leading into the workspace.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import DelegationCard from '../views/DelegationCard.svelte';
import RunTreeNode from '../views/RunTreeNode.svelte';
import type { Delegation, TreeRun } from './api-types';
import { hostileMarkup } from './approvalTesting';
import fixtures from './hidden.fixtures.json';
import { filesHref, parseRoute, runHref, treeHref } from './router';
import {
  briefView,
  delegationsById,
  endedText,
  filesView,
  limitsText,
  readingText,
  resultView,
  runForest,
  stateText,
} from './runTree';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const run = (name: string, fields: Partial<TreeRun> = {}): TreeRun => ({
  name,
  run_id: `run-${name}`,
  role: 'main',
  parent: null,
  delegation: null,
  started_at: '2026-10-15T00:00:00Z',
  state: 'ended',
  stopped: 'final',
  kept: true,
  alias: 'main-model',
  limits: { max_turns: 40, run_share: 0.5 },
  tool_mix: null,
  ...fields,
});

const delegation = (fields: Partial<Delegation> = {}): Delegation => ({
  delegation_id: 'del-1',
  parent: 'a',
  run: 'b',
  kind: 'check',
  brief: {
    task: 'Check C-4 at revision 2.',
    note: 'Look hard at the second lemma.',
    inputs: ['supports, proof: block b3 of doc-1 r2'],
    check: { entry: 'entry-1', handle: 'C-4', revision: 2 },
    limits: { turns: 12, tokens: 40000 },
    model: { alias: 'checker-b', asked: 'checker-a', fallback: true },
  },
  started_at: '2026-10-15T00:00:00Z',
  ended: 'returned',
  stopped: 'final',
  report: 'Confirmed.',
  ended_at: '2026-10-15T00:05:00Z',
  refused: null,
  acknowledged: 'a',
  result: {
    entry: 'C-4',
    revision: 2,
    status: 'complete',
    verdict: 'confirmed',
    summary: 'Every step holds.',
    coverage: { examined: ['the statement', 'lemma 2'], not_examined: [] },
    deviations: [],
    open_questions: [],
  },
  errors: [],
  alias: 'checker-b',
  models: ['model-x'],
  read_entry: true,
  read_proofs: false,
  files: null,
  environment_id: null,
  environment: null,
  ...fields,
});

describe('the run tree', () => {
  it('nests each run under its parent, and puts a run whose parent is not read at the top', () => {
    const forest = runForest([
      run('a'),
      run('b', { role: 'delegated', parent: 'a', delegation: 'del-1' }),
      run('c', { role: 'delegated', parent: 'b' }),
      run('d', { role: 'delegated', parent: 'gone' }),
    ]);
    expect(forest.map((node) => node.run.name)).toEqual(['a', 'd']);
    expect(forest[0]!.children[0]!.run.name).toBe('b');
    expect(forest[0]!.children[0]!.children[0]!.run.name).toBe('c');
  });

  it('loses no run to a cycle of parents', () => {
    const forest = runForest([run('a', { parent: 'b' }), run('b', { parent: 'a' })]);
    expect(forest.map((node) => node.run.name).sort()).toEqual(['a', 'b']);
  });

  it("says each run's state and limits", () => {
    expect(stateText(run('a'))).toBe('ended (final)');
    expect(stateText(run('a', { state: 'running', stopped: null }))).toBe('running');
    expect(stateText(run('a', { state: 'interrupted', stopped: null }))).toBe(
      'interrupted: its process died',
    );
    expect(limitsText({ max_turns: 40, run_share: 0.5 })).toBe('max_turns 40, run_share 0.5');
    expect(limitsText(null)).toBe('');
  });

  it("reads a delegation's brief: the task, the entry, the inputs, the note, the model and limits", () => {
    expect(briefView(delegation())).toEqual({
      task: 'Check C-4 at revision 2.',
      entry: { handle: 'C-4', revision: 2 },
      inputs: ['supports, proof: block b3 of doc-1 r2'],
      note: 'Look hard at the second lemma.',
      model: "checker-b, the project's fallback for checker-a",
      limits: 'turns 12, tokens 40000',
      context: '',
      track: '',
      issue: '',
      delegates: false,
    });
    expect(briefView(delegation({ brief: {} }))).toMatchObject({
      task: '',
      entry: null,
      model: 'the delegated role’s',
    });
  });

  it('says how a delegation ended, what its result claims, and what the platform observed', () => {
    expect(endedText(delegation())).toBe('It returned (final).');
    expect(endedText(delegation({ ended: 'limit_reached', stopped: 'turn_limit' }))).toBe(
      'It ended: limit reached (turn_limit).',
    );
    expect(endedText(delegation({ ended: null }))).toBe('Running: no outcome yet.');
    expect(resultView(delegation())).toMatchObject({
      verdict: 'confirmed',
      status: 'complete',
      examined: ['the statement', 'lemma 2'],
      notExamined: [],
    });
    expect(resultView(delegation({ result: null }))).toBeNull();
    expect(readingText(delegation())).toBe(
      "The platform observed that it read the entry at its revision and did not read each proof's target.",
    );
    expect(delegationsById([delegation()]).get('del-1')?.run).toBe('b');
  });

  it("reads a task's brief and result as a task's, with no verdict and no reading", () => {
    // Lab 41: a lead on a track, which may delegate, and a task on an issue with a file.
    const task = delegation({
      kind: 'task',
      brief: {
        task: 'Complete the orders track.',
        context: 'Curve25519 over GF(2^255-19).',
        inputs: [],
        track: { track_id: 'orders', plan: { plan_id: 'plan-1', version: 1 } },
        issue: { entry: 'entry-9', handle: 'I-1' },
        delegate: true,
        limits: { turns: 20, tokens: 200000 },
        model: { alias: 'sandbox-orchestrator' },
      },
      read_entry: false,
      read_proofs: null,
      result: {
        status: 'partial',
        summary: 'Computed l.',
        obstacle: null,
        deviations: [],
        open_questions: [],
        dismissals: [{ what: 'the twist', limit: 'time' }],
        obstructions: [{ claim: 'No paper copy.', evidence: 'The download failed.' }],
        result_files: ['results/ell.json'],
      },
    });
    expect(briefView(task)).toMatchObject({
      task: 'Complete the orders track.',
      context: 'Curve25519 over GF(2^255-19).',
      track: 'orders of plan plan-1 version 1',
      issue: 'I-1',
      delegates: true,
      entry: null,
    });
    expect(resultView(task)).toMatchObject({
      verdict: '',
      status: 'partial',
      dismissals: ['the twist: time'],
      obstructions: ['No paper copy.: The download failed.'],
      resultFiles: ['results/ell.json'],
    });
    expect(readingText(task)).toBe('');
    const html = render(DelegationCard, {
      props: { projectId: 'p1', sessionId: 's1', delegation: task },
    }).body;
    expect(html).toContain('Status: partial');
    expect(html).not.toContain('Verdict');
    expect(html).not.toContain('read the entry');
    for (const shown of [
      'Context from its parent',
      "On the plan's track orders of plan plan-1 version 1",
      'On the issue I-1',
      'It may delegate tasks of its own.',
      'the twist: time',
      'Its declared result files',
      'results/ell.json',
    ]) {
      expect(html).toContain(shown);
    }
    const check = render(DelegationCard, {
      props: { projectId: 'p1', sessionId: 's1', delegation: delegation() },
    }).body;
    expect(check).toContain('Verdict: confirmed');
    expect(check).not.toContain('It may delegate');
  });

  it('leads to a run by its name, and to the tree', () => {
    expect(parseRoute(treeHref('p 1', 's/1'))).toEqual({
      view: 'tree',
      project: 'p 1',
      session: 's/1',
    });
    expect(parseRoute(runHref('p1', 's1', '2026-10-15T00:00:00Z-run-1'))).toEqual({
      view: 'run',
      project: 'p1',
      session: 's1',
      run: '2026-10-15T00:00:00Z-run-1',
    });
  });
});

describe('the run tree views', () => {
  it('render no text as HTML', () => {
    const views = import.meta.glob(
      [
        '../views/DelegationCard.svelte',
        '../views/RunTreeNode.svelte',
        '../views/RunTreeView.svelte',
        '../views/RunEventsView.svelte',
      ],
      { query: '?raw', import: 'default', eager: true },
    );
    expect(Object.keys(views)).toHaveLength(4);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });

  it('show runs and their delegations as text, every link leading into the workspace', () => {
    const hostile = delegation({
      delegation_id: HOSTILE,
      brief: {
        task: HOSTILE,
        note: `${HOSTILE} [a](javascript:alert(1))`,
        inputs: [HOSTILE],
        check: { handle: 'C-4', revision: 2 },
        limits: { [HOSTILE]: HOSTILE },
        model: { alias: HOSTILE },
      },
      report: HOSTILE,
      refused: HOSTILE,
      errors: [HOSTILE],
      alias: HOSTILE,
      models: [HOSTILE],
      result: {
        verdict: HOSTILE,
        status: HOSTILE,
        summary: HOSTILE,
        refutation: HOSTILE,
        coverage: { examined: [HOSTILE], not_examined: [HOSTILE] },
      },
    });
    const html = render(RunTreeNode, {
      props: {
        projectId: 'p1',
        sessionId: 's1',
        node: {
          run: run('a', { run_id: HOSTILE, alias: HOSTILE, stopped: HOSTILE }),
          children: [
            {
              run: run(`b/${HOSTILE}`, { role: 'delegated', parent: 'a', delegation: 'del-1' }),
              children: [],
            },
          ],
        },
        delegations: delegationsById([{ ...hostile, delegation_id: 'del-1' }]),
      },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect([...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1])).toEqual([
      '#/projects/p1/sessions/s1/runs/a',
      `#/projects/p1/sessions/s1/runs/${encodeURIComponent(`b/${HOSTILE}`)}`,
      '#/projects/p1/ledger/C-4/revisions/2',
    ]);
    expect(html).toContain("The parent's note, which is not evidence");
  });

  const places = [
    "the parent's note",
    'the summary',
    'the refutation',
    'the report',
    "a task's context",
    "a task's track",
  ];
  const cases = fixtures.cases.flatMap((c) =>
    places.map((where) => ({ ...c, where, hidden: c.hidden as [number, string][] })),
  );
  it.each(cases)(
    '$name, in $where: each hidden character as its code point',
    ({ text, where, hidden }) => {
      const base = delegation();
      const shown =
        where === "the parent's note"
          ? delegation({ brief: { ...base.brief, note: text } })
          : where === "a task's context"
            ? delegation({ kind: 'task', brief: { task: 'T.', context: text } })
            : where === "a task's track"
              ? delegation({ kind: 'task', brief: { task: 'T.', track: { track_id: text } } })
              : where === 'the report'
                ? delegation({ report: text })
                : delegation({
                    result: {
                      ...base.result,
                      verdict: 'refuted',
                      ...(where === 'the summary' ? { summary: text } : { refutation: text }),
                    },
                  });
      const html = render(DelegationCard, {
        props: { projectId: 'p1', sessionId: 's1', delegation: shown },
      }).body;
      for (const [, code] of hidden) expect(html).toContain(`⟦${code}⟧`);
    },
  );
});

const SHA = 'ab'.repeat(32);
const file = (path: string, fields: Record<string, unknown> = {}) => ({
  path,
  artifact_id: 'art-1',
  revision: 2,
  sha256: SHA,
  size: 2048,
  content_retained: true,
  deleted: false,
  reproduced: false,
  ...fields,
});

describe('the files a delegated run recorded', () => {
  it('lists each, what its revision is, and what the calls could not record', () => {
    const found = filesView(
      delegation({
        files: {
          total: 5,
          files: [
            file('out/table.csv'),
            file('out/old.csv', { deleted: true, revision: 3, size: 0, content_retained: false }),
            file('out/big.bin', { content_retained: false, reproduced: true }),
          ],
          gaps: { incomplete: 1, failed: 2, 'larger than 4 GiB': 3, ignored: 0 },
        },
      }),
    )!;
    expect(found.total).toBe(5);
    expect(found.files).toEqual([
      { path: 'out/table.csv', what: 'art-1, written r2, 2.0 KiB, sha256 abababababab…' },
      { path: 'out/old.csv', what: 'art-1, deleted r3: a tombstone' },
      {
        path: 'out/big.bin',
        what: 'art-1, the same bytes again r2, 2.0 KiB, sha256 abababababab…; bytes not kept',
      },
    ]);
    expect(found.gaps).toEqual([
      '1 list the worker cut short',
      '2 recordings that stopped part way',
      '3 left out: larger than 4 GiB',
    ]);
    expect(filesView(delegation({ files: null }))).toBeNull();
  });

  it('show paths as text, isolated, leading to the artifact list of the run', () => {
    const shown = (runId: string | null, path: string) =>
      render(DelegationCard, {
        props: {
          projectId: 'p1',
          sessionId: 's1',
          runId,
          delegation: delegation({
            files: { total: 250, files: [file(path)], gaps: { [HOSTILE]: 1 } },
          }),
        },
      }).body;
    const html = shown('run-child', `${HOSTILE}/‮gnp.exe`);
    expect(hostileMarkup(html)).toBe(false);
    expect(html).toContain('<bdi>');
    expect(html).toContain('⟦U+202E⟧gnp.exe');
    expect(html).toContain('Files its calls recorded: 250, the first 1 listed');
    expect([...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1])).toContain(
      '#/projects/p1/files/s1/run-child',
    );
    // The artifact list takes identifiers only: no link for another run ID.
    expect(shown('../x', 'a').includes('/files/')).toBe(false);
    expect(shown(null, 'a').includes('/files/')).toBe(false);
    expect(parseRoute(filesHref('p 1', 's1', 'run-child'))).toEqual({
      view: 'files',
      project: 'p 1',
      session: 's1',
      run: 'run-child',
    });
  });

  const pathCases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(pathCases)(
    '$name, in a path: each hidden character as its code point',
    ({ text, hidden }) => {
      const html = render(DelegationCard, {
        props: {
          projectId: 'p1',
          sessionId: 's1',
          delegation: delegation({ files: { total: 1, files: [file(text)] } }),
        },
      }).body;
      for (const [, code] of hidden) expect(html).toContain(`⟦${code}⟧`);
    },
  );
});
