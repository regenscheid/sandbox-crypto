// SPDX-License-Identifier: MIT
// The session's Agents and Context tabs: each agent opens what its model was given (the
// context inspector) without leaving the session page; the research agent comes first.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SessionAgents from '../views/SessionAgents.svelte';
import SessionContext from '../views/SessionContext.svelte';
import type { TreeRun } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const run = (name: string, state: TreeRun['state']): TreeRun => ({
  name,
  run_id: `id-${name}`,
  role: 'main',
  parent: null,
  delegation: null,
  started_at: '2026-10-21T18:00:00Z',
  state,
  stopped: state === 'ended' ? 'answered' : null,
  kept: true,
  alias: null,
  limits: null,
  tool_mix: null,
});

it('offers each agent its context management, read for that run', async () => {
  const fetch = vi.fn(async (_url: string) => new Response('{}', { status: 500 }));
  vi.stubGlobal('fetch', fetch);
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(SessionAgents, {
      target,
      props: {
        projectId: 'p1',
        sessionId: 's1',
        tree: {
          runs: [run('run-1', 'ended'), run('run-2', 'running')],
          delegations: [],
          delegations_total: 0,
          earlier: [],
        },
      },
    }),
  );
  flushSync();
  const buttons = [...target.querySelectorAll<HTMLButtonElement>('.agent-card button')].filter(
    (button) => button.textContent === 'Context management…',
  );
  expect(buttons).toHaveLength(2);
  buttons[0]!.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(String(fetch.mock.calls[0]?.[0])).toContain('/sessions/s1/runs/run-2');
});

it('puts the research agent first in the Context tab, one click from its context', async () => {
  // No saved run record yet, as for a run still in progress: the inspector opens anyway.
  const fetch = vi.fn(async (url: string) =>
    /\/runs\/run-1$/.test(url)
      ? new Response('{}', { status: 404 })
      : new Response('{}', { status: 500 }),
  );
  vi.stubGlobal('fetch', fetch);
  const target = document.createElement('div');
  document.body.append(target);
  const sub = { ...run('run-2', 'ended'), role: 'delegated' as const };
  mounted.push(
    mount(SessionContext, {
      target,
      props: {
        projectId: 'p1',
        sessionId: 's1',
        tree: {
          runs: [run('run-1', 'ended'), sub],
          delegations: [],
          delegations_total: 0,
          earlier: [],
        },
      },
    }),
  );
  flushSync();
  expect(target.querySelector('.main-agent')!.textContent).toContain('Research agent');
  expect(target.querySelector('.main-agent')!.textContent).toContain('run-1');
  target.querySelector<HTMLButtonElement>('.main-agent button')!.click();
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
  expect(String(fetch.mock.calls[0]?.[0])).toContain('/sessions/s1/runs/run-1');
  // It opens on what the model is given, across the agent's continuations: a resumed run
  // may have captured nothing of its own.
  expect(document.querySelector('.context-dialog')).not.toBeNull();
  const urls = fetch.mock.calls.map((call) => String(call[0]));
  expect(urls.some((url) => url.includes('/context/captures?scope=agent'))).toBe(true);
  expect(target.querySelectorAll('.runs li')).toHaveLength(1);
});
