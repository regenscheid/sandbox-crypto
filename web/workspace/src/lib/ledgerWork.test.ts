// SPDX-License-Identifier: MIT
// An entry's work records as the entry view shows them (LG-6, LG-7): how each ended and
// its detail, its references leading where the workspace shows them, the author's text as
// text with every hidden character as its code point (hidden.fixtures.json), and a
// decision's assessment field leading to the issue it obstructs.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import WorkRecordItem from '../views/WorkRecordItem.svelte';
import type { EntryLink, WorkRecord } from './api-types';
import { hostileMarkup } from './approvalTesting';
import fixtures from './hidden.fixtures.json';
import { fieldRows } from './ledger';
import {
  checkView,
  detailText,
  endingText,
  holdsWork,
  producedLinks,
  referenceView,
} from './ledgerWork';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const record = (fields: Partial<WorkRecord> = {}): WorkRecord => ({
  record_id: 'work-0123456789abcdef01234567',
  position: 7,
  entry_id: 'entry-0123456789abcdef01234567',
  handle: 'I-1',
  entry_revision: 2,
  tried: 'Lattice reduction with BKZ-60.',
  ending: 'limit',
  limit: 'time',
  produced: [],
  assesses: null,
  verdict: null,
  references: [],
  part_of: null,
  author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
  reason: 'r',
  operation_id: 'op-1',
  provenance: null,
  created: '2026-10-15T00:00:00Z',
  ...fields,
});

const hrefs = (html: string) => [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]!);

