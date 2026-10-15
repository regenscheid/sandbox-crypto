// SPDX-License-Identifier: MIT
// A frontier's history and its best known at a time, mounted on the frontier view: the
// history read on request, older changes after it, and read again after the user's drop
// of a link; the past read at the time picked, with its zone.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FrontierView from '../views/FrontierView.svelte';
import type {
  DominatesLink,
  EntryView,
  FrontierAnswer,
  FrontierChange,
  FrontierHistoryAnswer,
  FrontierPastAnswer,
  FrontierResult,
} from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const HOLDER = 'entry-0123456789abcdef0123456a';
const TARGET = 'entry-0123456789abcdef0123456b';

const link: DominatesLink = {
  entry_id: HOLDER,
  handle: 'R-3',
  reason: 'tighter',
  author: { kind: 'user', user: 'owner' },
  revision: 2,
  target: 'R-5',
  void: null,
};

const result = (handle: string, fields: Partial<FrontierResult> = {}): FrontierResult => ({
  entry_id: handle === 'R-5' ? TARGET : HOLDER,
  handle,
  revision: 1,
  title: `Result ${handle}`,
  statement: 'S',
  status: 'established',
  origin: 'project',
  disputed: false,
  unverified: false,
  author: { kind: 'user', user: 'owner' },
  conditions: { attack: 'dual' },
  measures: { time: 160 },
  evidence_types: [],
  sources: [],
  artifacts: [],
  open_findings: 0,
  open_flags: 0,
  dominated_by: [],
  linked_by: [],
  would_dominate: [],
  ...fields,
});

const frontier: FrontierAnswer = {
  project_id: 'p1',
  observed_at: 'now',
  next_groups_after: null,
  frontier: {
    category: {
      category: 'key-recovery',
      revision: 1,
      head: 1,
      target: 'Kyber-768',
      kind: 'cost',
      measures: [{ name: 'time', unit: 'bits', direction: 'lower' }],
      conditions: ['attack'],
      author: { kind: 'user', user: 'owner' },
      reason: 'r',
      operation_id: 'op-1',
      created: '2026-10-15T00:00:00Z',
      replayed: false,
    },
    groups: [
      {
        conditions: { attack: 'dual' },
        best: [result('R-3')],
        dominated: [result('R-5', { dominated_by: ['R-3'], linked_by: [link] })],
        not_compared: [],
        candidates: [],
        more: {},
      },
    ],
    unplaced: [],
    truncated: false,
    next_candidates_before: null,
    lookalike: [],
    void_links: [],
    agent_revised: null,
    unplaced_more: 0,
  },
};

const change = (seq: number, added: string): FrontierChange => ({
  seq,
  conditions: { attack: 'dual' },
  best: [{ entry_id: HOLDER, handle: added, revision: 1 }],
  added: [added],
  removed: [],
  cause: { operation_id: `op-${seq}`, author: 'user:user', reason: `reason ${seq}`, entries: [] },
  created: '2026-10-15T00:00:00Z',
  more: {},
});

const history = (changes: FrontierChange[], next: number | null): FrontierHistoryAnswer => ({
  project_id: 'p1',
  observed_at: 'now',
  history: { category: 'key-recovery', changes, next_before: next },
});

const past: FrontierPastAnswer = {
  project_id: 'p1',
  observed_at: 'now',
  past: {
    category: 'key-recovery',
    at: '2026-10-01T00:00:00+00:00',
    groups: [
      {
        conditions: { attack: 'dual' },
        best: [{ entry_id: HOLDER, handle: 'R-3', revision: 1 }],
        more: 2,
      },
    ],
  },
};

const holder: EntryView = {
  entry_id: HOLDER,
  handle: 'R-3',
  type: 'result',
  origin: 'project',
  revision: 4,
  head: 4,
  title: 'Result R-3',
  status: 'established',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 0,
  statement: 'S',
  fields: {},
  links: [
    {
      kind: 'dominates',
      target: 'entry',
      target_id: TARGET,
      revision: null,
      block: null,
      note: 'tighter',
      handle: 'R-5',
      session: null,
    },
  ],
  evidence: [],
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [],
  author: { kind: 'user', user: 'owner' },
  reason: 'r',
};

