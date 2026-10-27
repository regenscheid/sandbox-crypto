// SPDX-License-Identifier: MIT
// The Monitor view (D103): its sections from the route's figures, the parts it says are
// unavailable, and the window switch, which reads the other window.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import MonitorPage from '../views/MonitorPage.svelte';
import type { ProjectMonitor } from './api-types';
import { browserState } from './browserState';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

const settle = async () => {
  for (let i = 0; i < 6; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
};

const COST = {
  last_day: 0.625,
  total: 10.625,
  by_role: [],
  model_info_last_day: null,
  model_info_total: null,
};

function figures(window: '24h' | '7d'): ProjectMonitor {
  return {
    project_id: 'p1',
    window,
    since: '2026-10-24T12:00:00Z',
    at: '2026-10-25T12:00:00Z',
    worker: {
      project_id: 'p1',
      window,
      since: '2026-10-24T12:00:00Z',
      at: '2026-10-25T12:00:00Z',
      unavailable: null,
      records: {
        sources: ['policy-middleware'],
        scanned: 40,
        truncated: false,
        calls: [
          {
            effect: 'read',
            server: 'papers',
            host: null,
            allowed: 6,
            escalated: 0,
            denied: 1,
            held: 1,
          },
          {
            effect: 'network',
            server: null,
            host: 'data.org',
            allowed: 1,
            escalated: 2,
            denied: 0,
            held: 0,
          },
        ],
        approvals: [
          {
            kind: 'mcp',
            waiting: 1,
            approved: 2,
            denied: 0,
            expired: 0,
            median_wait_seconds: 42,
          },
          {
            kind: 'network',
            waiting: 0,
            approved: 0,
            denied: 1,
            expired: 1,
            median_wait_seconds: null,
          },
          {
            kind: 'packages',
            waiting: 0,
            approved: 0,
            denied: 0,
            expired: 0,
            median_wait_seconds: null,
          },
          {
            kind: 'hold',
            waiting: 1,
            approved: 0,
            denied: 0,
            expired: 0,
            median_wait_seconds: null,
          },
        ],
        holds: [
          {
            rule: 'detect.same-url',
            count: 1,
            sessions: ['s-1'],
            oldest_at: '2026-10-25T11:00:00Z',
          },
        ],
        drift: [{ server: 'web', at: '2026-10-25T10:00:00Z' }],
        inspection: [{ server: 'pa\u202Epers', alerts: 3, blocks: 1, uninspected: 2 }],
        inspection_enabled: true,
        inspection_dropped: 4,
      },
      environments: [
        {
          environment_id: 'env1',
          kind: 'session',
          profile: 'minimal',
          state: 'ready',
          declared_tier: 3,
          tier: 3,
          job_id: null,
        },
        {
          environment_id: 'job1',
          kind: 'job',
          profile: 'minimal',
          state: 'stopped',
          declared_tier: 3,
          tier: null,
          job_id: 'exe-1',
        },
      ],
    },
    worker_unavailable: null,
    spend: {
      cost: 0.625,
      tokens: 29,
      calls: 3,
      unpriced: 1,
      by_role: [
        { role: 'main', model: null, cost: 0.125, tokens: 17, calls: 2, unpriced: 1 },
        { role: 'watcher', model: null, cost: 0.5, tokens: 12, calls: 1, unpriced: 0 },
      ],
      by_model: [
        { role: null, model: 'small', cost: 0.5, tokens: 12, calls: 1, unpriced: 0 },
        { role: null, model: null, cost: null, tokens: 5, calls: 1, unpriced: 1 },
      ],
    },
    model_cost: COST,
    canaries: [
      {
        server: 'papers',
        canary: { at: '2026-10-25T11:48:00Z', status: 'listed', tool_count: 12, elapsed_ms: 340 },
      },
      { server: 'web', canary: null },
    ],
    network_drops: "Network-policy drops are not available: the sandboxes' logs carry none yet.",
  };
}

function serve(answer: (window: string) => ProjectMonitor): string[] {
  const asked: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      asked.push(url);
      const window = new URL(url, 'http://workspace.test').searchParams.get('window') ?? '';
      return new Response(JSON.stringify(answer(window)));
    }),
  );
  return asked;
}

function show(): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(MonitorPage, { target, props: { projectId: 'p1' } }));
  return target;
}

const texts = (target: HTMLElement, selector: string) =>
  [...target.querySelectorAll(selector)].map((item) => item.textContent?.replace(/\s+/g, ' '));

