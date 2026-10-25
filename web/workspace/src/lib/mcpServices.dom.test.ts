// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import McpServices from '../views/McpServices.svelte';
import { mcpHref, parseRoute } from './router';

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

it('keeps configured, observed, and unknown service states distinct without live calls', async () => {
  const fetcher = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          project_id: 'p1',
          policy_revision: 'sha256:' + 'a'.repeat(64),
          inspected_runs: 3,
          skipped_runs: 1,
          observation_scope: 'Bounded sample',
          services: [
            {
              name: 'papers',
              purpose: 'Find papers',
              observation: {
                session: 's1',
                run: 'r1',
                at: '2026-10-18T12:00:00Z',
                status: 'listed',
                listed_tools: 3,
              },
            },
            { name: 'archive', purpose: 'Find artifacts', observation: null },
          ],
        }),
      ),
  );
  vi.stubGlobal('innerWidth', 1400);
  vi.stubGlobal('fetch', fetcher);
  expect(parseRoute(mcpHref('p1'))).toEqual({ view: 'mcp', project: 'p1' });
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(McpServices, { target, props: { projectId: 'p1' } }));
  await settle();
  expect(target.querySelectorAll('.mcp-row')).toHaveLength(2);
  const tabs = [...target.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  tabs.find((button) => button.textContent === 'Tools')!.click();
  flushSync();
  expect(target.textContent).toContain('Tool metadata unavailable');
  tabs.find((button) => button.textContent === 'Observations')!.click();
  flushSync();
  expect(target.textContent).toContain('r1');
  const filter = target.querySelector<HTMLSelectElement>('select')!;
  filter.value = 'unobserved';
  filter.dispatchEvent(new Event('input', { bubbles: true }));
  filter.dispatchEvent(new Event('change', { bubbles: true }));
  await settle();
  expect(target.querySelectorAll('.mcp-row')).toHaveLength(1);
  expect(target.querySelector('.mcp-detail h2')?.textContent).toBe('papers');
  target.querySelector<HTMLButtonElement>('.mcp-row')!.click();
  flushSync();
  expect(target.querySelector('.mcp-detail h2')?.textContent).toBe('archive');
  expect(target.querySelector('.mcp-row')?.getAttribute('aria-pressed')).toBe('true');
  // Viewing reads the inventory once, the user's settings and the classifications; it calls
  // no service.
  const urls = fetcher.mock.calls.map((call) => String((call as unknown[])[0]));
  expect(urls.filter((url) => url.endsWith('/mcp-services'))).toHaveLength(1);
  expect(
    urls.filter(
      (url) => !/\/(mcp-services|mcp-services\/settings|grants|classifications)$/.test(url),
    ),
  ).toEqual([]);
});

it('checks availability only after an explicit action and never calls a tool', async () => {
  const requests: string[] = [];
  const inventory = {
    project_id: 'check-p',
    policy_revision: 'a',
    inspected_runs: 0,
    skipped_runs: 0,
    observation_scope: 'Bounded',
    services: [
      {
        name: 'papers',
        purpose: 'Papers',
        observation: null,
        metadata: null,
        proxy_status: 'reachable',
        proxy_checked_at: '2026-10-18T12:00:00Z',
        availability: null,
      },
    ],
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      requests.push(`${options?.method ?? 'GET'} ${url}`);
      return new Response(
        JSON.stringify(
          options?.method === 'POST'
            ? { checked_at: '2026-10-18T12:00:00Z', status: 'listed' }
            : inventory,
        ),
      );
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(McpServices, { target, props: { projectId: 'check-p' } }));
  await settle();
  expect(requests.filter((item) => !/\/(settings|grants|classifications)$/.test(item))).toEqual([
    'GET /api/v0/projects/check-p/mcp-services',
  ]);
  [...target.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === 'Check availability')!
    .click();
  await settle();
  expect(requests.filter((item) => item.startsWith('POST'))).toEqual([
    'POST /api/v0/projects/check-p/mcp-services/papers/check',
  ]);
  expect(requests.every((item) => !item.includes('tools/call'))).toBe(true);
});
