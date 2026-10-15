// SPDX-License-Identifier: MIT
// The user's check of a ledger entry: what they attest to shows as exact text, every
// hidden character as its code point (hidden.fixtures.json), and the check names the
// revision read, so a newer one is a conflict.
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerCheckText from '../views/LedgerCheckText.svelte';
import LedgerEntryBody from '../views/LedgerEntryBody.svelte';
import { api, ApiError, type LedgerChangeAnswer } from './api';
import type { EntryView } from './api-types';
import fixtures from './hidden.fixtures.json';
import {
  checkable,
  checkFailure,
  checkRecord,
  fieldsText,
  noteProblem,
  recordCheck,
} from './ledgerCheck';
import { idle } from './submit';

afterEach(() => vi.unstubAllGlobals());

const answer: LedgerChangeAnswer = {
  operation_id: 'op',
  replayed: false,
  entries: [{ entry_id: 'entry-1', handle: 'C-1', revision: 4, status: 'proposed' }],
  cleared_total: 0,
  flags_cleared: [],
  flags_reopened: [],
};

const entry = (fields: Partial<EntryView> = {}): EntryView => ({
  entry_id: 'entry-0123456789abcdef01234567',
  handle: 'C-1',
  type: 'claim',
  origin: 'project',
  revision: 3,
  head: 3,
  title: 'Fermat for 17',
  status: 'proposed',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 0,
  statement: '$a^{16} = 1$ for every nonzero $a$.',
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

describe('a check', () => {
  it('is offered on a claim or result at its current revision only', () => {
    expect(checkable(entry())).toBe(true);
    expect(checkable(entry({ type: 'result' }))).toBe(true);
    expect(checkable(entry({ revision: 2 }))).toBe(false);
    for (const type of ['issue', 'assumption', 'decision', 'task', 'reference'] as const) {
      expect(checkable(entry({ type })), type).toBe(false);
    }
  });

  it('names the revision read, and its note only when there is one', () => {
    expect(checkRecord(3, '')).toEqual({ revision: 3 });
    expect(checkRecord(3, '  \n')).toEqual({ revision: 3 });
    expect(checkRecord(3, 'By hand, all 16.')).toEqual({ revision: 3, note: 'By hand, all 16.' });
    expect(noteProblem('x'.repeat(4097))).toMatch(/longer than 4096/);
    expect(noteProblem('')).toBeNull();
  });

  it('shows the fields as exact text, and none for an entry without', () => {
    expect(fieldsText({})).toBe('');
    expect(fieldsText({ kind: 'lemma', measures: { time: 1 } })).toBe(
      '{\n  "kind": "lemma",\n  "measures": {\n    "time": 1\n  }\n}',
    );
  });
});

describe('what the user attests to, as they will be held to it', () => {
  it.each(fixtures.cases)(
    '$name: every hidden character shows as its code point',
    ({ text, hidden }) => {
      const html = render(LedgerCheckText, {
        props: { entry: entry({ title: text, statement: text, fields: { note: text } }) },
      }).body;
      for (const [, code] of hidden as [number, string][]) expect(html).toContain(`⟦${code}⟧`);
      if ((hidden as unknown[]).length > 0) expect(html).toContain('you may not see or tell apart');
    },
  );

  it('shows hostile text as text, the statement rendered apart and labelled', () => {
    const hostile = '<script>alert(1)</script><img src=x onerror=alert(1)>[a](javascript:x)';
    const html = render(LedgerCheckText, {
      props: {
        entry: entry({ title: hostile, statement: hostile, fields: { [hostile]: hostile } }),
      },
    }).body;
    expect(html).not.toMatch(/<script|<img/);
    expect(html).toContain('A preview, as rendered');
    expect(html).toContain('The title of C-1 r3, exactly');
  });

  it('is offered from the entry view only where it can be recorded', () => {
    const offered = render(LedgerEntryBody, { props: { projectId: 'p1', entry: entry() } }).body;
    expect(offered).toContain('I checked this revision');
    for (const shown of [entry({ revision: 2 }), entry({ type: 'issue' })]) {
      const html = render(LedgerEntryBody, { props: { projectId: 'p1', entry: shown } }).body;
      expect(html).not.toContain('I checked this revision');
    }
  });
});

describe("the user's check, sent", () => {
  it('posts the revision read with an operation ID, then reads the entry again', async () => {
    const send = vi.fn().mockResolvedValue(answer);
    const reload = vi.fn().mockResolvedValue(undefined);
    const saving = idle();
    expect(
      await recordCheck(saving, 'p1', 'entry-1', { revision: 3, note: 'n' }, send, reload),
    ).toBe(true);
    expect(send.mock.calls[0]?.[0]).toEqual({
      revision: 3,
      note: 'n',
      operation_id: expect.any(String),
    });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('on a newer revision, records nothing, says so, and reads the entry again', async () => {
    const reload = vi.fn().mockResolvedValue(undefined);
    const saving = idle();
    const conflict = vi.fn().mockRejectedValue(new ApiError(409, 'conflict', 'C-1 is at r4'));
    expect(await recordCheck(saving, 'p1', 'entry-1', { revision: 3 }, conflict, reload)).toBe(
      false,
    );
    expect(checkFailure(saving.error)).toMatch(/newer revision than the one you read/);
    expect(reload).toHaveBeenCalledTimes(1);
    // The same check again keeps its operation ID: a lost answer is not recorded twice.
    await recordCheck(saving, 'p1', 'entry-1', { revision: 3 }, conflict, reload);
    expect(conflict.mock.calls[1]?.[0].operation_id).toBe(conflict.mock.calls[0]?.[0].operation_id);
  });

  it('keeps any other refusal as it is', async () => {
    const saving = idle();
    const refused = vi.fn().mockRejectedValue(new ApiError(400, 'invalid_request', 'bad note'));
    await recordCheck(saving, 'p1', 'entry-1', { revision: 3, note: 'x' }, refused, async () => {});
    expect((saving.error as ApiError).status).toBe(400);
    expect(checkFailure(saving.error)).toBeNull();
  });

  it("posts to the entry's checks", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(answer)));
    vi.stubGlobal('fetch', fetch);
    await api.checkLedgerEntry('p/1', 'C-1', { revision: 3, operation_id: 'o1' });
    const [url, init] = (fetch.mock.calls as unknown as [string, RequestInit][])[0]!;
    expect(url).toBe('/api/v0/projects/p%2F1/ledger/C-1/checks');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ revision: 3, operation_id: 'o1' });
  });
});
