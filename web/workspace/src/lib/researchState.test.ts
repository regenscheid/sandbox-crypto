// SPDX-License-Identifier: MIT
// The research state on the ledger (CM-10 to CM-12): an entry's and a work record's
// standing label with its scope and who settled it, an issue's kind, a session-scoped
// decision, a reopen condition and its flag, caveats, a reopening, and how far a
// reference's sources were read. Agents' text is shown as text, never as markup.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import LedgerColumn from '../views/LedgerColumn.svelte';
import ResearchState from '../views/ResearchState.svelte';
import StandingBadge from '../views/StandingBadge.svelte';
import WorkRecordItem from '../views/WorkRecordItem.svelte';
import type { EntryFlag, EntryView, SourceRead, Standing, WorkRecord } from './api-types';
import { hostileMarkup } from './approvalTesting';
import { fieldRows } from './ledger';
import {
  acceptedLabel,
  anchorView,
  coveredText,
  provenanceView,
  entryTags,
  readText,
  reopenedText,
  standingLine,
  standingView,
  STATE_FIELDS,
} from './researchState';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const impossible: Standing = {
  label: 'impossible',
  text: 'impossible, settled by the ledger',
  scope: 'memory below 2^64',
  assumes: ['A-2'],
  by: 'D-3',
};

const entry = (fields: Partial<EntryView> = {}): EntryView => ({
  entry_id: 'entry-0123456789abcdef01234567',
  pins: [],
  figure_checks: [],
  handle: 'I-1',
  type: 'issue',
  kind: 'hypothesis',
  session: null,
  standing: impossible,
  basis: null,
  anchors: [],
  origin: null,
  revision: 2,
  head: 2,
  title: 'A lattice attack',
  status: 'obstructed',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: null,
  open_flags: 1,
  statement: 'Does it work?',
  fields: {
    kind: 'hypothesis',
    reopen_condition: 'a cheaper sieve is published',
    caveats: ['only for n = 512', HOSTILE],
    excluded_scope: 'quantum',
  },
  links: [],
  evidence: [],
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [{ cause: 'reopen_condition' } as EntryFlag],
  author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
  reason: 'r',
  ...fields,
});

describe('a standing label', () => {
  it('says what it is, its scope, what it assumes and who settled it, leading there', () => {
    const view = standingView('p1', impossible)!;
    expect(view.word).toBe('Impossible');
    expect(view.by).toEqual({ text: 'D-3', href: '#/projects/p1/ledger/D-3' });
    expect(view.assumes).toEqual([{ text: 'A-2', href: '#/projects/p1/ledger/A-2' }]);
    expect(standingLine(view)).toBe(
      'Impossible: impossible, settled by the ledger; within memory below 2^64; settled by D-3',
    );
    const flagged = standingView('p1', { ...impossible, label: 'flagged', by: 'not a handle' })!;
    expect(flagged.by).toEqual({ text: 'not a handle', href: null });
    expect(standingLine(flagged)).toContain('; flagged from not a handle');
    expect(standingView('p1', null)).toBeNull();
  });

  it('shows in a list beside the entry, with its kind and session scope as tags', () => {
    expect(entryTags({ type: 'issue', kind: 'contradiction', session: null })).toEqual([
      'contradiction',
    ]);
    expect(entryTags({ type: 'decision', kind: 'exclude', session: 's1' })).toEqual([
      'for session s1 only',
    ]);
    const html = render(StandingBadge, {
      props: { projectId: 'p1', standing: impossible, tags: ['hypothesis'] },
    }).body;
    expect(html).toMatch(/class="standing impossible[ "]/);
    expect(html).toContain('>Impossible<');
    expect(html).toContain('title="Impossible: impossible, settled by the ledger;');
    expect(html).toContain('>hypothesis<');
    // The board's column renders it for each card (here, with no card loaded yet).
    const board = render(LedgerColumn, {
      props: { projectId: 'p1', type: 'issue', status: 'open', filter: '' },
    }).body;
    expect(hostileMarkup(board)).toBe(false);
  });
});

