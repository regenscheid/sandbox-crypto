// SPDX-License-Identifier: MIT
// The Grants panel (D97): each grant with what it covers, its scope and last use, and
// Revoke, which asks the server and shows the list it answers.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import GrantsPanel from '../views/GrantsPanel.svelte';
import type { ProjectGrant } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const settle = async () => {
  for (let i = 0; i < 4; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
};

const grant: ProjectGrant = {
  grant_id: 'grant-1',
  kind: 'mcp',
  scope: 'project',
  session: null,
  server: 'crypto-research-mcp',
  effect: 'write',
  host: null,
  methods: null,
  ecosystem: null,
  channel: null,
  created_at: '2026-10-24T12:00:00Z',
  expires_at: null,
  last_used_at: '2026-10-24T12:05:00Z',
  uses: 2,
};

it('lists grants and revokes one in one click', async () => {
  const requests: string[] = [];
  const hosts: ProjectGrant = {
    ...grant,
    grant_id: 'grant-2',
    kind: 'network',
    scope: 'session',
    session: 's-1',
    server: null,
    effect: null,
    host: 'data.example.org',
    methods: 'read',
    last_used_at: null,
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      requests.push(`${options?.method ?? 'GET'} ${url}`);
      const grants = options?.method === 'POST' ? [hosts] : [grant, hosts];
      return new Response(JSON.stringify({ project: 'p1', grants }));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(GrantsPanel, { target, props: { projectId: 'p1' } }));
  await settle();
  const rows = [...target.querySelectorAll('.grant-row')].map((row) => row.textContent ?? '');
  expect(rows[0]).toContain('write calls to crypto-research-mcp');
  expect(rows[0]).toContain('This project');
  expect(rows[0]).toContain('last used');
  expect(rows[1]).toContain("Research code's reads from data.example.org");
  expect(rows[1]).toContain('Session s-1');
  expect(rows[1]).toContain('not used yet');
  target.querySelector<HTMLButtonElement>('.grant-row button')!.click();
  await settle();
  expect(requests).toEqual([
    'GET /api/v0/projects/p1/grants',
    'POST /api/v0/projects/p1/grants/grant-1/revoke',
  ]);
  expect(target.querySelectorAll('.grant-row')).toHaveLength(1);
});
