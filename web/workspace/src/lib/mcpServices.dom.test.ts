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
          checks: true,
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
              listing_state: 'none',
            },
            {
              name: 'archive',
              purpose: 'Find artifacts',
              observation: null,
              listing_state: 'unavailable',
            },
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
  expect(tabs.map((button) => button.textContent)).toEqual([
    'Overview',
    'Tools',
    'Observations',
    'Classification',
  ]);
  tabs.find((button) => button.textContent === 'Tools')!.click();
  flushSync();
  expect(target.textContent).toContain('No listing has been reported yet');
  expect(target.querySelector('.mcp-listing-state')?.textContent).toContain('none reported yet');
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
    checks: true,
    services: [
      {
        name: 'papers',
        purpose: 'Papers',
        observation: null,
        listing: null,
        listing_state: 'none',
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
            ? { checked_at: '2026-10-18T12:00:00Z', status: 'listed', tool_count: 5 }
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

it("shows the latest scheduled canary beside the user's own check", async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-25T12:12:00Z'));
  const service = {
    purpose: 'Papers',
    observation: null,
    listing: null,
    listing_state: 'none',
    availability: null,
  };
  const inventory = {
    project_id: 'canary-p',
    policy_revision: 'a',
    inspected_runs: 0,
    skipped_runs: 0,
    observation_scope: 'Bounded',
    checks: false,
    services: [
      {
        ...service,
        name: 'papers',
        canary: { at: '2026-10-25T12:00:00Z', status: 'listed', tool_count: 12, elapsed_ms: 340 },
      },
      { ...service, name: 'archive', canary: null },
    ],
  };
  const requests: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      requests.push(`${options?.method ?? 'GET'} ${url}`);
      return new Response(JSON.stringify(inventory));
    }),
  );
  try {
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(mount(McpServices, { target, props: { projectId: 'canary-p' } }));
    await settle();
    const line = () => target.querySelector('.mcp-canary')?.textContent?.replace(/\s+/g, ' ');
    expect(line()).toContain('Canary: listed 12 minutes ago · 12 tools · 340 ms.');
    expect(target.textContent).toContain('Availability: Unknown');
    target.querySelectorAll<HTMLButtonElement>('.mcp-row')[1]!.click();
    flushSync();
    expect(line()).toContain('Canary: none recorded.');
    // Reading the canary calls nothing: no check, no tool.
    expect(requests.filter((item) => item.startsWith('POST'))).toEqual([]);
  } finally {
    vi.useRealTimers();
  }
});

it("shows the runtime's latest reported listing, each tool with its class and decision", async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-26T09:05:00Z'));
  const inventory = {
    project_id: 'tools-p',
    policy_revision: 'a',
    inspected_runs: 0,
    skipped_runs: 0,
    observation_scope: 'Bounded',
    checks: false,
    services: [
      {
        name: 'papers',
        purpose: 'Papers',
        observation: null,
        availability: null,
        canary: null,
        listing_state: 'reported',
        listing: {
          server: 'papers',
          reported_at: '2026-10-26T09:00:00Z',
          session: 's1',
          run: 'main-1',
          listing: 'f'.repeat(64),
          drifted: true,
          tools: [
            { name: 'search', hint: 'read', effect: 'read', classified: true, action: 'allow' },
            {
              name: 'submit',
              hint: 'read',
              effect: 'write',
              classified: true,
              action: 'escalate',
            },
            { name: 'reset', hint: 'admin', effect: 'admin', classified: false, action: 'deny' },
          ],
        },
      },
    ],
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(inventory))),
  );
  try {
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(mount(McpServices, { target, props: { projectId: 'tools-p' } }));
    await settle();
    expect(target.querySelector('.mcp-listing-state')?.textContent?.replace(/\s+/g, ' ')).toContain(
      'reported 5 minutes ago · drifted from its pin',
    );
    // Without a check configured, the button says so and asks nothing.
    const button = [...target.querySelectorAll<HTMLButtonElement>('button')].find(
      (item) => item.textContent === 'Check availability',
    )!;
    expect(button.disabled).toBe(true);
    expect(target.textContent).toContain('The availability check is not configured');
    [...target.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find((item) => item.textContent === 'Tools')!
      .click();
    flushSync();
    const rows = [...target.querySelectorAll('.mcp-tools li')].map((item) =>
      item.textContent?.replace(/\s+/g, ' ').trim(),
    );
    expect(rows).toEqual([
      'search · read · allowed',
      'submit · write · asks you first',
      'reset · admin (by its hint) · denied',
    ]);
    expect(target.textContent).toContain('This listing drifted');
  } finally {
    vi.useRealTimers();
  }
});
