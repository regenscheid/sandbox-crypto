// SPDX-License-Identifier: MIT
// The work records on the entry view, mounted: read with an issue, claim or result,
// older ones on request, none for other types, and a failed read beside the entry.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerEntryView from '../views/LedgerEntryView.svelte';
import type { EntryView, WorkRecord } from './api-types';

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

const ID = 'entry-0123456789abcdef01234567';

const entry = (type: EntryView['type']): EntryView => ({
  entry_id: ID,
  handle: type === 'issue' ? 'I-1' : 'A-1',
  type,
  origin: null,
  revision: 2,
  head: 2,
  title: 'Recover the key',
  status: type === 'issue' ? 'open' : 'active',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 0,
  statement: 'S',
  fields: {},
  links: [],
  evidence: [],
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [],
  author: { kind: 'user', user: 'owner' },
  reason: 'r',
});

const record = (n: number, fields: Partial<WorkRecord> = {}): WorkRecord => ({
  record_id: `work-${String(n).padStart(24, '0')}`,
  position: n,
  entry_id: ID,
  handle: 'I-1',
  entry_revision: 2,
  tried: `Attempt ${n}.`,
  ending: 'limit',
  limit: 'ideas',
  produced: [],
  assesses: null,
  verdict: null,
  references: [],
  part_of: null,
  author: { kind: 'user', user: 'owner' },
  reason: 'r',
  operation_id: `op-${n}`,
  provenance: null,
  created: '2026-10-15T00:00:00Z',
  ...fields,
});

/** The entry, its gate, and its work records by `before` (a status for a failed read). */
function server(shown: EntryView, pages: Record<string, WorkRecord[] | number>): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      urls.push(url);
      const reply = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), { status });
      const found = new URL(url, 'http://localhost');
      if (found.pathname.endsWith('/work')) {
        const page = pages[found.searchParams.get('before') ?? ''];
        if (typeof page === 'number' || page === undefined) {
          return reply({ error: { kind: 'unavailable', message: 'no work now' } }, page ?? 404);
        }
        const next = page.length ? page.at(-1)!.position : null;
        return reply({
          project_id: 'p1',
          observed_at: 'now',
          work: {
            entry_id: ID,
            handle: 'I-1',
            records: page,
            next_before: next === 1 ? null : next,
          },
        });
      }
      if (found.pathname.endsWith('/gate')) {
        return reply({
          project_id: 'p1',
          observed_at: 'now',
          gate: {
            entry_id: ID,
            handle: shown.handle,
            revision: 2,
            status: shown.status,
            applies: false,
            holds: false,
            clauses: [],
            decisions: [],
          },
        });
      }
      return reply({ project_id: 'p1', observed_at: 'now', entry: shown });
    }),
  );
  return urls;
}

function show(): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(LedgerEntryView, {
      target,
      props: { projectId: 'p1', entryName: 'I-1', revision: null },
    }),
  );
  flushSync();
  return target;
}

const tried = (root: HTMLElement) =>
  [...root.querySelectorAll('li.work .tried')].map((p) => p.textContent);
const button = (root: HTMLElement, words: string) =>
  [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === words);

describe('the work records on an entry', () => {
  it('are read with an issue, newest first, and older ones on request', async () => {
    const urls = server(entry('issue'), {
      '': [record(3), record(2, { ending: 'obstruction_claimed', limit: null })],
      '2': [record(1)],
    });
    const root = show();
    await settle();
    expect(tried(root)).toEqual(['Attempt 3.', 'Attempt 2.']);
    expect(root.textContent).toContain('Obstruction claimed');
    button(root, 'Older work records')!.click();
    await settle();
    expect(urls.filter((url) => url.includes('/work'))).toEqual([
      `/api/v0/projects/p1/ledger/${ID}/work?limit=20`,
      `/api/v0/projects/p1/ledger/${ID}/work?limit=20&before=2`,
    ]);
    expect(tried(root)).toEqual(['Attempt 3.', 'Attempt 2.', 'Attempt 1.']);
    expect(button(root, 'Older work records')).toBeUndefined();
  });

  it('are not read for an entry that holds none', async () => {
    const urls = server(entry('assumption'), { '': [] });
    const root = show();
    await settle();
    expect(urls.some((url) => url.includes('/work'))).toBe(false);
    expect(root.textContent).not.toContain('Work records');
  });

  it('say when there are none, and show a failed read beside the entry', async () => {
    server(entry('issue'), { '': [] });
    const empty = show();
    await settle();
    expect(empty.textContent).toContain('No work is recorded on it yet.');
    unmount(mounted.pop()!);
    server(entry('issue'), { '': 503 });
    const failed = show();
    await settle();
    expect(failed.textContent).toContain('no work now');
    expect(failed.textContent).toContain('Recover the key');
  });

  it("show a delegated check's verdict and status, as a check of the entry (lab 40, B6)", async () => {
    server(entry('issue'), {
      '': [
        record(4, {
          tried: 'An independent check of I-1 at revision 2, by a delegated run.',
          ending: 'assessment',
          limit: null,
          references: [{ delegation: 'del-1', note: 'refuted, complete: Step 3 fails.' }],
          author: { kind: 'agent', session_id: 's1', run_id: 'run-2', role: 'delegated' },
        }),
      ],
    });
    const root = show();
    await settle();
    const item = root.querySelector('li.work')!;
    expect(item.querySelector('strong')!.textContent).toBe('Check of the entry');
    expect(item.textContent).toContain(
      'A check of the entry by delegation del-1: refuted, complete.',
    );
    expect(item.textContent).not.toContain('critique');
    expect(
      [...item.querySelectorAll('a')]
        .find((a) => a.textContent === 'The run tree')!
        .getAttribute('href'),
    ).toBe('#/projects/p1/sessions/s1/tree');
  });
});
