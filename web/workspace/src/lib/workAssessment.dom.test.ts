// SPDX-License-Identifier: MIT
// The user's acceptance and rejection of a claimed obstruction, mounted on the entry view:
// the claim and the issue shown exactly, the assessment bound to the revision read with
// the user's note, a missing note, a conflict that records nothing and then the issue
// moving on, and no offer where none belongs.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerEntryView from '../views/LedgerEntryView.svelte';
import type { LedgerChangeAnswer } from './api';
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
const CLAIM = 'work-00000000000000000000000a';

const issue = (fields: Partial<EntryView> = {}): EntryView => ({
  entry_id: ID,
  pins: [],
  figure_checks: [],
  handle: 'I-1',
  type: 'issue',
  kind: null,
  session: null,
  standing: null,
  basis: null,
  anchors: [],
  origin: null,
  revision: 2,
  head: 2,
  title: 'Recover the key',
  status: 'open',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 0,
  statement: 'Recover the key from the leaked signatures.',
  fields: { scope: 'classical attacks' },
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

const record = (id: string, fields: Partial<WorkRecord> = {}): WorkRecord => ({
  record_id: id,
  standing: null,
  decision: null,
  basis: null,
  anchors: [],
  position: 2,
  entry_id: ID,
  handle: 'I-1',
  entry_revision: 2,
  tried: 'Every reduction we know needs the hidden subgroup oracle.',
  ending: 'obstruction_claimed',
  limit: null,
  produced: [],
  assesses: null,
  verdict: null,
  references: [{ delegation: 'del-1', note: 'its outcome' }],
  part_of: null,
  author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
  reason: 'r',
  operation_id: 'op-1',
  provenance: null,
  created: '2026-10-15T00:00:00Z',
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

interface Call {
  method: string;
  url: string;
  body: Record<string, unknown> | null;
}

/** The issue and its records; the assessment's answer, and the issue as read after it. */
function server(state: {
  entry: EntryView;
  records: WorkRecord[];
  assess: number;
  after?: EntryView;
}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
      calls.push({ method, url, body });
      const reply = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), { status });
      if (method === 'POST') {
        if (state.after) state.entry = state.after;
        if (state.assess !== 200) {
          return reply({ error: { kind: 'conflict', message: 'I-1 is at r3' } }, state.assess);
        }
        state.records = [
          record('work-00000000000000000000000b', {
            position: 3,
            ending: 'assessment',
            assesses: CLAIM,
            verdict: body!.verdict as WorkRecord['verdict'],
            tried: String(body!.note),
            references: [],
            author: { kind: 'user', user: 'owner' },
          }),
          ...state.records,
        ];
        return reply(answer);
      }
      if (url.includes('/work?')) {
        return reply({
          project_id: 'p1',
          observed_at: 'now',
          work: { entry_id: ID, handle: 'I-1', records: state.records, next_before: null },
        });
      }
      if (url.endsWith('/gate')) {
        return reply({
          project_id: 'p1',
          observed_at: 'now',
          gate: {
            entry_id: ID,
            handle: 'I-1',
            revision: state.entry.revision,
            status: 'open',
            applies: false,
            holds: false,
            clauses: [],
            decisions: [],
          },
        });
      }
      return reply({ project_id: 'p1', observed_at: 'now', entry: state.entry });
    }),
  );
  return calls;
}

function show(revision: number | null = null): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(LedgerEntryView, { target, props: { projectId: 'p1', entryName: 'I-1', revision } }),
  );
  flushSync();
  return target;
}

const button = (root: HTMLElement, words: string) =>
  [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === words) as
    HTMLButtonElement | undefined;
const exact = (root: HTMLElement) =>
  [...root.querySelectorAll('pre.exact')].map((pre) => pre.textContent);

function write(root: HTMLElement, text: string): void {
  const box = [...root.querySelectorAll('label')]
    .find((label) => label.textContent?.includes('Your note'))!
    .querySelector('textarea')!;
  box.value = text;
  box.dispatchEvent(new Event('input'));
  flushSync();
}

