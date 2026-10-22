// SPDX-License-Identifier: MIT
// The context inspector's timeline keeps its chart while the session's run tree is polled
// (owner, 2026-10-21: the chart vanished for half a second now and then). Each poll gives
// an equal run with a new object, and `run={run.name}` reads that object.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SessionAgents from '../views/SessionAgents.svelte';
import type { RunTree, TreeRun } from './api-types';
import { reactiveProps } from './reactiveProps.svelte';

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

const run = (name: string, state: TreeRun['state']): TreeRun => ({
  name,
  run_id: name,
  role: 'main',
  parent: null,
  delegation: null,
  started_at: '2026-10-22T02:40:00Z',
  state,
  stopped: state === 'running' ? null : 'answered',
  kept: true,
  alias: null,
  limits: null,
});
const tree = (): RunTree => ({
  runs: [run('run-old', 'ended'), run('run-live', 'running')],
  delegations: [],
  delegations_total: 0,
});

it('reads the timeline once across polls of the run tree', async () => {
  const reads: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.includes('/context/timeline')) {
        reads.push(url);
        const page = {
          answers: [],
          folds: [],
          auxiliary: [],
          answers_after: 0,
          events_after: 0,
          more_answers: false,
          more_events: false,
          more_auxiliary: false,
          coverage: '',
        };
        return new Response(JSON.stringify(page));
      }
      // A running attempt has no run record yet.
      return new Response(JSON.stringify({ error: { kind: 'not_found', message: 'none' } }), {
        status: 404,
      });
    }),
  );
  const props = reactiveProps({ projectId: 'p', sessionId: 's', tree: tree() });
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(SessionAgents, { target, props }));
  await settle();
  const open = [...target.querySelectorAll('button')].find((item) =>
    item.textContent?.startsWith('Context management'),
  )!;
  open.click();
  await settle();
  expect(reads).toHaveLength(1);
  expect(target.textContent).toContain('Displaying 0 answer observations');
  props.tree = tree();
  flushSync();
  expect(target.textContent).not.toContain('Loading attempt observations');
  expect(target.textContent).toContain('Displaying 0 answer observations');
  await settle();
  expect(reads).toHaveLength(1);
});
