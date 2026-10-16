// SPDX-License-Identifier: MIT
// "I checked this revision", mounted and used: opening it on a copy of the revision shown,
// confirming, a conflict, the buttons while it runs, and the entry moving on under it.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerCheck from '../views/LedgerCheck.svelte';
import LedgerEntryView from '../views/LedgerEntryView.svelte';
import type { LedgerChangeAnswer } from './api';
import type { EntryFlag, EntryView } from './api-types';

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

const entry = (fields: Partial<EntryView> = {}): EntryView => ({
  entry_id: 'entry-0123456789abcdef01234567',
  handle: 'C-1',
  type: 'claim',
  kind: null,
  session: null,
  standing: null,
  origin: 'project',
  revision: 3,
  head: 3,
  title: 'Fermat for 17',
  status: 'proposed',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 0,
  statement: 'The statement of r3.',
  fields: { kind: 'observation' },
  links: [],
  evidence: [],
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [],
  author: { kind: 'user', user: 'owner' },
  reason: 'r',
  ...fields,
});

const answer: LedgerChangeAnswer = {
  operation_id: 'op',
  replayed: false,
  entries: [],
  cleared_total: 0,
  flags_cleared: [],
  flags_reopened: [],
};

function reply(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

describe('the check panel', () => {
  it('opens on the revision shown, and records the check of that revision with the note', async () => {
    const sent: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        sent.push({ url, body: JSON.parse(String(init?.body)) });
        return reply(200, answer);
      }),
    );
    const changed = vi.fn().mockResolvedValue(undefined);
    const root = show(LedgerCheck, { projectId: 'p1', entry: entry(), changed });
    expect(root.textContent).not.toContain('The statement of r3.');
    button(root, 'I checked this revision').click();
    flushSync();
    expect(root.textContent).toContain('The statement of r3.');
    expect(root.textContent).toContain('The title of C-1 r3, exactly');
    type(root, 'A note', 'All 16 powers by hand.');
    button(root, 'Record my check of r3').click();
    await settle();
    expect(sent).toEqual([
      {
        url: '/api/v0/projects/p1/ledger/entry-0123456789abcdef01234567/checks',
        body: { revision: 3, note: 'All 16 powers by hand.', operation_id: expect.any(String) },
      },
    ]);
    expect(changed).toHaveBeenCalledTimes(1);
    expect(root.textContent).toContain('Your check of r3 is recorded.');
    expect(root.querySelector('div.check')).toBeNull();
  });

  it('records nothing on a newer revision, says so, and closes', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply(409, { error: { kind: 'conflict', message: 'C-1 is at r4' } })),
    );
    const changed = vi.fn().mockResolvedValue(undefined);
    const root = show(LedgerCheck, { projectId: 'p1', entry: entry(), changed });
    button(root, 'I checked this revision').click();
    flushSync();
    button(root, 'Record my check of r3').click();
    await settle();
    expect(root.textContent).toContain('The entry has a newer revision than the one you read');
    expect(root.querySelector('div.check')).toBeNull();
    expect(changed).toHaveBeenCalledTimes(1); // the entry is read again
  });

  it('holds its buttons and the note while the check is on its way', async () => {
    let release: (response: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((resolve) => (release = resolve))),
    );
    const root = show(LedgerCheck, {
      projectId: 'p1',
      entry: entry(),
      changed: vi.fn().mockResolvedValue(undefined),
    });
    button(root, 'I checked this revision').click();
    flushSync();
    button(root, 'Record my check of r3').click();
    await settle();
    expect(button(root, 'Record my check of r3').disabled).toBe(true);
    expect(button(root, 'Cancel').disabled).toBe(true);
    expect(root.querySelector('div.check textarea')!.hasAttribute('disabled')).toBe(true);
    release(reply(200, answer));
    await settle();
    expect(button(root, 'I checked this revision').disabled).toBe(false);
  });

  it('on a reference shows the PDF it names, whole, and vouches for it', async () => {
    const sha = '0123456789abcdef'.repeat(4);
    const sent: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        sent.push({ url, body: JSON.parse(String(init?.body)) });
        return reply(200, answer);
      }),
    );
    const reference = entry({
      handle: 'REF-1',
      type: 'reference',
      status: 'active',
      revision: 2,
      head: 2,
      title: 'A paper',
      fields: {
        authors: ['A. Author'],
        pdf: { artifact_id: 'art-1', revision: 4, sha256: sha },
      },
    });
    const root = show(LedgerCheck, {
      projectId: 'p1',
      entry: reference,
      changed: vi.fn().mockResolvedValue(undefined),
    });
    button(root, 'I checked this revision').click();
    flushSync();
    expect(root.textContent).toContain('Your check vouches for that PDF');
    const exact = [...root.querySelectorAll('pre.exact')].map((pre) => pre.textContent);
    expect(exact).toContain(`artifact art-1, revision 4\nsha256 ${sha}`);
    expect(exact.some((text) => text?.includes('"authors"'))).toBe(true);
    button(root, 'Record my check of r2').click();
    await settle();
    expect(sent).toEqual([
      {
        url: '/api/v0/projects/p1/ledger/entry-0123456789abcdef01234567/checks',
        body: { revision: 2, operation_id: expect.any(String) },
      },
    ]);
    expect(root.textContent).toContain(
      'Your check of r2 is recorded. It vouches for the PDF that revision names.',
    );
  });

  it('on a reference that names no PDF says it vouches for none', () => {
    const root = show(LedgerCheck, {
      projectId: 'p1',
      entry: entry({ handle: 'REF-2', type: 'reference', fields: { authors: ['B'] } }),
      changed: vi.fn().mockResolvedValue(undefined),
    });
    button(root, 'I checked this revision').click();
    flushSync();
    expect(root.textContent).toContain('It names no PDF: your check vouches for none.');
  });

  it('is offered nowhere but a claim, result or reference at its current revision', () => {
    for (const shown of [
      entry({ type: 'issue' }),
      entry({ revision: 2 }),
      entry({ type: 'reference', revision: 2 }),
    ]) {
      const root = show(LedgerCheck, {
        projectId: 'p1',
        entry: shown,
        changed: vi.fn().mockResolvedValue(undefined),
      });
      expect(root.textContent).not.toContain('I checked this revision');
    }
  });
});

