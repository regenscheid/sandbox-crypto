// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import McpServices from '../views/McpServices.svelte';
import { mcpHref, parseRoute } from './router';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
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
  expect(target.textContent).toContain('listed 3 tools');
  expect(target.textContent?.replace(/\s+/g, ' ')).toContain(
    'does not retain service-scoped tool names',
  );
  tabs.find((button) => button.textContent === 'Observations')!.click();
  flushSync();
  expect(target.textContent).toContain('r1');
  const filter = target.querySelector<HTMLSelectElement>('select')!;
  filter.value = 'unobserved';
  filter.dispatchEvent(new Event('input', { bubbles: true }));
  filter.dispatchEvent(new Event('change', { bubbles: true }));
  await settle();
  expect(target.querySelectorAll('.mcp-row')).toHaveLength(1);
  expect(target.querySelector('.mcp-detail h2')?.textContent).toBe('archive');
  expect(target.querySelector('.mcp-row')?.getAttribute('aria-pressed')).toBe('true');
  expect(fetcher).toHaveBeenCalledTimes(1);
});
