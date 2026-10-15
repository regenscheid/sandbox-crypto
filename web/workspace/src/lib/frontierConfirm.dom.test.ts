// SPDX-License-Identifier: MIT
// The user's confirmation of an unverified result, mounted on the frontier: the result is
// read as it is now and shown as exact text, every hidden character as its code point
// (hidden.fixtures.json); the check names the revision read; a conflict records nothing
// and says so; and the frontier is read again after a check.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FrontierView from '../views/FrontierView.svelte';
import type { LedgerChangeAnswer } from './api';
import type { EntryView, FrontierAnswer, FrontierResult } from './api-types';
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

const ID = 'entry-0123456789abcdef0123456c';
const AGENT = { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'analyst' };

const result = (unverified: boolean): FrontierResult => ({
  entry_id: ID,
  handle: 'R-8',
  revision: 3,
  title: 'A published bound',
  statement: 'S',
  status: 'recorded',
  origin: 'published',
  disputed: false,
  unverified,
  author: AGENT,
  conditions: { attack: 'dual' },
  measures: { time: 140 },
  evidence_types: ['source_claim'],
  sources: ['REF-1'],
  artifacts: [],
  open_findings: 0,
  open_flags: 0,
  dominated_by: [],
  linked_by: [],
  would_dominate: [],
});

function frontier(confirmed: boolean): FrontierAnswer {
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
          best: confirmed ? [result(false)] : [],
          dominated: [],
          not_compared: [],
          candidates: confirmed ? [] : [result(true)],
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

const entry = (fields: Partial<EntryView> = {}): EntryView => ({
  entry_id: ID,
  handle: 'R-8',
  type: 'result',
  origin: 'published',
  revision: 3,
  head: 3,
  title: 'A published bound',
  status: 'recorded',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 0,
  statement: 'The cost is $2^{140}$.',
  fields: { category: 'key-recovery', conditions: { attack: 'dual' }, measures: { time: 140 } },
  links: [],
  evidence: [],
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [],
  author: AGENT,
  reason: 'imported',
  ...fields,
});

const answer: LedgerChangeAnswer = {
  operation_id: 'op',
  replayed: false,
  entries: [{ entry_id: ID, handle: 'R-8', revision: 4, status: 'recorded' }],
  cleared_total: 0,
  flags_cleared: [],
  flags_reopened: [],
};

interface Call {
  method: string;
  url: string;
  body: Record<string, unknown> | null;
}

/** The frontier, the result as it reads now, and the check's answer (a status for a failure). */
function server(state: { entry: EntryView; check: number }): Call[] {
  const calls: Call[] = [];
  let checked = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
      calls.push({ method, url, body });
      const reply = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), { status });
      if (method === 'POST') {
        if (state.check !== 200) {
          return reply({ error: { kind: 'conflict', message: 'a newer revision' } }, state.check);
        }
        checked = true;
        return reply(answer);
      }
      if (url.includes('/ledger/')) {
        return reply({ project_id: 'p1', observed_at: 'now', entry: state.entry });
      }
      return reply(frontier(checked));
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
  button(root, 'Confirm…')!.click();
  await settle();
}

const exact = (root: HTMLElement) =>
  [...root.querySelectorAll('pre.exact')].map((pre) => pre.textContent);

