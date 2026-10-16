// SPDX-License-Identifier: MIT
// The flags on a ledger entry, mounted and used: what a clearing and a reopening send,
// the buttons while a change runs, and the history read again after a change.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerEntryView from '../views/LedgerEntryView.svelte';
import LedgerFlags from '../views/LedgerFlags.svelte';
import type { LedgerChangeAnswer } from './api';
import type { ClearedFlag, EntryFlag, EntryView } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

/** Let answers arrive and the views update. */
async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

function show<P extends Record<string, any>>(component: Component<P>, props: P) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const button = (root: HTMLElement, words: string) => {
  const found = [...root.querySelectorAll('button')].find((b) =>
    b.textContent?.trim().startsWith(words),
  );
  if (!found) throw new Error(`no button "${words}"`);
  return found as HTMLButtonElement;
};

function type(root: HTMLElement, label: string, text: string): void {
  const field = [...root.querySelectorAll('label')]
    .find((l) => l.textContent?.includes(label))
    ?.querySelector('textarea');
  if (!field) throw new Error(`no field "${label}"`);
  field.value = text;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

const flag = (id: string, fields: Partial<EntryFlag> = {}): EntryFlag => ({
  flag_id: id,
  cause: 'dependency_status',
  source: { entry: 'entry-0', handle: 'A-1', revision: 2, status: 'withdrawn' },
  via: null,
  revision: 3,
  raised: '2026-10-15T00:00:00Z',
  reviews: [],
  reopens: null,
  ...fields,
});

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
  title: 'T',
  status: 'proposed',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 3,
  statement: 'S',
  fields: {},
  links: [],
  evidence: [],
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [flag('flag-1'), flag('flag-2', { cause: 'spread' }), flag('flag-3')],
  author: { kind: 'user', user: 'owner' },
  reason: 'r',
  ...fields,
});

const answer = (fields: Partial<LedgerChangeAnswer> = {}): LedgerChangeAnswer => ({
  operation_id: 'op',
  replayed: false,
  entries: [],
  cleared_total: 0,
  flags_cleared: [],
  flags_reopened: [],
  ...fields,
});

/** A fetch that answers each POST with `reply` and records the bodies sent. */
function posts(reply: (body: Record<string, unknown>) => unknown) {
  const sent: { url: string; body: Record<string, unknown> }[] = [];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
    sent.push({ url, body });
    return new Response(JSON.stringify(reply(body)));
  });
  vi.stubGlobal('fetch', fetch);
  return sent;
}

