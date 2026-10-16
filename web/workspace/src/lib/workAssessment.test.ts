// SPDX-License-Identifier: MIT
// The user's assessment of a claimed obstruction (LG-7): offered only on an issue at its
// current revision, on a claimed obstruction; the claim and the issue shown as exact text,
// every hidden character as its code point (hidden.fixtures.json), since an acceptance
// holds only for that wording.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import WorkAssessment from '../views/WorkAssessment.svelte';
import { ApiError } from './api';
import type { EntryView, WorkRecord } from './api-types';
import { exactViews } from './approvalTesting';
import fixtures from './hidden.fixtures.json';
import { assessable, assessmentConflict, assessmentProblem, claimText } from './ledgerWork';

const claim = (fields: Partial<WorkRecord> = {}): WorkRecord => ({
  record_id: 'work-0123456789abcdef01234567',
  standing: null,
  decision: null,
  basis: null,
  anchors: [],
  position: 4,
  entry_id: 'entry-0123456789abcdef01234567',
  handle: 'I-1',
  entry_revision: 2,
  tried: 'Every reduction we know needs the hidden subgroup oracle.',
  ending: 'obstruction_claimed',
  limit: null,
  produced: [],
  assesses: null,
  verdict: null,
  references: [{ document: 'doc-1', revision: 3, block: 'b2', note: 'the argument' }],
  part_of: null,
  author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
  reason: 'r',
  operation_id: 'op-1',
  provenance: null,
  created: '2026-10-15T00:00:00Z',
  ...fields,
});

const issue = (fields: Partial<EntryView> = {}): EntryView => ({
  entry_id: 'entry-0123456789abcdef01234567',
  pins: [],
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

const panel = (entry: EntryView, record: WorkRecord) =>
  render(WorkAssessment, {
    props: {
      projectId: 'p1',
      entry,
      claim: record,
      verdict: 'accepted',
      done: async () => {},
      oncancel: () => {},
    },
  }).body;

describe("the user's assessment of a claimed obstruction", () => {
  it('is offered only on an issue at its current revision, on a claimed obstruction', () => {
    expect(assessable(issue(), claim())).toBe(true);
    expect(assessable(issue({ revision: 1 }), claim())).toBe(false);
    expect(assessable(issue({ type: 'claim' }), claim())).toBe(false);
    expect(assessable(issue(), claim({ ending: 'limit' }))).toBe(false);
  });

  it('shows the claim exactly: what was tried, then each piece of evidence and its note', () => {
    expect(claimText(claim())).toBe(
      'Every reduction we know needs the hidden subgroup oracle.\n\nIts evidence:\n' +
        '1. {"document":"doc-1","revision":3,"block":"b2"}\nNote: the argument',
    );
    expect(claimText(claim({ references: [] }))).toMatch(/It cites no evidence\.$/);
  });

  it('needs a note, and says what a conflict means', () => {
    expect(assessmentProblem(' ')).toBe('Say what you read, and why you decide so.');
    expect(assessmentProblem('x'.repeat(8193))).toMatch(/longer than 8192/);
    expect(assessmentProblem('I read the reduction.')).toBeNull();
    expect(assessmentConflict(new ApiError(409, 'conflict', 'x'), 'I-1', 2)).toMatch(
      /^I-1 has a newer revision than the r2 you read, so nothing was recorded\./,
    );
    expect(assessmentConflict(new ApiError(503, 'unavailable', 'x'), 'I-1', 2)).toBeNull();
  });

  it('renders no text as HTML', () => {
    const views = import.meta.glob('../views/WorkAssessment.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    expect(Object.keys(views)).toHaveLength(1);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });

  it('shows the claim, the statement and the fields, each whole', () => {
    const views = exactViews(panel(issue(), claim()));
    expect(views).toEqual([
      claimText(claim()),
      'Recover the key from the leaked signatures.',
      '{\n  "scope": "classical attacks"\n}',
    ]);
  });

  const places = ['what was tried', 'an evidence note', "the issue's statement"];
  const cases = fixtures.cases.flatMap((c) =>
    places.map((where) => ({ ...c, where, hidden: c.hidden as [number, string][] })),
  );
  it.each(cases)(
    '$name, in $where: every hidden character shows as its code point',
    ({ text, where, hidden }) => {
      const html =
        where === 'what was tried'
          ? panel(issue(), claim({ tried: text }))
          : where === 'an evidence note'
            ? panel(issue(), claim({ references: [{ delegation: 'del-1', note: text }] }))
            : panel(issue({ statement: text }), claim());
      for (const [, code] of hidden) expect(html).toContain(`⟦${code}⟧`);
    },
  );
});
