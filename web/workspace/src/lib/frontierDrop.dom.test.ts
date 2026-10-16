// SPDX-License-Identifier: MIT
// The user's drop of a dominates link, mounted on the frontier: the holder is read as it
// is now and the link's reason shown exactly, every hidden character as its code point
// (hidden.fixtures.json); the drop names the revision read and the user's reason; a
// conflict drops nothing and says so; and the frontier is read again after a drop.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FrontierView from '../views/FrontierView.svelte';
import type { LedgerChangeAnswer } from './api';
import type { DominatesLink, EntryView, FrontierAnswer, FrontierResult } from './api-types';
import fixtures from './hidden.fixtures.json';

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
  reason: 'a tighter bound',
  author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
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
  confirmed_by: null,
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

function frontier(linked: boolean): FrontierAnswer {
  const target = result('R-5', linked ? { dominated_by: ['R-3'], linked_by: [link] } : {});
  return {
    project_id: 'p1',
    observed_at: 'now',
    next_groups_after: null,
    next_groups_key: null,
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
          key: JSON.stringify({ attack: 'dual' }),
          best: linked ? [result('R-3')] : [result('R-3'), target],
          dominated: linked ? [target] : [],
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
}

const holder = (note: string | null, revision = 4): EntryView => ({
  entry_id: HOLDER,
  handle: 'R-3',
  type: 'result',
  kind: null,
  session: null,
  standing: null,
  basis: null,
  anchors: [],
  origin: 'project',
  revision,
  head: revision,
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
      note,
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
});

const answer: LedgerChangeAnswer = {
  operation_id: 'op',
  replayed: false,
  entries: [{ entry_id: HOLDER, handle: 'R-3', revision: 5, status: 'established' }],
  cleared_total: 0,
  flags_cleared: [],
  flags_reopened: [],
};

interface Call {
  method: string;
  url: string;
  body: Record<string, unknown> | null;
}

/** The frontier, the holder as it reads now, and the drop's answer (a status for a failure). */
function server(state: { holder: EntryView; drop: number }): Call[] {
  const calls: Call[] = [];
  let dropped = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
      calls.push({ method, url, body });
      const reply = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), { status });
      if (method === 'POST') {
        if (state.drop !== 200) {
          return reply({ error: { kind: 'conflict', message: 'a newer revision' } }, state.drop);
        }
        dropped = true;
        return reply(answer);
      }
      if (url.includes('/ledger/'))
        return reply({ project_id: 'p1', observed_at: 'now', entry: state.holder });
      return reply(frontier(!dropped));
    }),
  );
  return calls;
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

async function open(root: HTMLElement): Promise<void> {
  await settle();
  button(root, 'Drop this link…')!.click();
  await settle();
}

function write(root: HTMLElement, reason: string): void {
  const box = root.querySelector('textarea')!;
  box.value = reason;
  box.dispatchEvent(new Event('input'));
  flushSync();
}

describe("the user's drop of a dominates link", () => {
  it('shows the reason exactly, drops at the revision read, and reads the frontier again', async () => {
    const calls = server({ holder: holder('a tighter ‮bound'), drop: 200 });
    const root = show();
    await open(root);
    expect(calls.at(-1)!.url).toBe(`/api/v0/projects/p1/ledger/${HOLDER}`);
    expect(root.querySelector('pre.exact')!.textContent).toBe('a tighter ⟦U+202E⟧bound');
    write(root, 'the two are comparable now');
    button(root, 'Drop the link at r4')!.click();
    await settle();
    const post = calls.find((call) => call.method === 'POST')!;
    expect(post.url).toBe(`/api/v0/projects/p1/ledger/${HOLDER}/dominates/dropped`);
    expect(post.body).toMatchObject({
      target: TARGET,
      revision: 4,
      reason: 'the two are comparable now',
    });
    expect(typeof post.body!.operation_id).toBe('string');
    expect(calls.filter((call) => call.url.includes('/frontier/'))).toHaveLength(2);
    expect(root.querySelector('[role="status"]')!.textContent).toBe(
      "Your drop of R-3's link to R-5, at r4, is recorded.",
    );
    expect(button(root, 'Drop this link…')).toBeUndefined();
  });

  it('asks for a reason before it sends anything', async () => {
    const calls = server({ holder: holder('why'), drop: 200 });
    const root = show();
    await open(root);
    button(root, 'Drop the link at r4')!.click();
    await settle();
    expect(root.textContent).toContain('Give the reason: why the link should not count.');
    expect(calls.some((call) => call.method === 'POST')).toBe(false);
  });

  it('drops nothing on a conflict, and says so', async () => {
    const calls = server({ holder: holder('why'), drop: 409 });
    const root = show();
    await open(root);
    write(root, 'stale');
    button(root, 'Drop the link at r4')!.click();
    await settle();
    expect(root.textContent).toContain(
      'R-3 has a newer revision than the r4 you read, so nothing was dropped.',
    );
    expect(calls.filter((call) => call.url.includes('/frontier/'))).toHaveLength(1);
    expect(root.querySelector('[role="status"]')).toBeNull();
  });

  it('offers no drop when the holder no longer holds the link', async () => {
    const moved = { ...holder('why'), links: [] };
    server({ holder: moved, drop: 200 });
    const root = show();
    await open(root);
    expect(root.textContent).toContain('R-3 r4 no longer holds a dominates link to R-5.');
    expect(button(root, 'Drop the link at r4')).toBeUndefined();
    button(root, 'Close')!.click();
    flushSync();
    expect(button(root, 'Drop this link…')).toBeDefined();
  });

  const cases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(cases)(
    "$name, in the link's reason: each hidden character as its code point",
    async ({ text, hidden }) => {
      server({ holder: holder(text), drop: 200 });
      const root = show();
      await open(root);
      const exact = root.querySelector('pre.exact')!.textContent!;
      for (const [, code] of hidden) expect(exact).toContain(`⟦${code}⟧`);
      if (hidden.length === 0) expect(exact).toBe(text);
    },
  );
});
