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
import { parseRoute, runHref, treeHref } from './router';
import {
  briefView,
  delegationsById,
  endedText,
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

  const places = ["the parent's note", 'the summary', 'the refutation', 'the report'];
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
          : where === 'the report'
            ? delegation({ report: text })
            : delegation({
                result: {
                  ...base.result,
                  verdict: 'refuted',
                  ...(where === 'the summary' ? { summary: text } : { refutation: text }),
                },
              });
      const html = render(DelegationCard, { props: { projectId: 'p1', delegation: shown } }).body;
      for (const [, code] of hidden) expect(html).toContain(`⟦${code}⟧`);
    },
  );
});
