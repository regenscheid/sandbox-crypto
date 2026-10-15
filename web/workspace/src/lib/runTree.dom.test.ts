// SPDX-License-Identifier: MIT
// The run tree and a run's events, mounted: the tree read and nested, its delegations under
// their runs and apart, read again on request, a failed read; and a run's events read
// page by page, its newer events on request while it runs.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import RunEventsView from '../views/RunEventsView.svelte';
import RunTreeView from '../views/RunTreeView.svelte';
import type { Delegation, RunEvent, RunTree, TreeRun } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

function show<P extends Record<string, any>>(component: Component<P>, props: P): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

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
  limits: null,
  ...fields,
});

const delegation = (id: string, child: string, fields: Partial<Delegation> = {}): Delegation => ({
  delegation_id: id,
  parent: 'root',
  run: child,
  kind: 'check',
  brief: {
    task: `Task of ${id}.`,
    note: `Note of ${id}.`,
    inputs: [],
    check: { handle: 'C-4', revision: 2 },
    limits: { turns: 12, tokens: 40000 },
  },
  started_at: '2026-10-15T00:00:00Z',
  ended: null,
  stopped: null,
  report: null,
  ended_at: null,
  refused: null,
  acknowledged: null,
  result: null,
  errors: [],
  alias: null,
  models: [],
  read_entry: null,
  read_proofs: null,
  files: null,
  ...fields,
});

function treeServer(answers: (RunTree | number)[]): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      urls.push(url);
      const found = answers[Math.min(urls.length - 1, answers.length - 1)]!;
      return typeof found === 'number'
        ? new Response(
            JSON.stringify({ error: { kind: 'unavailable', message: 'the tree cannot be read' } }),
            { status: found },
          )
        : new Response(JSON.stringify(found));
    }),
  );
  return urls;
}

const tree: RunTree = {
  runs: [
    run('root', { state: 'running', stopped: null, kept: false }),
    run('child', {
      role: 'delegated',
      parent: 'root',
      delegation: 'del-1',
      state: 'running',
      stopped: null,
      kept: false,
      alias: 'checker-b',
    }),
  ],
  delegations: [delegation('del-1', 'child'), delegation('del-0', 'gone')],
  delegations_total: 3,
};

describe('the run tree', () => {
  it('nests the delegated run under its parent, with its delegation, and the rest apart', async () => {
    const urls = treeServer([tree]);
    const root = show(RunTreeView, { projectId: 'p1', sessionId: 's1' });
    await settle();
    expect(urls).toEqual(['/api/v0/projects/p1/sessions/s1/tree']);
    const top = root.querySelector('ul.tree > li.tree-run')!;
    expect(top.querySelector(':scope > p')!.textContent).toContain('Main run');
    const nested = top.querySelector('ul.tree > li.tree-run')!;
    expect(nested.textContent).toContain('Delegated run');
    expect(nested.textContent).toContain('model checker-b');
    expect(nested.textContent).toContain('Task of del-1.');
    expect(nested.textContent).toContain('Running: no outcome yet.');
    expect(nested.querySelector('a')!.getAttribute('href')).toBe(
      '#/projects/p1/sessions/s1/runs/child',
    );
    expect(root.textContent).toContain('Delegations whose runs are not listed');
    expect(root.textContent).toContain('Task of del-0.');
    expect(root.textContent!.replace(/\s+/g, ' ')).toContain(
      "The latest 2 of the session's 3 delegations are listed.",
    );
  });

  it('is read again on request, and shows a failed read', async () => {
    const ended: RunTree = {
      ...tree,
      runs: tree.runs.map((r) => ({ ...r, state: 'ended', stopped: 'final', kept: true })),
      delegations: [
        delegation('del-1', 'child', {
          ended: 'returned',
          stopped: 'final',
          result: {
            entry: 'C-4',
            revision: 2,
            status: 'partial',
            verdict: 'refuted',
            summary: 's',
            refutation: 'Step 3 fails for p = 2.',
            coverage: { examined: ['step 3'], not_examined: ['step 4'] },
          },
        }),
      ],
      delegations_total: 1,
    };
    const urls = treeServer([tree, ended, 503]);
    const root = show(RunTreeView, { projectId: 'p1', sessionId: 's1' });
    await settle();
    [...root.querySelectorAll('button')].find((b) => b.textContent === 'Read again')!.click();
    await settle();
    expect(root.textContent).toContain('Verdict: refuted');
    expect(root.textContent).toContain('Step 3 fails for p = 2.');
    expect(root.textContent).toContain('step 4');
    [...root.querySelectorAll('button')].find((b) => b.textContent === 'Read again')!.click();
    await settle();
    expect(urls).toHaveLength(3);
    expect(root.textContent).toContain('the tree cannot be read');
  });
});

const event = (seq: number, text: string): RunEvent => ({
  seq,
  at: '2026-10-15T00:00:00Z',
  kind: 'assistant',
  data: { text },
});

describe("a run's events", () => {
  it('are read from the start, and newer ones on request while it runs', async () => {
    const pages: Record<string, { state: string; events: RunEvent[] }> = {
      '0': { state: 'active', events: [event(1, 'Reading C-4.'), event(2, 'Lemma 2 holds.')] },
      '2': { state: 'ended', events: [event(3, 'Confirmed.')] },
    };
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(url);
        const after = new URL(url, 'http://localhost').searchParams.get('after')!;
        return new Response(JSON.stringify(pages[after]));
      }),
    );
    const root = show(RunEventsView, { projectId: 'p1', sessionId: 's1', run: 'child' });
    await settle();
    expect(root.textContent).toContain('Lemma 2 holds.');
    expect(root.textContent).toContain('Running');
    [...root.querySelectorAll('button')]
      .find((b) => b.textContent === 'Read newer events')!
      .click();
    await settle();
    expect(urls).toEqual([
      '/api/v0/projects/p1/sessions/s1/runs/child/events?after=0&limit=200',
      '/api/v0/projects/p1/sessions/s1/runs/child/events?after=2&limit=200',
    ]);
    expect(root.textContent).toContain('Confirmed.');
    expect(root.textContent).toContain('Ended.');
    expect(
      [...root.querySelectorAll('button')].some((b) => b.textContent?.includes('events')),
    ).toBe(false);
  });
});
