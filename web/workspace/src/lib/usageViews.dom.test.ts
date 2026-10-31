// SPDX-License-Identifier: MIT
// The usage view's two surfaces (D102): a session's Usage tab (spend by agent and model
// against the soft caps, check-ins and loop detections) and the project's Usage page (its
// sessions in a window). Both read only and say when a figure is unknown.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ProjectUsage from '../views/ProjectUsage.svelte';
import SessionUsage from '../views/SessionUsage.svelte';
import type { ProjectUsageView, ResourceRequestPage, SessionUsageView } from './api-types';
import { browserState } from './browserState';
import { agent, checkIn, CHILD, counts, LOOP, projectView, sessionView } from './usageTesting';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  sessionStorage.clear();
  // The kept choices live in memory too; put the defaults back.
  browserState.save('usage-page:p1', { span: '24h' });
  browserState.save('usage:p1:a', { order: 'tree' });
  vi.unstubAllGlobals();
});

const settle = async () => {
  for (let i = 0; i < 6; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
};
const texts = (target: HTMLElement, selector: string) =>
  [...target.querySelectorAll(selector)].map((item) =>
    item.textContent?.replace(/\s+/g, ' ').trim(),
  );

/** Each table row as its cells, joined by single spaces. */
const rows = (target: ParentNode, selector: string) =>
  [...target.querySelectorAll(selector)].map((row) =>
    [...row.querySelectorAll('td')]
      .map((cell) => cell.textContent?.replace(/\s+/g, ' ').trim())
      .join(' '),
  );

function serve(answer: (url: URL) => unknown): string[] {
  const asked: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      asked.push(url);
      return new Response(JSON.stringify(answer(new URL(url, 'http://workspace.test'))));
    }),
  );
  return asked;
}

function showSession(active = true): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(SessionUsage, { target, props: { projectId: 'p1', sessionId: 'a', active } }));
  return target;
}

function showProject(): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ProjectUsage, { target, props: { projectId: 'p1' } }));
  return target;
}

it('shows the session by agent against its soft caps, by model, and its check-ins', async () => {
  const asked = serve(() => sessionView());
  const target = showSession();
  await settle();
  expect(asked).toEqual(['/api/v0/projects/p1/sessions/a/usage/view']);
  expect(target.querySelector('.session-cost')!.textContent).toBe('$2.25 (+1 unpriced)');
  expect(texts(target, '.role-row')[2]).toContain('The reviewer: $0.25');
  // The root first, then the child by its task; the reviewer's calls are its own line.
  expect(texts(target, '.agent-title')).toEqual(['Research agent', 'Verify finite-field claims']);
  expect(rows(target.querySelectorAll('.agent')[1]!, '.line')).toEqual([
    'Delegated runs small $0.50 550 1',
    'The reviewer judge $0.25 550 1',
  ]);
  // Each cap with its use; a cap at its total is marked, a missing one says so.
  const caps = target.querySelectorAll('.agent')[1]!.querySelectorAll('.cap');
  expect([...caps].map((cap) => cap.getAttribute('data-state'))).toEqual([
    'near',
    'reached',
    'ok',
    'off',
  ]);
  expect(caps[3]!.textContent).toContain('$0.75, no soft cap recorded');
  expect(target.querySelector('.strain')!.textContent).toContain('1 agent is at a soft cap');
  expect(rows(target, '.model-row')).toEqual([
    'big $1.50 (+1 unpriced) 2,200 2',
    'small $0.50 550 1',
    'judge $0.25 550 1',
  ]);
  // The check-ins: the loop detection with its heuristic, detail and how it was answered.
  expect(target.querySelector('.check-in-summary')!.textContent).toBe(
    '2 check-ins, 1 from a loop detection, 1 awaiting a decision',
  );
  expect(target.querySelector('.answered')!.textContent).toContain('1 by the parent agent');
  expect(texts(target, '.loops li')[0]).toContain('1 × the same tool call');
  const items = texts(target, '.check-in');
  expect(items[0]).toContain('Loop detection: the same tool call');
  expect(items[0]).toContain('8 calls of shell with the same arguments');
  expect(items[0]).toContain('The parent agent redirected it: Try a different approach');
  expect(items[1]).toContain('Reached its soft tokens cap');
  expect(items[1]).toContain('Awaiting a decision');
  expect(target.textContent).toContain('Project notice at $1: crossed');
  expect(target.querySelector('.limit')).toBeNull();
  // Reading changes nothing: every request was a GET.
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => (init?.method ?? 'GET') === 'GET')).toBe(
    true,
  );
});

