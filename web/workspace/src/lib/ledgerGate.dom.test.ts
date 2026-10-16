// SPDX-License-Identifier: MIT
// The gate and decisions on the entry view, mounted: read with the entry, read again after
// the user's change to it, and a failed read beside an entry still shown.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerEntryView from '../views/LedgerEntryView.svelte';
import type { LedgerChangeAnswer } from './api';
import type { EntryView, GateView } from './api-types';

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

function show<P extends Record<string, any>>(component: Component<P>, props: P): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const entry = (fields: Partial<EntryView> = {}): EntryView => ({
  entry_id: 'entry-0123456789abcdef01234567',
  pins: [],
  handle: 'C-1',
  type: 'claim',
  kind: null,
  session: null,
  standing: null,
  basis: null,
  anchors: [],
  origin: 'project',
  revision: 3,
  head: 3,
  title: 'Fermat for 17',
  status: 'proposed',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 1,
  statement: 'S',
  fields: {},
  links: [],
  evidence: [],
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [
    {
      flag_id: 'flag-1',
      cause: 'spread',
      source: { handle: 'A-1' },
      via: null,
      revision: 3,
      raised: '2026-10-15T00:00:00Z',
      reviews: [],
      reopens: null,
    },
  ],
  author: { kind: 'user', user: 'owner' },
  reason: 'r',
  ...fields,
});

const gate = (flagsHold: boolean): GateView => ({
  entry_id: 'entry-0123456789abcdef01234567',
  handle: 'C-1',
  revision: 3,
  status: 'proposed',
  applies: true,
  holds: false,
  clauses: [
    { clause: 'evidence', holds: true, reason: 'a supporting proof link' },
    { clause: 'review', holds: false, reason: 'no completed review held r3 whole' },
    { clause: 'flags', holds: flagsHold, reason: flagsHold ? 'no open flag' : '1 open flag' },
  ],
  decisions: [
    {
      entry_id: 'entry-d',
      handle: 'D-2',
      title: 'Establish C-1 once reviewed',
      status: 'proposed',
      revision: 1,
      author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
    },
  ],
});

const answer: LedgerChangeAnswer = {
  operation_id: 'op',
  replayed: false,
  entries: [],
  cleared_total: 1,
  flags_cleared: ['flag-1'],
  flags_reopened: [],
};

function server(state: { entry: EntryView; gate: GateView | null }) {
  const calls: { method: string; url: string }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      calls.push({ method, url });
      const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
      if (method === 'POST') {
        state.entry = entry({ open_flags: 0, flags: [] });
        state.gate = gate(true);
        return reply(answer);
      }
      // The work records, read with a claim: none here.
      if (url.includes('/work?'))
        return reply({
          project_id: 'p1',
          observed_at: 'now',
          work: { entry_id: 'e', handle: 'X-1', records: [], next_before: null },
        });
      if (url.endsWith('/gate')) {
        return state.gate === null
          ? reply({ error: { kind: 'unavailable', message: 'the gate cannot be read now' } }, 503)
          : reply({ project_id: 'p1', observed_at: 'now', gate: state.gate });
      }
      return reply({ project_id: 'p1', observed_at: 'now', entry: state.entry });
    }),
  );
  return calls;
}

describe('the gate on the entry view', () => {
  it('is read with the entry: each clause, and the decisions that affect it', async () => {
    const calls = server({ entry: entry(), gate: gate(false) });
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'C-1', revision: null });
    await settle();
    expect(
      calls.some((c) => c.url === '/api/v0/projects/p1/ledger/entry-0123456789abcdef01234567/gate'),
    ).toBe(true);
    const text = root.textContent ?? '';
    expect(text).toContain('1 of 3 clauses hold');
    expect(text).toContain('Not yet: A completed review that held the entry whole.');
    expect(text).toContain('no completed review held r3 whole');
    expect(text).toContain('Other decisions that affect it');
    const link = [...root.querySelectorAll('a')].find((a) => a.textContent === 'D-2');
    expect(link?.getAttribute('href')).toBe('#/projects/p1/ledger/D-2');
  });

  it('is read again after the user changes the entry', async () => {
    const calls = server({ entry: entry(), gate: gate(false) });
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'C-1', revision: null });
    await settle();
    expect(root.textContent).toContain('Not yet: No open flag.');
    const reason = root.querySelector('label.reason textarea') as HTMLTextAreaElement;
    reason.value = 'Still holds.';
    reason.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    [...root.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('Clear this flag'))!
      .click();
    await settle();
    expect(calls.filter((c) => c.url.endsWith('/gate'))).toHaveLength(2);
    expect(root.textContent).toContain('Holds: No open flag.');
  });

  it('says when it cannot be read, and the entry still shows', async () => {
    server({ entry: entry(), gate: null });
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'C-1', revision: null });
    await settle();
    expect(root.textContent).toContain('C-1: Fermat for 17');
    expect(root.textContent).toContain('the gate cannot be read now');
  });
});