describe('a work record', () => {
  it('says how it ended: what ran out, or what it assesses and the verdict', () => {
    expect(detailText(record())).toBe('Ran out of time; the work can go on.');
    const claim = record({ record_id: 'work-a', ending: 'obstruction_claimed', limit: null });
    expect(detailText(claim)).toBe('');
    const assessment = record({
      ending: 'assessment',
      limit: null,
      assesses: 'work-a',
      verdict: 'accepted',
    });
    expect(detailText(assessment, [claim])).toBe(
      'Assesses work record work-a (obstruction claimed): accepted.',
    );
    expect(detailText(assessment)).toBe('Assesses work record work-a: accepted.');
    expect(detailText(record({ ending: 'assessment', limit: null, verdict: 'rejected' }))).toBe(
      'A critique, rejected.',
    );
  });

  it('names each reference, leading to a document block at its revision', () => {
    const sha = 'ab'.repeat(32);
    expect(referenceView('p1', { delegation: 'del-1', note: 'its outcome' })).toEqual({
      text: 'the outcome of delegation del-1',
      href: null,
      note: 'its outcome',
    });
    expect(referenceView('p1', { artifact: 'art-1', revision: 2, sha256: sha }).text).toBe(
      'artifact art-1 r2, sha256 abababababab…',
    );
    expect(referenceView('p 1', { document: 'doc-1', revision: 3, block: 'b4' })).toMatchObject({
      text: 'document doc-1 r3, block b4',
      href: '#/projects/p%201/documents/doc-1/revisions/3/blocks/b4',
    });
    expect(referenceView('p1', { document: 'doc-1', revision: 3, block: '../x' }).href).toBeNull();
    expect(
      referenceView('p1', { work: 'work-a' }, [record({ record_id: 'work-a', ending: 'produced' })])
        .text,
    ).toBe('work record work-a, produced');
    expect(referenceView('p1', { other: 1 }).text).toBe('{"other":1}');
  });

  it('leads to the entries it produced, and only to names the server takes', () => {
    expect(producedLinks('p1', record({ produced: ['C-2', 'C-1/../x'] }))).toEqual([
      { text: 'C-2', href: '#/projects/p1/ledger/C-2' },
      { text: 'C-1/../x', href: null },
    ]);
    expect(['issue', 'claim', 'result'].every(holdsWork)).toBe(true);
    expect(['assumption', 'decision', 'task', 'reference'].some(holdsWork)).toBe(false);
  });

  it('is shown as text, every link leading into the workspace', () => {
    const html = render(WorkRecordItem, {
      props: {
        projectId: 'p1',
        record: record({
          record_id: HOSTILE,
          tried: `${HOSTILE}\n\n[a](javascript:alert(1))`,
          ending: 'produced',
          limit: null,
          produced: [HOSTILE, 'C-3'],
          references: [
            { document: HOSTILE, revision: 1, block: 'b1', note: HOSTILE },
            { delegation: HOSTILE },
            { work: HOSTILE, note: 'javascript:alert(1)' },
          ],
          part_of: HOSTILE,
          author: { kind: 'agent', role: HOSTILE, run_id: HOSTILE },
          reason: HOSTILE,
        }),
      },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect(hrefs(html)).toEqual([
      '#/projects/p1/ledger/C-3',
      `#/projects/p1/documents/${encodeURIComponent(HOSTILE)}/revisions/1/blocks/b1`,
    ]);
  });

  const cases = fixtures.cases.flatMap((c) =>
    ['what was tried', "a reference's note", 'the reason'].map((where) => ({
      ...c,
      where,
      hidden: c.hidden as [number, string][],
    })),
  );
  it.each(cases)(
    '$name, in $where: each hidden character as its code point',
    ({ text, where, hidden }) => {
      const shown = record(
        where === 'what was tried'
          ? { tried: text }
          : where === 'the reason'
            ? { reason: text }
            : { references: [{ delegation: 'del-1', note: text }] },
      );
      const html = render(WorkRecordItem, { props: { projectId: 'p1', record: shown } }).body;
      for (const [, code] of hidden) expect(html).toContain(`⟦${code}⟧`);
    },
  );

  it('views render no text as HTML', () => {
    const views = import.meta.glob(
      ['../views/WorkRecordItem.svelte', '../views/LedgerWork.svelte'],
      {
        query: '?raw',
        import: 'default',
        eager: true,
      },
    );
    expect(Object.keys(views)).toHaveLength(2);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });
});

describe("a delegated check's record (lab 40, B6)", () => {
  /** A check's outcome as the platform records it (delegation/work.py, `_check`). */
  const checked = (note: string, fields: Partial<WorkRecord> = {}) =>
    record({
      tried: 'An independent check of C-4 at revision 2, by a delegated run with a fresh context.',
      ending: 'assessment',
      limit: null,
      references: [{ delegation: 'del-1', note }],
      author: { kind: 'agent', session_id: 's1', run_id: 'run-2', role: 'delegated' },
      provenance: { delegation: 'del-1' },
      ...fields,
    });

  it('is a check of the entry, with the verdict and status its note begins with', () => {
    const shown = checked('confirmed, complete: Every step holds.');
    expect(checkView(shown)).toEqual({
      delegation: 'del-1',
      verdict: 'confirmed',
      status: 'complete',
      session: 's1',
    });
    expect(endingText(shown)).toBe('Check of the entry');
    expect(detailText(shown)).toBe(
      'A check of the entry by delegation del-1: confirmed, complete.',
    );
    expect(detailText(checked('refuted, partial'))).toMatch(/: refuted, partial\.$/);
  });

  it('reads only the head the platform writes, and says when the note gives no verdict', () => {
    expect(detailText(checked('the verdict is confirmed, complete'))).toBe(
      'A check of the entry by delegation del-1: its note gives no verdict; its outcome is in ' +
        'the run tree.',
    );
    expect(checkView(checked('approved, complete: x'))!.verdict).toBeNull();
  });

  it('is no other assessment', () => {
    expect(checkView(checked('confirmed, complete', { assesses: 'work-a' }))).toBeNull();
    expect(
      checkView(checked('x', { references: [{ document: 'doc-1', revision: 1, block: 'b1' }] })),
    ).toBeNull();
    expect(endingText(record({ ending: 'assessment', limit: null }))).toBe('Assessment');
  });

  it('leads to the run tree of its session, and says no critique', () => {
    const html = render(WorkRecordItem, {
      props: { projectId: 'p1', record: checked('confirmed, complete: ok') },
    }).body;
    expect(html).toContain('Check of the entry');
    expect(html).not.toContain('critique');
    expect(hrefs(html)).toEqual(['#/projects/p1/sessions/s1/tree']);
    const hostile = render(WorkRecordItem, {
      props: {
        projectId: 'p1',
        record: checked(`${HOSTILE}`, { author: { kind: 'agent', session_id: '../x' } }),
      },
    }).body;
    expect(hostileMarkup(hostile)).toBe(false);
    expect(hrefs(hostile)).toEqual([]);
  });
});

describe("a decision's fields", () => {
  const affects = (handle: string): EntryLink => ({
    kind: 'affects',
    target: 'entry',
    target_id: 'entry-0123456789abcdef01234599',
    revision: null,
    block: null,
    note: null,
    handle,
    session: null,
  });

  it('name the assessment, leading to the issue whose work records hold it', () => {
    const fields = { excluded_scope: 'Quantum ‮attacks', assessment: 'work-a' };
    const rows = fieldRows(fields, 'p1', [affects('C-1'), affects('I-4')]);
    expect(rows).toEqual([
      {
        key: 'excluded_scope',
        name: 'Excluded scope',
        value: 'Quantum ⟦U+202E⟧attacks',
        href: null,
      },
      {
        key: 'assessment',
        name: 'Assessment',
        value: 'work record work-a, on I-4',
        href: '#/projects/p1/ledger/I-4',
      },
    ]);
    expect(fieldRows({ assessment: 'work-a' }, 'p1')[0]).toMatchObject({
      value: 'work record work-a',
      href: null,
    });
  });
});
