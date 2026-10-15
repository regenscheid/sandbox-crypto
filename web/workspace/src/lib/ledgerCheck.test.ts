// SPDX-License-Identifier: MIT
// The user's check of a ledger entry: what they attest to shows as exact text, every
// hidden character as its code point (hidden.fixtures.json), and the check names the
// revision read, so a newer one is a conflict.
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerCheckText from '../views/LedgerCheckText.svelte';
import LedgerEntryBody from '../views/LedgerEntryBody.svelte';
import { api, ApiError, type LedgerChangeAnswer } from './api';
import type { EntryLink, EntryView, EvidenceLink } from './api-types';
import fixtures from './hidden.fixtures.json';
import {
  checkable,
  checkFailure,
  assumesText,
  checkRecord,
  evidenceText,
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

const evidence = (fields: Partial<EvidenceLink> = {}): EvidenceLink => ({
  evidence_id: 'ev-1',
  role: 'supports',
  type: 'exact_computation',
  target: 'artifact',
  target_id: 'artifact-9',
  revision: 2,
  block: null,
  span: null,
  sha256: 'ab'.repeat(32),
  checked: null,
  locator: null,
  scope: null,
  added: 1,
  handle: null,
  ...fields,
});

const assumes = (fields: Partial<EntryLink> = {}): EntryLink => ({
  kind: 'assumes',
  target: 'entry',
  target_id: 'entry-0a',
  revision: 2,
  block: null,
  note: null,
  handle: 'A-1',
  session: null,
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
  /** An entry whose one place named holds `text`, and nothing hidden anywhere else. */
  const holding = (where: string, text: string): EntryView => {
    switch (where) {
      case 'title':
        return entry({ title: text });
      case 'statement':
        return entry({ statement: text });
      case 'fields':
        return entry({ fields: { note: text } });
      case 'evidence scope':
        return entry({ evidence: [evidence({ scope: text })] });
      default:
        return entry({ links: [assumes({ note: text })] });
    }
  };
  const places = ['title', 'statement', 'fields', 'evidence scope', 'assumes note'];
  const cases = fixtures.cases.flatMap((c) =>
    places.map((where) => ({ ...c, where, hidden: c.hidden as [number, string][] })),
  );

  it.each(cases)(
    '$name, in its $where: every hidden character shows as its code point',
    ({ text, hidden, where }) => {
      const html = render(LedgerCheckText, { props: { entry: holding(where, text) } }).body;
      for (const [, code] of hidden) {
        const character = String.fromCodePoint(parseInt(code.slice(2), 16));
        // JSON writes a control character in the fields as an escape, which is visible text.
        const escaped = where === 'fields' && JSON.stringify(character).length > 3;
        if (escaped) expect(html).not.toContain(character);
        else expect(html).toContain(`⟦${code}⟧`);
      }
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

describe('the text of what a check pins', () => {
  it('names every evidence link with its role, type, whole target and scope note', () => {
    expect(
      evidenceText([
        evidence({ scope: '$p = 17$ only' }),
        evidence({
          role: 'independent_check',
          type: 'proof',
          target: 'entry',
          target_id: 'entry-0b',
          handle: 'C-1',
          checked: 3,
          sha256: null,
        }),
        evidence({ target: 'block', target_id: 'doc-1', block: 'b4', span: 'm1', sha256: null }),
        evidence({ target: 'review', target_id: 'rev-1', revision: null, sha256: null }),
      ]),
    ).toBe(
      [
        `1. Supports, exact computation: artifact artifact-9 r2, sha256 ${'ab'.repeat(32)}\nScope: $p = 17$ only`,
        '2. Independent check, proof, of revision 3: entry C-1 (entry-0b) r2',
        '3. Supports, exact computation: document doc-1 r2, block b4, math span m1',
        '4. Supports, exact computation: review rev-1',
      ].join('\n\n'),
    );
    expect(evidenceText([])).toBe('');
  });

  it('names every assumes link, uncapped, and no other kind', () => {
    const many = Array.from({ length: 80 }, (_, n) => assumes({ handle: `A-${n + 1}` }));
    const text = assumesText([...many, assumes({ kind: 'builds_on', handle: 'C-9' })]);
    expect(text.split('\n')).toHaveLength(80);
    expect(text).toContain('Assumes A-80 r2');
    expect(text).not.toContain('C-9');
    expect(assumesText([assumes({ note: 'only for 17' })])).toBe('Assumes A-1 r2: only for 17');
  });

  it('shows every evidence link and assumption in the confirmation, uncapped', () => {
    const links = Array.from({ length: 70 }, (_, n) =>
      evidence({ evidence_id: `ev-${n}`, scope: `scope ${n}` }),
    );
    const html = render(LedgerCheckText, {
      props: { entry: entry({ evidence: links, links: [assumes({})] }) },
    }).body;
    expect(html).toContain('all 70 links');
    expect(html).toContain('scope 69');
    expect(html).toContain('Assumes A-1 r2');
  });
});