it('shows each section from the figures', async () => {
  serve((window) => figures(window as '24h'));
  const target = show();
  await settle();
  expect(texts(target, 'h2')).toEqual([
    'Calls',
    'Approvals',
    'Holds and drift',
    'Inspected answers',
    'Spend and tokens',
    'Sandbox tiers',
    'Canaries',
  ]);
  const calls = texts(target, '.call-row');
  expect(calls[0]).toContain('papers');
  expect(calls[0]).toContain('read');
  expect(calls[1]).toContain('data.org');
  // Each row's bar is plain HTML: its four parts sized against the busiest row.
  const bar = target.querySelector<HTMLElement>('.call-row .bar')!;
  expect(bar.querySelector<HTMLElement>('.allowed')!.style.width).toBe('75%');
  expect(bar.querySelector<HTMLElement>('.held')!.style.width).toBe('12.5%');
  expect(texts(target, '.approval-row')[0]).toContain('42 s');
  expect(texts(target, '.holds li')[0]).toContain('detect.same-url: 1 open, sessions s-1');
  const drift = target.querySelector<HTMLAnchorElement>('.drift a')!;
  expect(drift.getAttribute('href')).toBe('#/projects/p1/mcp');
  expect(target.querySelector('a[href="#/projects/p1/mcp"]')!.textContent).toBe('Grants');
  // Alerts and blocks by server; a server's name shows its hidden characters.
  expect(texts(target, '.inspection-row')).toEqual(['pa⟦U+202E⟧pers312']);
  expect(target.querySelector('.inspection-state')!.textContent).toContain('inspection: on');
  expect(target.querySelector('.inspection-dropped')!.textContent).toContain('4 plain answers');
  expect(texts(target, '.model-row')[1]).toContain('unknown');
  expect(texts(target, '.role-row')[0]).toContain('The main agent');
  expect(target.querySelector('.project-cost')!.textContent).toContain('$10.63 in all');
  expect(texts(target, '.tier-row')[1]).toContain('not measured');
  const canaries = texts(target, '.canaries li');
  expect(canaries[0]).toContain('listed 12 minutes ago');
  expect(canaries[1]).toContain('none recorded');
  expect(target.textContent).toContain('Network-policy drops are not available');
  // A drifted server's link opens its Classification tab on the Connectors page.
  drift.addEventListener('click', (event) => event.preventDefault());
  drift.click();
  expect(browserState.load('mcp:p1').draft).toMatchObject({
    selected: 'web',
    tab: 'classification',
  });
});

it('says which parts are unavailable', async () => {
  serve((window) => ({
    ...figures(window as '24h'),
    worker: null,
    worker_unavailable: 'the worker cannot be reached',
    model_cost: { ...COST, last_day: null, total: null },
  }));
  const target = show();
  await settle();
  expect(target.querySelector('.unavailable')!.textContent).toContain(
    'Calls, approvals, holds, drift and sandbox tiers are unavailable: the worker cannot be reached.',
  );
  expect(target.querySelectorAll('.call-row')).toHaveLength(0);
  expect(target.textContent).toContain("The project's cost: unknown");
  expect(texts(target, '.model-row')[1]).toContain('unknown');
  // The event store alone can be out of reach: the tiers stay.
  serve((window) => {
    const found = figures(window as '24h');
    return {
      ...found,
      worker: { ...found.worker!, records: null, unavailable: 'the event store cannot be read' },
    };
  });
  unmount(mounted.pop()!);
  document.body.innerHTML = '';
  const again = show();
  await settle();
  expect(again.querySelector('.unavailable')!.textContent).toContain(
    'Calls, approvals, holds and drift are unavailable: the event store cannot be read.',
  );
  expect(again.querySelectorAll('.tier-row')).toHaveLength(2);
});

it('reads the other window when the switch changes', async () => {
  const asked = serve((window) => figures(window as '24h'));
  const target = show();
  await settle();
  expect(asked).toEqual(['/api/v0/projects/p1/monitor?window=24h']);
  const week = [...target.querySelectorAll<HTMLButtonElement>('.spans button')].find(
    (button) => button.textContent === 'Last 7 days',
  )!;
  expect(week.getAttribute('aria-pressed')).toBe('false');
  week.click();
  await settle();
  expect(asked.at(-1)).toBe('/api/v0/projects/p1/monitor?window=7d');
  expect(week.getAttribute('aria-pressed')).toBe('true');
});