describe("an entry's research state", () => {
  it('shows its standing, kind, reopen condition with its flag, and caveats, as text', () => {
    const html = render(ResearchState, { props: { projectId: 'p1', entry: entry() } }).body;
    expect(html).toContain('<h2>Research state</h2>');
    expect(html).toContain('Within: <bdi>memory below 2^64</bdi>');
    expect(html).toContain('href="#/projects/p1/ledger/D-3"');
    expect(html).toContain('a cheaper sieve is published');
    expect(html).toContain(': its flag is open');
    expect(html).toContain('only for n = 512');
    expect(hostileMarkup(html)).toBe(false);
    // The type's other fields stay in its fields; these are not shown twice.
    const rows = fieldRows(entry().fields, 'p1', [], STATE_FIELDS).map((row) => row.key);
    expect(rows).toEqual(['excluded_scope']);
  });

  it('says a decision is meant for one session, and shows nothing for a plain entry', () => {
    const decision = entry({ type: 'decision', kind: null, session: 's1', standing: null });
    const html = render(ResearchState, {
      props: { projectId: 'p1', entry: { ...decision, fields: {}, flags: [] } },
    }).body;
    expect(html).toContain('For session <bdi>s1</bdi> only');
    const plain = entry({ kind: null, standing: null, fields: {}, flags: [] });
    expect(render(ResearchState, { props: { projectId: 'p1', entry: plain } }).body).not.toContain(
      'Research state',
    );
  });
});

describe('where a revision comes from', () => {
  const words = 'only attacks with memory below 2^64, please';
  const constraint = entry({
    type: 'decision',
    status: 'in_effect',
    kind: null,
    standing: null,
    fields: {},
    flags: [],
    basis: 'user_quoted',
    anchors: [
      { message: 'msg-0123456789abcdef', kind: 'user', quote: `${words} ${HOSTILE}` },
      { document: 'doc-1', revision: 3, block: 'b4' },
    ],
  });

  it('names each anchor, leading to a document block or an entry at its revision', () => {
    expect(anchorView('p1', { document: 'doc-1', revision: 3, block: 'b4' })).toEqual({
      text: 'document doc-1 r3, block b4',
      href: '#/projects/p1/documents/doc-1/revisions/3/blocks/b4',
      quote: null,
    });
    const at = anchorView('p1', { entry: 'entry-0123456789abcdef01234567', revision: 2 });
    expect(at.href).toBe('#/projects/p1/ledger/entry-0123456789abcdef01234567/revisions/2');
    expect(
      anchorView('p1', { message: 'msg-1', kind: 'tool_result', start: 0, end: 80 }).text,
    ).toBe('tool result msg-1, characters 0–80');
    expect(
      anchorView('p1', { artifact: 'artifact-1', revision: 2, sha256: 'ab'.repeat(32) }).text,
    ).toBe('artifact artifact-1 r2 (sha256 abababababab…)');
    expect(anchorView('p1', { memory: 'mem-3', revision: 2 })).toEqual({
      text: 'memory entry mem-3 r2',
      href: '#/projects/p1/memory',
      quote: null,
    });
    expect(provenanceView('p1', { basis: 'observed', anchors: [] }).basis).toBe(
      'observed: what a tool result, an artifact or a source shows',
    );
  });

  it("shows a standing constraint's basis, anchors and the user's words, as text", () => {
    expect(provenanceView('p1', constraint).constraint).toBe(true);
    expect(provenanceView('p1', { ...constraint, status: 'proposed' }).constraint).toBe(false);
    const html = render(ResearchState, { props: { projectId: 'p1', entry: constraint } }).body;
    expect(html).toContain('<dt>Standing constraint</dt>');
    expect(html).toContain(words);
    expect(html).toContain("the user's words, quoted by the agent");
    expect(html).toContain('href="#/projects/p1/documents/doc-1/revisions/3/blocks/b4"');
    expect(hostileMarkup(html)).toBe(false);
  });
});

describe("a fold's proposal the user accepted", () => {
  // CM-15: the ledger keeps the model's basis on the user's revision; the digest marks it.
  const accepted = entry({
    type: 'decision',
    status: 'in_effect',
    basis: 'observed',
    anchors: [{ message: 'msg-0123456789abcdef', kind: 'tool_result' }],
    author: { kind: 'user', user: 'user' },
  });

  it('is labelled accepted by the user, and is a standing constraint in effect', () => {
    const view = provenanceView('p1', accepted);
    expect(view.accepted && view.constraint).toBe(true);
    expect(view.basis).toBe(
      "[observed, accepted by the user]: written by a fold's model from folded messages",
    );
    expect(acceptedLabel(null)).toBe('[basis not recorded, accepted by the user]');
    // The user's own words are the user's, never "accepted"; an agent's are the agent's.
    expect(provenanceView('p1', { ...accepted, basis: 'user' }).accepted).toBe(false);
    expect(provenanceView('p1', { ...accepted, author: { kind: 'agent' } }).accepted).toBe(false);
    expect(provenanceView('p1', { ...accepted, status: 'proposed' }).constraint).toBe(false);
    const html = render(ResearchState, { props: { projectId: 'p1', entry: accepted } }).body;
    expect(html).toContain('<dt>Standing constraint</dt>');
    expect(html).toContain("written by a fold's model, accepted by the user");
    expect(html).toContain('[observed, accepted by the user]');
    expect(html).toContain('href="#/projects/p1/messages/msg-0123456789abcdef"');
    expect(hostileMarkup(html)).toBe(false);
  });
});

