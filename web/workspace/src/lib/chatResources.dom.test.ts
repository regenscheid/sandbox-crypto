// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatResources from '../views/ChatResources.svelte';
import type { ResourceRequest } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const work = '20261018T000000000000Z-check';
const notice: ResourceRequest = {
  request_id: 'resource-a',
  work,
  current: work,
  epoch: 2,
  parent: '20261018T000000000000Z-parent',
  resource: 'tokens',
  grant_revision: 0,
  used: 20_000_000,
  total: 20_000_000,
  checkpoint_message_id: 'msg-checkpoint',
  created_at: 100,
  task: 'Verify the finite-field reference calculation',
  work_state: 'running',
  superseded: false,
  decision: null,
  scope: 'work',
  renewable: true,
  summary: null,
};
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

function button(target: HTMLElement, label: string): HTMLButtonElement {
  const found = [...target.querySelectorAll('button')].find((node) =>
    node.textContent?.includes(label),
  );
  if (!found) throw new Error(`missing ${label}`);
  return found;
}

function view() {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ChatResources, { target, props: { projectId: 'p', sessionId: 's' } }));
  return target;
}

it('shows a running check-in with its checkpoint and offers no token approval', async () => {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(url);
      return reply({ requests: [notice], next_after: null });
    }),
  );
  const target = view();
  await settle();
  expect(target.textContent).toContain(notice.task);
  expect(target.textContent).toContain('20,000,000 of 20,000,000');
  expect(target.textContent).toContain('Awaiting review');
  expect(target.querySelector('a')?.getAttribute('href')).toContain('msg-checkpoint');
  expect(calls.some((url) => url.includes('/token-grants'))).toBe(false);
  expect(target.textContent).not.toContain('Review resource allowance');
});

it('lets the user extend a paused root soft cap by one step', async () => {
  const writes: unknown[] = [];
  const root = { ...notice, parent: null, work_state: 'paused' as const };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        writes.push(JSON.parse(String(init.body)));
        return reply({ total: 40_000_000, operator: 'user' });
      }
      return reply({ requests: [root], next_after: null });
    }),
  );
  const target = view();
  await settle();
  button(target, 'Review resource allowance').click();
  await settle();
  button(target, 'Approve 20,000,000 more tokens').click();
  await settle();
  expect(writes).toEqual([
    {
      request_id: notice.request_id,
      current: work,
      epoch: 2,
      total: 20_000_000,
      action: 'extend',
      reason: 'Continue from the saved research checkpoint.',
    },
  ]);
});

it('names an automatic check-in decision and the loop heuristic', async () => {
  const loop = {
    ...notice,
    resource: 'loop' as const,
    used: 8,
    total: 8,
    summary: {
      brief: 'b',
      produced: [],
      recent: [],
      answer: null,
      rule: 'same_call',
      detail: '8 calls of shell with the same arguments',
      tokens: 0,
      turns: 9,
      seconds: 0,
      cost: 0,
      progress: 0,
    },
    decision: {
      request: { request_id: 'resource-a', action: 'extend' as const, reason: 'one step' },
      parent_run: work,
      operation: 'check-in:resource-a',
      total: 8,
      observed: 1,
      operator: null,
      progress: 0,
      resource: 'loop' as const,
      automatic: true,
    },
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => reply({ requests: [loop], next_after: null })),
  );
  const target = view();
  await settle();
  expect(target.textContent).toContain('8 calls of shell with the same arguments');
  expect(target.textContent).toContain('Automatic check-in: extend');
});

it('marks completed check-ins as historical and offers no token approval', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      reply({ requests: [{ ...notice, work_state: 'completed' }], next_after: null }),
    ),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ChatResources, { target, props: { projectId: 'p', sessionId: 's' } }));
  await settle();
  expect(target.textContent).toContain('Historical check-in');
  expect(target.textContent).not.toContain('Awaiting review');
  expect(target.textContent).not.toContain('Review resource allowance');
});

it('shows retained-artifact scope and locks one USER soft extension to its reviewed attempt', async () => {
  const writes: unknown[] = [];
  const soft = {
    ...notice,
    resource: 'retained_bytes' as const,
    total: 1000,
    used: 800,
    work_state: 'paused' as const,
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        writes.push(JSON.parse(String(init.body)));
        return reply({ total: 2000, operator: 'user' });
      }
      return reply({ requests: [soft], next_after: null });
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ChatResources, { target, props: { projectId: 'p', sessionId: 's' } }));
  await settle();
  expect(target.textContent?.replace(/\s+/g, ' ')).toContain(
    'research VM disk usage are not measured',
  );
  button(target, 'Review resource allowance').click();
  await settle();
  const approve = button(target, 'Approve 1 GiB');
  approve.click();
  approve.click();
  await settle();
  expect(writes).toEqual([
    {
      request_id: notice.request_id,
      current: work,
      epoch: 2,
      total: 1000,
      action: 'extend_storage',
      reason: 'Continue from the saved research checkpoint.',
    },
  ]);
  expect(target.textContent).toContain('eligible for automatic Resume');
});

it('never offers to extend hard retention capacity', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      reply({
        requests: [{ ...notice, resource: 'retention_bytes', renewable: false, scope: 'project' }],
        next_after: null,
      }),
    ),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ChatResources, { target, props: { projectId: 'p', sessionId: 's' } }));
  await settle();
  expect(target.textContent).toContain('Hard retention capacity');
  expect(target.textContent).not.toContain('Review resource allowance');
});