describe('the check panel, when the entry moves on under it', () => {
  it('keeps showing the copy it opened on, says the entry moved on, and offers no confirmation', async () => {
    let shown = entry({
      open_flags: 1,
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
        } satisfies EntryFlag,
      ],
    });
    const posts: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          posts.push(url);
          // While the flag is cleared, an agent revises the entry: the next read is r4.
          shown = entry({ revision: 4, head: 4, statement: 'The statement of r4.' });
          return reply(200, { ...answer, cleared_total: 1 });
        }
        // The work records, read with a claim: none here.
        if (url.includes('/work?'))
          return reply(200, {
            project_id: 'p1',
            observed_at: 'now',
            work: { entry_id: 'e', handle: 'X-1', records: [], next_before: null },
          });
        if (url.endsWith('/gate'))
          return reply(200, {
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
          });
        return reply(200, { project_id: 'p1', observed_at: 'now', entry: shown });
      }),
    );
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'C-1', revision: null });
    await settle();
    button(root, 'I checked this revision').click();
    flushSync();
    type(root, 'Reason for clearing', 'Still holds.');
    button(root, 'Clear this flag').click();
    await settle();
    const panel = root.querySelector('div.check') as HTMLElement;
    expect(panel.textContent).toContain('The statement of r3.');
    expect(panel.textContent).not.toContain('The statement of r4.');
    expect(panel.textContent).toContain('The entry has moved on to r4 since you opened this');
    expect(button(root, 'Record my check of r3').disabled).toBe(true);
    expect(posts.some((url) => url.endsWith('/checks'))).toBe(false);
  });
});