/** Answers each read by its path and query, and records them. */
function server(): string[] {
  const reads: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const found = new URL(url, 'http://localhost');
      const read = `${init?.method ?? 'GET'} ${found.pathname.replace('/api/v0/projects/p1', '')}${found.search}`;
      reads.push(read);
      const reply = (value: unknown) => new Response(JSON.stringify(value));
      if (init?.method === 'POST') {
        return reply({
          operation_id: 'op',
          replayed: false,
          entries: [],
          cleared_total: 0,
          flags_cleared: [],
          flags_reopened: [],
        });
      }
      if (found.pathname.endsWith('/history')) {
        return reply(
          found.searchParams.get('before') === '5'
            ? history([change(4, 'R-1')], null)
            : history([change(6, 'R-3'), change(5, 'R-2')], 5),
        );
      }
      if (found.pathname.endsWith('/past')) return reply(past);
      if (found.pathname.includes('/ledger/')) {
        return reply({ project_id: 'p1', observed_at: 'now', entry: holder });
      }
      return reply(frontier);
    }),
  );
  return reads;
}

function show(): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(FrontierView, { target, props: { projectId: 'p1', category: 'key-recovery' } }),
  );
  flushSync();
  return target;
}

const button = (root: HTMLElement, text: string) =>
  [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
const reasons = (root: HTMLElement) =>
  [...root.querySelectorAll('.frontier-history p.muted')]
    .map((p) => p.textContent ?? '')
    .filter((text) => text.startsWith('Its reason'));

describe("a frontier's history and its past", () => {
  it('reads the history on request, newest first, and older changes after it', async () => {
    const reads = server();
    const root = show();
    await settle();
    expect(reads.some((read) => read.includes('/history'))).toBe(false);
    button(root, 'Show the history')!.click();
    await settle();
    expect(reasons(root)).toEqual(['Its reason: reason 6', 'Its reason: reason 5']);
    button(root, 'Older changes')!.click();
    await settle();
    expect(reads.filter((read) => read.includes('/history'))).toEqual([
      'GET /frontier/key-recovery/history?limit=20',
      'GET /frontier/key-recovery/history?limit=20&before=5',
    ]);
    expect(reasons(root)).toHaveLength(3);
    expect(button(root, 'Older changes')).toBeUndefined();
  });

  it("reads the history again after the user's drop of a link", async () => {
    const reads = server();
    const root = show();
    await settle();
    button(root, 'Show the history')!.click();
    await settle();
    button(root, 'Drop this link…')!.click();
    await settle();
    const box = root.querySelector('textarea')!;
    box.value = 'comparable now';
    box.dispatchEvent(new Event('input'));
    flushSync();
    button(root, 'Drop the link at r4')!.click();
    await settle();
    expect(reads.filter((read) => read.includes('/history'))).toHaveLength(2);
  });

  it('reads the best known at the time picked, with its zone', async () => {
    const reads = server();
    const root = show();
    await settle();
    button(root, 'Show')!.click();
    flushSync();
    expect(root.textContent).toContain('Pick a date and a time.');
    const input = root.querySelector<HTMLInputElement>('input[type="datetime-local"]')!;
    input.value = '2026-10-01T09:30';
    input.dispatchEvent(new Event('input'));
    flushSync();
    button(root, 'Show')!.click();
    await settle();
    const at = new Date('2026-10-01T09:30').toISOString().replace('Z', '+00:00');
    expect(reads.at(-1)).toBe(
      `GET /frontier/key-recovery/past?${new URLSearchParams({ at }).toString()}`,
    );
    expect(root.textContent).toContain('attack: dual: R-3 r1 and 2 more');
    const links = [...root.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    expect(links).toContain('#/projects/p1/ledger/R-3/revisions/1');
  });
});