describe("the user's assessment of a claimed obstruction", () => {
  it.each([
    ['Accept…', 'Accept the claim at r2', 'accepted', 'acceptance'],
    ['Reject…', 'Reject the claim at r2', 'rejected', 'rejection'],
  ])(
    '%s shows the claim and the issue exactly, and records it at the revision read',
    async (open, confirm, verdict, word) => {
      const calls = server({ entry: issue(), records: [record(CLAIM)], assess: 200 });
      const root = show();
      await settle();
      button(root, open)!.click();
      flushSync();
      expect(exact(root)).toEqual([
        'Every reduction we know needs the hidden subgroup oracle.\n\nIts evidence:\n' +
          '1. {"delegation":"del-1"}\nNote: its outcome',
        'Recover the key from the leaked signatures.',
        '{\n  "scope": "classical attacks"\n}',
      ]);
      write(root, 'I read the reduction and its outcome.');
      button(root, confirm)!.click();
      await settle();
      const post = calls.find((call) => call.method === 'POST')!;
      expect(post.url).toBe(`/api/v0/projects/p1/ledger/${ID}/assessments`);
      expect(post.body).toEqual({
        claim: CLAIM,
        verdict,
        revision: 2,
        note: 'I read the reduction and its outcome.',
        operation_id: expect.any(String),
      });
      expect(root.querySelector('[role="status"]')!.textContent).toBe(
        `Your ${word} of the obstruction claimed in ${CLAIM}, on I-1 r2, is recorded.`,
      );
      // The entry and its work records are read again: the assessment is listed.
      expect(calls.filter((call) => call.url.includes('/work?'))).toHaveLength(2);
      expect(root.textContent).toContain(`Assesses work record ${CLAIM} (obstruction claimed)`);
      expect(root.querySelector('.frontier-action')).toBeNull();
    },
  );

  it('needs a note before it sends anything', async () => {
    const calls = server({ entry: issue(), records: [record(CLAIM)], assess: 200 });
    const root = show();
    await settle();
    button(root, 'Accept…')!.click();
    flushSync();
    button(root, 'Accept the claim at r2')!.click();
    await settle();
    expect(root.textContent).toContain('Say what you read, and why you decide so.');
    expect(calls.some((call) => call.method === 'POST')).toBe(false);
  });

  it('records nothing on a newer revision, says so, and offers no confirmation of the copy', async () => {
    const calls = server({
      entry: issue(),
      records: [record(CLAIM)],
      assess: 409,
      after: issue({ revision: 3, head: 3, statement: 'Reworded.' }),
    });
    const root = show();
    await settle();
    button(root, 'Accept…')!.click();
    flushSync();
    write(root, 'I read it.');
    button(root, 'Accept the claim at r2')!.click();
    await settle();
    expect(root.textContent!.replace(/\s+/g, ' ')).toContain(
      'I-1 has a newer revision than the r2 you read, so nothing was recorded.',
    );
    expect(root.textContent!.replace(/\s+/g, ' ')).toContain('I-1 has moved on to r3');
    expect(button(root, 'Accept the claim at r2')!.disabled).toBe(true);
    // What the user read stays on screen: the copy of r2, not the reworded r3.
    expect(exact(root)[1]).toBe('Recover the key from the leaked signatures.');
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
    expect(root.querySelector('[role="status"]')).toBeNull();
  });

  it('is offered on no other record, and not on an earlier revision', async () => {
    server({
      entry: issue({ revision: 1, head: 2 }),
      records: [
        record(CLAIM),
        record('work-00000000000000000000000c', { ending: 'limit', limit: 'time' }),
      ],
      assess: 200,
    });
    const earlier = show(1);
    await settle();
    expect(button(earlier, 'Accept…')).toBeUndefined();
    unmount(mounted.pop()!);
    server({
      entry: issue(),
      records: [record('work-00000000000000000000000c', { ending: 'limit', limit: 'time' })],
      assess: 200,
    });
    const current = show();
    await settle();
    expect(button(current, 'Accept…')).toBeUndefined();
    expect(button(current, 'Reject…')).toBeUndefined();
  });
});