describe('how far a reference was read', () => {
  const read = (fields: Partial<SourceRead>): SourceRead => ({
    kind: 'pdf',
    source: 'papers/eagle.pdf',
    extent: 'partial',
    covered: null,
    versions: null,
    ...fields,
  });

  it('says whole, in part with its pages or ranges, abstract only or preview only', () => {
    expect(readText(read({ extent: 'whole' }))).toBe('read whole');
    expect(readText(read({ covered: { pages: '1-3, 7', of: 12 } }))).toBe(
      'read in part: pages 1-3, 7 of 12',
    );
    expect(coveredText({ ranges: [[0, 4096]], size: 10000 })).toBe('bytes 0–4,096 of 10,000');
    expect(coveredText({ chars: [[0, 600]], end: true })).toBe('characters 0–600, to its end');
    expect(readText(read({ kind: 'eprint', extent: 'abstract_only' }))).toBe('its abstract only');
    expect(readText(read({ extent: 'preview_only' }))).toBe('a preview only');
  });

  it("is listed in the reference's research state, each source as text", () => {
    const reference = entry({
      type: 'reference',
      kind: null,
      standing: null,
      fields: {},
      flags: [],
    });
    const sources = [read({ covered: { pages: '1-3', of: 12 } }), read({ source: HOSTILE })];
    const html = render(ResearchState, {
      props: { projectId: 'p1', entry: reference, read: sources },
    }).body;
    expect(html).toContain('<dt>Read</dt>');
    expect(html).toContain('read in part: pages 1-3 of 12');
    expect(hostileMarkup(html)).toBe(false);
  });
});

describe('a work record', () => {
  const record = (fields: Partial<WorkRecord>): WorkRecord => ({
    record_id: 'work-0123456789abcdef01234567',
    standing: null,
    decision: null,
    basis: null,
    anchors: [],
    position: 7,
    entry_id: 'entry-0123456789abcdef01234567',
    handle: 'I-1',
    entry_revision: 2,
    tried: 'BKZ-60.',
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

  it('shows its basis and anchors', () => {
    const anchored = record({
      basis: 'interpreted',
      anchors: [{ entry: 'D-3', revision: 1 }],
    });
    const html = render(WorkRecordItem, { props: { projectId: 'p1', record: anchored } }).body;
    expect(html).toContain("Basis: interpreted: the agent's own reading.");
    expect(html).toContain('href="#/projects/p1/ledger/D-3/revisions/1"');
  });

  it('shows its standing, so a failed attempt never reads as an impossibility', () => {
    const attempt: Standing = {
      label: 'attempt',
      text: 'an attempt',
      scope: null,
      assumes: [],
      by: null,
    };
    const html = render(WorkRecordItem, {
      props: { projectId: 'p1', record: record({ standing: attempt }) },
    }).body;
    expect(html).toMatch(/class="standing attempt[ "]/);
    expect(html).toContain('>Attempt<');
  });

  it("says a reopening is the agent's, with its reason and the decision it reversed", () => {
    const reopened = record({
      ending: 'reopened',
      limit: null,
      reason: `A cheaper sieve appeared. ${HOSTILE}`,
      decision: 'D-3',
    });
    expect(reopenedText(reopened)).toContain('Reopened by the agent: A cheaper sieve appeared.');
    expect(reopenedText(record({}))).toBeNull();
    const html = render(WorkRecordItem, { props: { projectId: 'p1', record: reopened } }).body;
    expect(html).toContain('Reopened by the agent: A cheaper sieve appeared.');
    expect(html).toContain('href="#/projects/p1/ledger/D-3"');
    expect(hostileMarkup(html)).toBe(false);
  });
});