it('orders the agents by cost and remembers the order in the tab', async () => {
  serve(() =>
    sessionView({ agents: [agent({ key: 'cheap', cost: 0.1 }), agent({ key: 'dear', cost: 9 })] }),
  );
  const target = showSession();
  await settle();
  const order = (label: string) =>
    [...target.querySelectorAll<HTMLButtonElement>('.orders button')].find(
      (button) => button.textContent === label,
    )!;
  expect(order('As they began').getAttribute('aria-pressed')).toBe('true');
  order('Costliest first').click();
  await settle();
  expect(texts(target, '.agent-title')).toHaveLength(2);
  expect(target.querySelectorAll('.agent')[0]!.textContent).toContain('$9.00');
  expect(browserState.load('usage:p1:a').draft).toMatchObject({ order: 'cost' });
});

it('says what is unknown, and what is not there, instead of showing zeros', async () => {
  serve(() =>
    sessionView({
      total: { ...sessionView().total, cost: null, unpriced: 2, calls: 2, by_role: [] },
      agents: [agent({ cost: null, unpriced: 2, caps: [], lines: [] })],
      by_model: [{ model: null, cost: null, tokens: 5, calls: 2, unpriced: 2 }],
      check_ins: { requests: [], next_after: null },
      counts: counts(),
      notices: [],
      daily_cost_limit: 10,
    }),
  );
  const target = showSession();
  await settle();
  expect(target.querySelector('.session-cost')!.textContent).toBe('unknown (2 unpriced)');
  expect(rows(target, '.model-row')[0]).toBe('unknown unknown (2 unpriced) 5 2');
  expect(target.querySelector('.check-in-summary')!.textContent).toBe('No check-ins recorded.');
  expect(target.querySelector('.limit')!.textContent).toContain('local daily cost limit of $10.00');
  expect(target.querySelectorAll('.cap')).toHaveLength(0);
});

it('shows an empty session, and agent text with its hidden characters', async () => {
  serve(() =>
    sessionView({
      total: { ...sessionView().total, cost: null, tokens: 0, calls: 0, unpriced: 0, by_role: [] },
      agents: [],
      by_model: [],
      check_ins: { requests: [], next_after: null },
      counts: counts(),
      notices: [],
    }),
  );
  const empty = showSession();
  await settle();
  expect(empty.textContent).toContain('No model calls recorded in this session yet.');
  expect(empty.textContent).toContain('No agent has used a model in this session yet.');
  unmount(mounted.pop()!);
  document.body.innerHTML = '';
  serve(() =>
    sessionView({
      agents: [agent({ task: 'run‮ it' })],
      check_ins: {
        requests: [
          checkIn({
            task: 'brief​',
            summary: { ...LOOP.summary!, detail: 'a‮b' },
            decision: { ...LOOP.decision!, request: { ...LOOP.decision!.request, reason: 'x​y' } },
          }),
        ],
        next_after: null,
      },
    }),
  );
  const shown = showSession();
  await settle();
  expect(shown.querySelector('.agent-title')!.textContent).toBe('run⟦U+202E⟧ it');
  const item = shown.querySelector('.check-in')!.textContent!;
  expect(item).toContain('brief⟦U+200B⟧');
  expect(item).toContain('a⟦U+202E⟧b');
  expect(item).toContain('x⟦U+200B⟧y');
});

it('pages older check-ins through the check-in route, each once', async () => {
  const older: ResourceRequestPage = {
    requests: [
      checkIn({ request_id: 'resource-0', resource: 'turns', used: 300, total: 300 }),
      LOOP,
    ],
    next_after: null,
  };
  const asked = serve((url) =>
    url.pathname.endsWith('/recovery/resources')
      ? older
      : sessionView({ check_ins: { requests: [LOOP], next_after: '1793|resource-2' } }),
  );
  const target = showSession();
  await settle();
  expect(target.querySelectorAll('.check-in')).toHaveLength(1);
  const button = [...target.querySelectorAll('button')].find(
    (item) => item.textContent === 'Older check-ins',
  )!;
  button.click();
  await settle();
  expect(asked.some((url) => url.includes('/recovery/resources?after=1793%7Cresource-2'))).toBe(
    true,
  );
  expect(target.querySelectorAll('.check-in')).toHaveLength(2); // the loop check-in once
  expect(texts(target, '.check-in')[1]).toContain('Reached its soft turns cap');
  expect(
    [...target.querySelectorAll('button')].some((item) => item.textContent === 'Older check-ins'),
  ).toBe(false);
});

