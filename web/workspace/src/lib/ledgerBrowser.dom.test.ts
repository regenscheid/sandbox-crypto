// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import LedgerBoard from '../views/LedgerBoard.svelte';
import LedgerList from '../views/LedgerList.svelte';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 6; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

it('filters the loaded ledger, keeps entry links, and offers the board without losing navigation', async () => {
  vi.stubGlobal('innerWidth', 1400);
  sessionStorage.setItem('sandbox-science:last-session:p1', 's1');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const query = new URL(url, 'http://localhost').searchParams;
      const entries =
        query.get('type') === 'issue' && query.get('status') === 'open'
          ? [
              {
                entry_id: 'entry-' + 'a'.repeat(24),
                handle: 'I-7',
                type: 'issue',
                origin: 'project',
                revision: 3,
                title: 'Check the actual target system',
                status: 'open',
                updated: '2026-10-18T12:00:00Z',
                asserted_only: null,
                open_flags: 0,
                kind: 'research',
                session: null,
                standing: null,
                basis: 'observed',
              },
            ]
          : [];
      return new Response(
        JSON.stringify({
          project_id: 'p1',
          observed_at: '2026-10-18T12:00:00Z',
          page: { entries, next_after: null },
        }),
      );
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(LedgerBoard, { target, props: { projectId: 'p1' } }));
  await settle();
  expect(target.querySelector('a[href="#/projects/p1/sessions/s1"]')).not.toBeNull();
  expect(target.querySelector('a[href="#/projects/p1/ledger/I-7"]')).not.toBeNull();
  const search = target.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'other';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(target.querySelector('a[href="#/projects/p1/ledger/I-7"]')).toBeNull();
  expect(target.textContent).toContain('0 matching 1 loaded entries');
  target.querySelector<HTMLButtonElement>('button[aria-pressed="false"]')!.click();
  await settle();
  expect(target.querySelector('.board-column')).not.toBeNull();
  expect(target.querySelector('nav.trail')).not.toBeNull();
  search.value = '';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();
  target.querySelector<HTMLAnchorElement>('a[href="#/projects/p1/ledger/I-7"]')!.click();
  await settle();
  expect(target.querySelector('.ledger-split')).not.toBeNull();
});

it('retries a failed later page without discarding loaded entries', async () => {
  let secondPageAttempts = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const after = new URL(url, 'http://localhost').searchParams.get('after');
      if (after === 'cursor2' && ++secondPageAttempts === 1)
        return new Response('{}', { status: 503 });
      const handle = after ? 'I-2' : 'I-1';
      return new Response(
        JSON.stringify({
          page: {
            entries: [
              {
                entry_id: `entry-${after ? 'b' : 'a'}`,
                handle,
                type: 'issue',
                origin: 'project',
                revision: 1,
                title: handle,
                status: 'open',
                updated: '2026-10-18T12:00:00Z',
                asserted_only: null,
                open_flags: 0,
                kind: null,
                session: null,
                standing: null,
                basis: null,
              },
            ],
            next_after: after ? null : 'cursor2',
          },
        }),
      );
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(LedgerList, {
      target,
      props: { projectId: 'p1', type: 'issue', status: 'open', search: '', selected: null },
    }),
  );
  await settle();
  target.querySelector<HTMLButtonElement>('button')!.click();
  await settle();
  expect(target.textContent).toContain('Retry open');
  target.querySelector<HTMLButtonElement>('button')!.click();
  await settle();
  expect(secondPageAttempts).toBe(2);
  expect(target.querySelectorAll('.ledger-list-row')).toHaveLength(2);
});

it('resizes ledger details by keyboard up to half of the viewport', async () => {
  vi.stubGlobal('innerWidth', 1400);
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ page: { entries: [], next_after: null } }))),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(LedgerBoard, { target, props: { projectId: 'p1' } }));
  await settle();
  const splitter = target.querySelector<HTMLElement>('[aria-label="Resize ledger detail"]')!;
  splitter.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
  flushSync();
  expect(splitter.getAttribute('aria-valuenow')).toBe('700');
});
