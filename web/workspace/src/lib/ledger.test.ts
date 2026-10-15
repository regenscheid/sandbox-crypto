// SPDX-License-Identifier: MIT
import { describe, expect, test } from 'vitest';
import type { EntryLink } from './api-types';
import fixture from './ledger.fixtures.json';
import {
  authorText,
  ENTRY_TYPES,
  FIELD_NAMES,
  fieldRows,
  filterText,
  LINK_WORDS,
  incomingPage,
  linksByKind,
  linkTarget,
  matches,
  provenanceText,
  STATUSES,
  statusText,
} from './ledger';

const link = (fields: Partial<EntryLink>): EntryLink => ({
  kind: 'assumes',
  target: 'entry',
  target_id: 'entry-0123456789abcdef01234567',
  revision: null,
  block: null,
  note: null,
  handle: null,
  session: null,
  ...fields,
});

describe('types and statuses', () => {
  test('are the seven types of LG-2, each with its statuses in order', () => {
    expect(ENTRY_TYPES).toEqual([
      'issue',
      'claim',
      'result',
      'assumption',
      'decision',
      'task',
      'reference',
    ]);
    expect(STATUSES.issue).toEqual(['open', 'resolved', 'obstructed', 'split', 'withdrawn']);
    expect(STATUSES.result).toEqual(STATUSES.claim);
    expect(STATUSES.claim[0]).toBe('proposed');
    expect(STATUSES.decision).toContain('in_effect');
    expect(statusText('in_effect')).toBe('in effect');
    expect(statusText('in_progress')).toBe('in progress');
  });
});

test('the filter keeps entries with every word in their handle or title', () => {
  const entry = { handle: 'C-12', title: 'Fermat for every prime' };
  expect(matches(entry, '')).toBe(true);
  expect(matches(entry, 'c-12')).toBe(true);
  expect(matches(entry, '  PRIME  fermat ')).toBe(true);
  expect(matches(entry, 'prime composite')).toBe(false);
});

describe('a link', () => {
  test('to an entry leads to it, at the revision it pins', () => {
    expect(linkTarget('p 1', link({ handle: 'A-1', revision: 3 }))).toEqual({
      text: 'A-1 r3',
      href: '#/projects/p%201/ledger/A-1/revisions/3',
    });
    expect(linkTarget('p1', link({ kind: 'builds_on', handle: 'C-2' }))).toEqual({
      text: 'C-2',
      href: '#/projects/p1/ledger/C-2',
    });
  });

  test('toward the entry leads to the entry that holds it, as it is now', () => {
    expect(linkTarget('p1', link({ handle: 'C-2', revision: 3 }), 'in')).toEqual({
      text: 'C-2, pinning r3',
      href: '#/projects/p1/ledger/C-2',
    });
  });

  test('to an entry without its handle leads by ID; a name of neither kind goes nowhere', () => {
    expect(linkTarget('p1', link({})).href).toBe(
      '#/projects/p1/ledger/entry-0123456789abcdef01234567',
    );
    expect(linkTarget('p1', link({ target_id: '../../elsewhere' }))).toEqual({
      text: '../../elsewhere',
      href: null,
    });
  });

  test('to a document block leads to that block at its revision', () => {
    const block = link({
      kind: 'concerns',
      target: 'block',
      target_id: 'doc-1',
      revision: 2,
      block: 'b4',
    });
    expect(linkTarget('p1', block)).toEqual({
      text: 'doc-1 r2, block b4',
      href: '#/projects/p1/documents/doc-1/revisions/2/blocks/b4',
    });
  });

  test('to a review finding names it', () => {
    const finding = link({ kind: 'concerns', target: 'finding', target_id: 'finding-9' });
    expect(linkTarget('p1', finding)).toEqual({ text: 'review finding finding-9', href: null });
  });

  test('groups follow the order of the kinds, each in stored order', () => {
    const links = [
      link({ kind: 'builds_on', handle: 'C-1' }),
      link({ kind: 'answers', handle: 'I-1' }),
      link({ kind: 'builds_on', handle: 'C-3' }),
    ];
    expect(linksByKind(links).map(([kind, found]) => [kind, found.map((l) => l.handle)])).toEqual([
      ['answers', ['I-1']],
      ['builds_on', ['C-1', 'C-3']],
    ]);
  });
});

