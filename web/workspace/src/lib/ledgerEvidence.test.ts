// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { EntryFinding, EvidenceLink } from './api-types';
import {
  evidenceKind,
  evidenceState,
  evidenceTarget,
  findingLink,
  findingText,
} from './ledgerEvidence';

const SHA = 'ab'.repeat(32);
const link = (fields: Partial<EvidenceLink>): EvidenceLink => ({
  evidence_id: 'ev-1',
  role: 'supports',
  type: 'exact_computation',
  target: 'entry',
  target_id: 'entry-0123456789abcdef01234567',
  revision: 3,
  block: null,
  span: null,
  sha256: null,
  checked: null,
  locator: null,
  scope: null,
  added: 2,
  handle: 'C-4',
  ...fields,
});

describe('an evidence link', () => {
  it('to an entry leads to it at the revision it pins', () => {
    expect(evidenceTarget('p 1', link({}))).toEqual({
      text: 'C-4 r3',
      href: '#/projects/p%201/ledger/C-4/revisions/3',
    });
    expect(evidenceTarget('p1', link({ handle: null })).href).toBe(
      '#/projects/p1/ledger/entry-0123456789abcdef01234567/revisions/3',
    );
    expect(evidenceTarget('p1', link({ handle: 'C-4/../x' }))).toEqual({
      text: 'C-4/../x',
      href: null,
    });
  });

  it('to a document block or math span leads to the block at its revision', () => {
    const block = link({ target: 'block', target_id: 'doc-1', block: 'b7', span: 'm2' });
    expect(evidenceTarget('p1', block)).toEqual({
      text: 'doc-1 r3, block b7, math span m2',
      href: '#/projects/p1/documents/doc-1/revisions/3/blocks/b7',
    });
    expect(evidenceTarget('p1', { ...block, block: null }).href).toBeNull();
  });

  it('to an artifact revision or a review is named, with the start of its hash', () => {
    expect(
      evidenceTarget('p1', link({ target: 'artifact', target_id: 'artifact-9', sha256: SHA })),
    ).toEqual({
      text: 'artifact artifact-9 r3 (sha256 abababababab…)',
      href: null,
    });
    expect(
      evidenceTarget('p1', link({ target: 'review', target_id: 'rev-1', revision: null })),
    ).toEqual({
      text: 'review rev-1',
      href: null,
    });
  });

  it('to a quote leads to the reference, with where the quote is in its source', () => {
    const pdf = link({
      target: 'quote',
      target_id: 'entry-1',
      handle: 'REF-1',
      revision: 2,
      sha256: SHA,
      locator: { source: 'pdf', page: 3, label: '2', first_line: 30, last_line: 31, text: 't' },
    });
    expect(evidenceTarget('p1', pdf)).toEqual({
      text: 'quote of REF-1 r2, PDF page 3 (printed 2), lines 30–31',
      href: '#/projects/p1/ledger/REF-1/revisions/2',
    });
    const eprint = {
      ...pdf,
      sha256: null,
      locator: { source: 'eprint', dated: '2015-06-30', named: { eprint: '2015/625' }, text: 't' },
    };
    expect(evidenceTarget('p1', eprint).text).toBe(
      'quote of REF-1 r2, ePrint 2015/625 dated 2015-06-30',
    );
  });

  it('reads its role and type, and the revision a check examined', () => {
    expect(evidenceKind(link({}))).toBe('Supports, exact computation');
    expect(evidenceKind(link({ role: 'independent_check', type: 'proof', checked: 5 }))).toBe(
      'Independent check, proof, of revision 5',
    );
  });
});

it('an entry holds evidence, is asserted only, or is of a type that holds none', () => {
  expect(evidenceState({ asserted_only: false, evidence: [link({})] })).toBe('held');
  expect(evidenceState({ asserted_only: true, evidence: [] })).toBe('asserted');
  expect(evidenceState({ asserted_only: null, evidence: [] })).toBe('none');
});

describe('a finding on an entry', () => {
  const finding: EntryFinding = {
    finding_id: 'finding-1',
    session: 's/1',
    status: 'claimed_fixed',
    severity: 'fail',
    category: 'unsupported_claim',
    origin: 'review',
    field: 'statement',
    revision: 4,
  };

  it('reads its severity, category, status, field and revision', () => {
    expect(findingText(finding)).toBe(
      'fail · unsupported claim · claimed fixed, on its statement at r4',
    );
    expect(findingText({ ...finding, field: null, revision: null })).toBe(
      'fail · unsupported claim · claimed fixed',
    );
  });

  it("names a math finding's revision as the statement's text revision", () => {
    expect(findingText({ ...finding, category: 'math' })).toBe(
      "fail · math · claimed fixed, on its statement at the statement's text revision 4",
    );
  });

  it('leads to the finding in its session', () => {
    expect(findingLink('p1', finding)).toBe('#/projects/p1/sessions/s%2F1/findings/finding-1');
  });
});