describe('clearing flags', () => {
  it('names the one flag, the selected flags, or every listed flag by its ID', async () => {
    const sent = posts((body) => answer({ cleared_total: (body.flags as string[]).length }));
    const changed = vi.fn().mockResolvedValue(undefined);
    const root = show(LedgerFlags, { projectId: 'p1', entry: entry(), changed });

    type(root, 'Reason for clearing', 'Still holds.');
    [...root.querySelectorAll('li')][1]!.querySelector('button')!.click();
    await settle();
    expect(sent.at(-1)!.body).toEqual({
      flags: ['flag-2'],
      reason: 'Still holds.',
      operation_id: expect.any(String),
    });
    expect(root.textContent).toContain('Cleared 1 flag.');

    type(root, 'Reason for clearing', 'Not affected.');
    const boxes = [...root.querySelectorAll<HTMLInputElement>('input[type=checkbox]')];
    for (const box of [boxes[0]!, boxes[2]!]) {
      box.checked = true;
      box.dispatchEvent(new Event('change', { bubbles: true }));
    }
    flushSync();
    button(root, 'Clear the 2 selected').click();
    await settle();
    expect(sent.at(-1)!.body.flags).toEqual(['flag-1', 'flag-3']);

    type(root, 'Reason for clearing', 'All checked.');
    button(root, 'Clear all 3 open flags').click();
    await settle();
    expect(sent.at(-1)!.body.flags).toEqual(['flag-1', 'flag-2', 'flag-3']);
    expect(
      sent.every(
        ({ url }) => url === '/api/v0/projects/p1/ledger/entry-0123456789abcdef01234567/flags',
      ),
    ).toBe(true);
    expect(changed).toHaveBeenCalledTimes(3);
  });

  it('never clears all when some open flags are not listed; selecting them all clears those read', async () => {
    const sent = posts(() => answer({ cleared_total: 2 }));
    const root = show(LedgerFlags, {
      projectId: 'p1',
      entry: entry({ open_flags: 80 }),
      changed: vi.fn().mockResolvedValue(undefined),
    });
    expect(button(root, 'Clear all 80 open flags').disabled).toBe(true);
    expect(root.textContent).toContain('The newest 3 of the 80 open flags are listed');
    type(root, 'Reason for clearing', 'Read these.');
    button(root, 'Select the 3 listed').click();
    flushSync();
    button(root, 'Clear the 3 selected').click();
    await settle();
    expect(sent.at(-1)!.body.flags).toEqual(['flag-1', 'flag-2', 'flag-3']);
    // The ledger cleared fewer than were asked: the view says so.
    expect(root.textContent).toContain('Cleared 2 flags, though 3 were selected');
  });

  it('sends nothing without a reason, and holds every button while a change runs', async () => {
    let release: (value: Response) => void = () => {};
    const fetch = vi.fn(() => new Promise<Response>((resolve) => (release = resolve)));
    vi.stubGlobal('fetch', fetch);
    const root = show(LedgerFlags, {
      projectId: 'p1',
      entry: entry(),
      changed: vi.fn().mockResolvedValue(undefined),
    });
    button(root, 'Clear this flag').click();
    await settle();
    expect(fetch).not.toHaveBeenCalled();
    expect(root.textContent).toContain('Give the reason');

    type(root, 'Reason for clearing', 'Still holds.');
    button(root, 'Clear this flag').click();
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
    const buttons = [...root.querySelectorAll('button')];
    expect(buttons.every((b) => b.disabled)).toBe(true);
    expect(root.querySelector('textarea')!.disabled).toBe(true);
    release(new Response(JSON.stringify(answer({ cleared_total: 1 }))));
    await settle();
    expect(button(root, 'Clear this flag').disabled).toBe(false);
  });

  it('shows a conflict beside the flags, and keeps the reason', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: { kind: 'conflict', message: 'flag flag-1 is cleared already' },
            }),
            {
              status: 409,
            },
          ),
      ),
    );
    const changed = vi.fn().mockResolvedValue(undefined);
    const root = show(LedgerFlags, { projectId: 'p1', entry: entry(), changed });
    type(root, 'Reason for clearing', 'Still holds.');
    button(root, 'Clear this flag').click();
    await settle();
    expect(root.textContent).toContain('flag flag-1 is cleared already');
    expect(root.querySelector('textarea')!.value).toBe('Still holds.');
    expect(changed).toHaveBeenCalledTimes(1); // the entry is read again after a refusal
  });
});