describe('fields', () => {
  test("show a result's kind, category, conditions and measures as text", () => {
    expect(
      fieldRows({
        measures: { time: 120, memory: '2^{0.29n}' },
        kind: 'negative_result',
        conditions: { model: 'classical', success_at: '1/2' },
        category: 'key-recovery',
      }),
    ).toEqual([
      { key: 'kind', name: 'Kind', value: 'negative result' },
      { key: 'category', name: 'Frontier category', value: 'key-recovery' },
      { key: 'conditions', name: 'Conditions', value: 'model: classical; success_at: 1/2' },
      { key: 'measures', name: 'Measures', value: 'time: 120; memory: 2^{0.29n}' },
    ]);
  });

  test("show a reference's citation, and its PDF by artifact and revision", () => {
    expect(
      fieldRows({
        pdf: { artifact_id: 'artifact-1', revision: 2 },
        authors: ['A. Author', 'B. Author'],
        identifiers: { eprint: '2024/123' },
        retrieved: '2026-10-14',
      }),
    ).toEqual([
      { key: 'identifiers', name: 'Identifiers', value: 'eprint: 2024/123' },
      { key: 'authors', name: 'Authors', value: 'A. Author; B. Author' },
      { key: 'retrieved', name: 'Retrieved', value: '2026-10-14' },
      { key: 'pdf', name: 'PDF', value: 'artifact artifact-1 r2' },
    ]);
  });

  test('a field the view does not know yet still shows, under its own name', () => {
    expect(fieldRows({ new_field: 'x', kind: 'lemma' })).toEqual([
      { key: 'kind', name: 'Kind', value: 'lemma' },
      { key: 'new_field', name: 'new field', value: 'x' },
    ]);
    expect(fieldRows({})).toEqual([]);
  });
});

test('who wrote a revision reads as the documents say it; anything else as stored', () => {
  expect(authorText({ kind: 'user', user: 'owner' })).toBe('the user');
  expect(
    authorText({ kind: 'agent', session_id: 's1', run_id: 'run-2', role: 'delegated', model: 'm' }),
  ).toBe('delegated agent run run-2 of session s1, m');
  expect(authorText({ kind: 'robot' })).toBe('{"kind":"robot"}');
  expect(authorText({ kind: 'user', user: { nested: true } })).toBe(
    '{"kind":"user","user":{"nested":true}}',
  );
});

describe('the links to an entry, a page at a time', () => {
  const links = (count: number) =>
    Array.from({ length: count }, (_, i) => link({ handle: `I-${i + 1}` }));

  test('start with the page the entry came with, and continue after the links read', () => {
    expect(incomingPage({ incoming: links(200), incoming_total: 450 }, 0).next).toBe(200);
    expect(incomingPage({ incoming: links(200), incoming_total: 450 }, 200).next).toBe(400);
    expect(incomingPage({ incoming: links(50), incoming_total: 450 }, 400).next).toBeNull();
  });

  test('end with no links, or when the count is reached', () => {
    expect(incomingPage({ incoming: [], incoming_total: 0 }, 0)).toEqual({ items: [], next: null });
    expect(incomingPage({ incoming: links(3), incoming_total: 3 }, 0).next).toBeNull();
    // Links removed since the count: an empty page ends the list.
    expect(incomingPage({ incoming: [], incoming_total: 450 }, 400).next).toBeNull();
  });
});

test("a field's row is keyed by its name as stored, not its name in words", () => {
  // Two stored names that read the same in words stay two rows.
  const rows = fieldRows({ foo_bar: 1, 'foo bar': 2 });
  expect(rows.map((row) => row.name)).toEqual(['foo bar', 'foo bar']);
  expect(new Set(rows.map((row) => row.key)).size).toBe(2);
});

test("a column's filter says it searched only the entries read so far", () => {
  expect(filterText(0, 50, true)).toBe(
    '0 of 50 loaded match; more may match among those not read yet',
  );
  expect(filterText(3, 12, false)).toBe('3 of 12 loaded match');
});

describe("the views' tables agree with the ledger's models (ledger.fixtures.json)", () => {
  test('the types and their statuses, in order', () => {
    expect(ENTRY_TYPES).toEqual(Object.keys(fixture.statuses));
    expect(STATUSES).toEqual(fixture.statuses);
  });

  test('the link kinds, in order', () => {
    expect(Object.keys(LINK_WORDS)).toEqual(fixture.link_kinds);
  });

  test('the names of the fields the types give', () => {
    expect(Object.keys(FIELD_NAMES).sort()).toEqual(fixture.fields);
  });
});

test('an imported revision names its results file and line', () => {
  expect(provenanceText(null)).toBe('');
  expect(
    provenanceText({ file: 'shared/results/ff.jsonl', sha256: 'ab'.repeat(32), line: 3 }),
  ).toBe('imported from results file shared/results/ff.jsonl, line 3 (sha256 abababababab…)');
  expect(provenanceText({ file: 'x.jsonl', line: 1 })).toBe(
    'imported from results file x.jsonl, line 1',
  );
  expect(provenanceText({ source: 'pack' })).toBe('imported: {"source":"pack"}');
});