it('reads only while its tab is shown', async () => {
  const asked = serve(() => sessionView());
  showSession(false);
  await settle();
  expect(asked).toEqual([]);
});

it('says the figures are unavailable when the read fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{}', { status: 503 })),
  );
  const target = showSession();
  await settle();
  expect(target.textContent).toContain('The usage figures are unavailable');
});

function window(url: URL): '24h' | '7d' | 'all' {
  return (url.searchParams.get('window') ?? '24h') as '24h' | '7d' | 'all';
}

it('shows the project by session for a window and reads the other when switched', async () => {
  const asked = serve((url) => projectView(window(url)));
  const target = showProject();
  await settle();
  expect(asked).toEqual(['/api/v0/projects/p1/usage/view?window=24h']);
  expect(target.querySelector('.window-total')!.textContent).toContain('Last 24 hours: $2.25');
  expect(target.querySelector('.all-time')!.textContent).toContain('$9.50');
  expect(rows(target, '.session-row')).toEqual([
    'a $2.00 (+1 unpriced) 3,000 3 2 1 1',
    'b $0.25 300 1 0 0 0',
  ]);
  // A session leads to its Usage tab.
  expect(target.querySelector<HTMLAnchorElement>('.session-row a')!.getAttribute('href')).toBe(
    '#/projects/p1/sessions/a/usage',
  );
  expect(rows(target, '.model-row')[1]).toContain('unknown');
  expect(target.querySelector('.check-in-summary')!.textContent).toBe(
    '2 check-ins, 2 from a loop detection, 1 awaiting a decision',
  );
  expect(texts(target, '.loops li')).toHaveLength(2);
  expect(target.querySelector('a[href="#/projects/p1/monitor"]')).not.toBeNull();
  const week = [...target.querySelectorAll('.spans button')].find(
    (item) => item.textContent === 'Last 7 days',
  ) as HTMLButtonElement;
  week.click();
  await settle();
  expect(asked.at(-1)).toBe('/api/v0/projects/p1/usage/view?window=7d');
  expect(browserState.load('usage-page:p1').draft).toMatchObject({ span: '7d' });
  const all = [...target.querySelectorAll('.spans button')].find(
    (item) => item.textContent === 'All time',
  ) as HTMLButtonElement;
  all.click();
  await settle();
  expect(asked.at(-1)).toBe('/api/v0/projects/p1/usage/view?window=all');
  expect(target.querySelector('.all-time')).toBeNull(); // the window is all of it
});

it('shows the local limit, the notices, and calls outside any session', async () => {
  serve((url) =>
    projectView(window(url), {
      daily_cost_limit: 5,
      last_day_cost: 2.25,
      notices: [
        { threshold: 1, crossed_at: '2026-10-31T11:00:00Z', spent: 1.5 },
        { threshold: 50, crossed_at: null, spent: null },
      ],
      unassigned: {
        session: null,
        run: null,
        cost: 0.5,
        tokens: 10,
        calls: 2,
        unpriced: 0,
        by_role: [],
      },
      more_sessions: 3,
    }),
  );
  const target = showProject();
  await settle();
  expect(target.querySelector('.limit')!.textContent).toContain('$2.75 left');
  const notices = texts(target, '.notice');
  expect(notices[0]).toContain('Notice at $1: crossed');
  expect(notices[0]).toContain('with $1.50 recorded');
  expect(notices[1]).toContain('Notice at $50: not crossed yet. A notice never stops anything.');
  expect(texts(target, '.unassigned')[0]).toContain('no session is recorded for');
  expect(target.textContent).toContain('3 more sessions are not listed');
});

it('says so when a window has no use', async () => {
  serve((url) =>
    projectView(window(url), {
      total: {
        session: null,
        run: null,
        cost: null,
        tokens: 0,
        calls: 0,
        unpriced: 0,
        by_role: [],
      },
      sessions: [],
      by_model: [],
      counts: counts(),
    }),
  );
  const target = showProject();
  await settle();
  expect(target.textContent).toContain('No model calls recorded in this window.');
  expect(target.textContent).toContain('No session used a model in this window.');
  expect(target.querySelector('.window-total')!.textContent).toContain('unknown');
  expect(CHILD).toBeTruthy();
});

it('keeps the chosen window across a remount', async () => {
  const asked = serve((url) => projectView(window(url)));
  browserState.save('usage-page:p1', { span: 'all' });
  showProject();
  await settle();
  expect(asked[0]).toBe('/api/v0/projects/p1/usage/view?window=all');
});

export type { ProjectUsageView, SessionUsageView };