describe('the history', () => {
  const cleared = (id: string, fields: Partial<ClearedFlag> = {}): ClearedFlag => ({
    flag_id: id,
    cause: 'dependency_status',
    source: { entry: 'entry-0', handle: 'REF-1', revision: 2, status: 'withdrawn' },
    via: null,
    revision: 1,
    raised: '2026-10-15T00:00:00Z',
    cleared: '2026-10-15T00:10:00Z',
    cleared_by: { kind: 'user', user: 'owner' },
    reason: 'Secondary source.',
    operation_id: 'op-c',
    ...fields,
  });

  /** A server for the entry view: the entry, its history, and the user's changes. */
  function server(state: { entry: EntryView; cleared: ClearedFlag[] }) {
    const calls: { method: string; url: string; body?: Record<string, unknown> }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET';
        const body = init?.body
          ? (JSON.parse(String(init.body)) as Record<string, unknown>)
          : undefined;
        calls.push({ method, url, body });
        if (method === 'POST' && url.endsWith('/flags/reopened')) {
          return new Response(JSON.stringify(answer({ flags_reopened: ['flag-new'] })));
        }
        if (method === 'POST' && url.endsWith('/flags')) {
          state.entry = { ...state.entry, open_flags: 0, flags: [] };
          return new Response(JSON.stringify(answer({ cleared_total: 1 })));
        }
        // The work records, read with a claim: none here.
        if (url.includes('/work?'))
          return new Response(
            JSON.stringify({
              project_id: 'p1',
              observed_at: 'now',
              work: { entry_id: 'e', handle: 'X-1', records: [], next_before: null },
            }),
          );
        if (url.endsWith('/gate')) {
          return new Response(
            JSON.stringify({
              project_id: 'p1',
              observed_at: 'now',
              gate: {
                entry_id: 'entry-0123456789abcdef01234567',
                handle: 'C-1',
                revision: 3,
                status: 'proposed',
                applies: true,
                holds: false,
                clauses: [],
                decisions: [],
              },
            }),
          );
        }
        if (url.includes('/history')) {
          return new Response(
            JSON.stringify({
              project_id: 'p1',
              observed_at: '2026-10-15T00:00:00Z',
              history: {
                entry_id: state.entry.entry_id,
                handle: 'C-1',
                revisions: [],
                next_before: null,
                flags_cleared: state.cleared,
              },
            }),
          );
        }
        return new Response(
          JSON.stringify({
            project_id: 'p1',
            observed_at: '2026-10-15T00:00:00Z',
            entry: state.entry,
          }),
        );
      }),
    );
    return calls;
  }

  it('lists the cleared flags, offers only those not open again, and reopens those selected', async () => {
    const state = {
      entry: entry({ open_flags: 1, flags: [flag('flag-9', { reopens: 'flag-b' })] }),
      cleared: [cleared('flag-a'), cleared('flag-b')],
    };
    const calls = server(state);
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'C-1', revision: null });
    await settle();
    button(root, 'Read its revisions').click();
    await settle();
    expect(root.textContent).toContain('Cleared');
    expect(root.textContent).toContain('Open again as flag-9.');
    const history = root.querySelector('h3 ~ ul.flags') as HTMLElement;
    const boxes = [...history.querySelectorAll<HTMLInputElement>('input[type=checkbox]')];
    expect(boxes).toHaveLength(1); // flag-b is open again as flag-9
    boxes[0]!.checked = true;
    boxes[0]!.dispatchEvent(new Event('change', { bubbles: true }));
    type(root, 'Reason for reopening', 'Reconsider.');
    button(root, 'Reopen the 1 selected').click();
    await settle();
    const reopened = calls.find((c) => c.url.endsWith('/flags/reopened'));
    expect(reopened?.body).toEqual({
      flags: ['flag-a'],
      reason: 'Reconsider.',
      operation_id: expect.any(String),
    });
    expect(root.textContent).toContain('Reopened 1 flag');
  });

  it('is read again after a change to the entry, once it was read', async () => {
    const state = {
      entry: entry({ open_flags: 1, flags: [flag('flag-1')] }),
      cleared: [] as ClearedFlag[],
    };
    const calls = server(state);
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'C-1', revision: null });
    await settle();
    button(root, 'Read its revisions').click();
    await settle();
    const before = calls.filter((c) => c.url.includes('/history')).length;
    expect(before).toBe(1);
    state.cleared = [cleared('flag-1', { reason: 'Just cleared.' })];
    type(root, 'Reason for clearing', 'Just cleared.');
    button(root, 'Clear this flag').click();
    await settle();
    expect(calls.filter((c) => c.url.includes('/history')).length).toBe(2);
    expect(root.textContent).toContain('Just cleared.');
    expect(root.textContent).not.toContain('Reason for clearing'); // no open flag left
  });
});