describe("the user's confirmation of an unverified result", () => {
  it('shows the result exactly, checks the revision read, and reads the frontier again', async () => {
    const calls = server({ entry: entry({ title: 'A ‮bound' }), check: 200 });
    const root = show();
    await settle();
    expect(root.textContent).toContain(
      'Unverified: published by analyst agent run run-1 of session s1, role analyst',
    );
    await open(root);
    expect(calls.at(-1)!.url).toBe(`/api/v0/projects/p1/ledger/${ID}`);
    expect(exact(root)[0]).toBe('A ⟦U+202E⟧bound');
    expect(exact(root)[1]).toBe('The cost is $2^{140}$.');
    const box = root.querySelector('textarea')!;
    box.value = 'read the source';
    box.dispatchEvent(new Event('input'));
    flushSync();
    button(root, 'Record my check of r3')!.click();
    await settle();
    const post = calls.find((call) => call.method === 'POST')!;
    expect(post.url).toBe(`/api/v0/projects/p1/ledger/${ID}/checks`);
    expect(post.body).toMatchObject({ revision: 3, note: 'read the source' });
    expect(typeof post.body!.operation_id).toBe('string');
    expect(calls.filter((call) => call.url.includes('/frontier/'))).toHaveLength(2);
    expect(root.querySelector('[role="status"]')!.textContent).toBe(
      'Your check of R-8 r3 is recorded.',
    );
    expect(root.textContent).not.toContain('Unverified');
    expect(button(root, 'Confirm…')).toBeUndefined();
  });

  it('records nothing on a conflict, and says so', async () => {
    const calls = server({ entry: entry(), check: 409 });
    const root = show();
    await open(root);
    button(root, 'Record my check of r3')!.click();
    await settle();
    expect(root.textContent!.replace(/\s+/g, ' ')).toContain(
      'R-8 has a newer revision than the r3 you read, so nothing was recorded. Cancel, then ' +
        'confirm it again to read it as it is now.',
    );
    expect(calls.filter((call) => call.url.includes('/frontier/'))).toHaveLength(1);
    expect(root.querySelector('[role="status"]')).toBeNull();
    // No check of the text read is offered again: the user cancels and reads it anew.
    expect(button(root, 'Record my check of r3')!.disabled).toBe(true);
    button(root, 'Cancel')!.click();
    flushSync();
    button(root, 'Confirm…')!.click();
    await settle();
    expect(button(root, 'Record my check of r3')!.disabled).toBe(false);
  });

  it('shows its fields, evidence and assumptions exactly, as the check pins them', async () => {
    server({
      entry: entry({
        fields: { category: 'key-recovery', measures: { time: 140 } },
        evidence: [
          {
            evidence_id: 'ev-1',
            role: 'supports',
            type: 'source_claim',
            target: 'entry',
            target_id: 'entry-0123456789abcdef01234599',
            revision: 2,
            block: null,
            span: null,
            sha256: null,
            checked: null,
            scope: 'Table 3, \u202Erow 2',
            added: 1,
            handle: 'REF-1',
          },
        ],
        links: [
          {
            kind: 'assumes',
            target: 'entry',
            target_id: 'entry-0123456789abcdef01234598',
            revision: 1,
            block: null,
            note: 'the core-SVP model',
            handle: 'A-2',
            session: null,
          },
        ],
      }),
      check: 200,
    });
    const root = show();
    await open(root);
    const shown = exact(root);
    expect(shown).toContain(
      JSON.stringify({ category: 'key-recovery', measures: { time: 140 } }, null, 2),
    );
    expect(shown).toContain(
      '1. Supports, source claim: entry REF-1 (entry-0123456789abcdef01234599) r2\n' +
        'Scope: Table 3, ⟦U+202E⟧row 2',
    );
    expect(shown).toContain('Assumes A-2 r1: the core-SVP model');
  });

  it('says when the result has moved on from the revision the frontier shows, and checks the one read', async () => {
    const calls = server({ entry: entry({ revision: 5, head: 5 }), check: 409 });
    const root = show();
    await open(root);
    expect(root.textContent!.replace(/\s+/g, ' ')).toContain(
      'The frontier shows r3, and R-8 is now at r5: what you check is r5, below.',
    );
    button(root, 'Record my check of r5')!.click();
    await settle();
    expect(calls.find((call) => call.method === 'POST')!.body).toMatchObject({ revision: 5 });
    button(root, 'Cancel')!.click();
    flushSync();
    expect(button(root, 'Confirm…')).toBeDefined();
  });

  const cases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(cases)(
    '$name, in its statement: each hidden character as its code point',
    async ({ text, hidden }) => {
      server({ entry: entry({ statement: text }), check: 200 });
      const root = show();
      await open(root);
      const shown = exact(root)[1]!;
      for (const [, code] of hidden) expect(shown).toContain(`⟦${code}⟧`);
      if (hidden.length === 0) expect(shown).toBe(text);
    },
  );
});
