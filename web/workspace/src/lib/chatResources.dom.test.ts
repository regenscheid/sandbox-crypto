// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatResources from '../views/ChatResources.svelte';
import TokenAllowance from '../views/TokenAllowance.svelte';
import type { ResourceRequest, TokenGrantStatus } from './api-types';

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
const path = '/projects/p/sessions/s/recovery';
const grant: TokenGrantStatus = {
  work,
  current: work,
  epoch: 2,
  revision: 0,
  total_tokens: 20_000_000,
  used_tokens: 20_000_001,
  next_total_tokens: 30_000_000,
  blocked_reason: null,
  blocked_ancestor_work: null,
};
const notice: ResourceRequest = {
  request_id: 'resource-a',
  work,
  current: work,
  epoch: 2,
  parent: '20261018T000000000000Z-parent',
  resource: 'tokens',
  grant_revision: 0,
  used: 16_000_000,
  total: 20_000_000,
  checkpoint_message_id: 'msg-checkpoint',
  created_at: 100,
  task: 'Verify the finite-field reference calculation',
  work_state: 'paused',
  superseded: false,
  user_grant_revision: null,
  decision: null,
};
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

function showAllowance() {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(TokenAllowance, { target, props: { path, work, warningTotal: 20_000_000 } }));
  return target;
}

function button(target: HTMLElement, label: string): HTMLButtonElement {
  const found = [...target.querySelectorAll('button')].find((node) =>
    node.textContent?.includes(label),
  );
  if (!found) throw new Error(`missing ${label}`);
  return found;
}

it('shows the branch progress and archived checkpoint before offering a token review', async () => {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(url);
      return reply(url.includes('/resources?') ? { requests: [notice], next_after: null } : grant);
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ChatResources, { target, props: { projectId: 'p', sessionId: 's' } }));
  await settle();
  expect(target.textContent).toContain(notice.task);
  expect(target.textContent).toContain('16,000,000 of 20,000,000');
  expect(target.querySelector('a')?.getAttribute('href')).toContain('msg-checkpoint');
  expect(calls.some((url) => url.includes('/token-grants/'))).toBe(false);
  button(target, 'Review token allowance').click();
  await settle();
  expect(target.textContent).toContain('30,000,000 total');
});

it('pins one approval to the reviewed revision and does not offer a second increment', async () => {
  const writes: unknown[] = [];
  let current = { ...grant };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        writes.push(JSON.parse(String(init.body)));
        current = {
          ...grant,
          revision: 1,
          total_tokens: 30_000_000,
          next_total_tokens: 40_000_000,
        };
        return reply({ revision: 1 });
      }
      return reply(current);
    }),
  );
  const target = showAllowance();
  await settle();
  const approve = button(target, 'Approve 10,000,000');
  approve.click();
  approve.click();
  await settle();
  expect(writes).toEqual([
    {
      work,
      current: work,
      epoch: 2,
      expected_revision: 0,
      total_tokens: 30_000_000,
      reason: 'Continue this research branch with 10,000,000 more tokens.',
    },
  ]);
  expect(target.textContent).toContain('The allowance has increased');
  expect(target.textContent).not.toContain('Approve 10,000,000');
});

it('reconciles a lost write response by reading, without sending another approval', async () => {
  let writes = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        writes++;
        throw new Error('connection lost after commit');
      }
      return reply(writes ? { ...grant, revision: 1, total_tokens: 30_000_000 } : grant);
    }),
  );
  const target = showAllowance();
  await settle();
  button(target, 'Approve 10,000,000').click();
  await settle();
  expect(writes).toBe(1);
  expect(target.textContent).toContain('The allowance has increased');
  button(target, 'Refresh allowance').click();
  await settle();
  expect(writes).toBe(1);
});

it('shows the ancestor or active-tree blocker without offering an approval', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      reply({
        ...grant,
        next_total_tokens: null,
        blocked_reason: 'Ancestor needs its own approved token extension first',
      }),
    ),
  );
  const target = showAllowance();
  await settle();
  expect(target.textContent).toContain('Ancestor needs its own approved');
  expect(target.textContent).not.toContain('Approve 10,000,000');
});

it('keeps an unknown approval locked even if the follow-up read fails', async () => {
  let writes = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      if (init.method === 'POST') writes++;
      if (writes) throw new Error('offline');
      return reply(grant);
    }),
  );
  const target = showAllowance();
  await settle();
  button(target, 'Approve 10,000,000').click();
  await settle();
  expect(writes).toBe(1);
  expect(target.textContent).not.toContain('Approve 10,000,000');
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
  expect(target.textContent).not.toContain('Review token allowance');
});

it('allows separately reviewed ancestor and child increments when the ancestor had no warning', async () => {
  const parent = '20261018T000000000000Z-parent';
  const writes: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        const body = JSON.parse(String(init.body));
        writes.push(body.work);
        expect(body.total_tokens).toBe(30_000_000);
        return reply({ revision: 1 });
      }
      if (url.endsWith(parent))
        return reply({
          ...grant,
          work: parent,
          current: parent,
          used_tokens: 1_000_000,
          total_tokens: writes.includes(parent) ? 30_000_000 : 20_000_000,
        });
      return reply({
        ...grant,
        total_tokens: writes.includes(work) ? 30_000_000 : 20_000_000,
        next_total_tokens: writes.includes(parent) ? 30_000_000 : null,
        blocked_reason: writes.includes(parent) ? null : 'Ancestor needs approval first',
        blocked_ancestor_work: writes.includes(parent) ? null : parent,
      });
    }),
  );
  const target = showAllowance();
  await settle();
  button(target, 'Review ancestor allowance').click();
  await settle();
  expect(target.textContent).toContain('1,000,000 tokens used');
  button(target, 'Approve 10,000,000').click();
  await settle();
  expect(writes).toEqual([parent]);
  const refreshes = [...target.querySelectorAll('button')].filter((node) =>
    node.textContent?.includes('Refresh allowance'),
  );
  refreshes.at(-1)!.click();
  await settle();
  button(target, 'Approve 10,000,000').click();
  await settle();
  expect(writes).toEqual([parent, work]);
});
