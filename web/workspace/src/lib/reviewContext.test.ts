// SPDX-License-Identifier: MIT
// Context in a review's snapshot (CR-14 as amended for lab 40's B3): a document revision
// with its blocks, a delegation's outcome and an entry at a pinned revision, each marked as
// context pinned by an entry's evidence, not reviewed material, leading to its target
// where the workspace has a page; agents' text with every hidden character as its code
// point (hidden.fixtures.json).
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import PinnedEvidencePreview from '../views/PinnedEvidencePreview.svelte';
import ReviewDetail from '../views/ReviewDetail.svelte';
import { hostileMarkup } from './approvalTesting';
import fixtures from './hidden.fixtures.json';
import { emptyReviewPanel, ReviewBrowser } from './reviewBrowser';
import { contextLinks, contextTarget, contextText, outcomeOf, pinnedBy } from './reviewContext';
import type { Evidence, Review } from './reviews';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';
const BY = { entry: 'entry-0123456789abcdef01234567', handle: 'C-4', revision: 3 };

const item = (kind: string, locator: Record<string, unknown>, n = 1): Evidence => ({
  evidence: `ev-${n}`,
  ordinal: n,
  kind,
  locator: locator as Evidence['locator'],
  sha256: 'ab'.repeat(32),
  complete: true,
});

const documentItem = (fields: Record<string, unknown> = {}) =>
  item('document', {
    document: 'doc-1',
    revision: 2,
    sha256: 'cd'.repeat(32),
    blocks: ['b3', 'b5'],
    pinned_by: BY,
    ...fields,
  });

describe('context in a review snapshot', () => {
  it('names the entry revision that pins it, and says it is not reviewed material', () => {
    expect(pinnedBy(documentItem())).toEqual({ name: 'C-4', revision: 3 });
    expect(contextText(documentItem())).toBe(
      'Context pinned by the evidence of C-4 r3, not reviewed material.',
    );
    // Material under review names no pinning entry.
    expect(contextText(item('ledger', { entry: 'e', handle: 'C-4', revision: 3 }))).toBe('');
  });

  it('says what each kind of context is', () => {
    expect(contextTarget(documentItem())).toBe(
      'document doc-1 revision 2, the blocks its proofs cite: b3, b5',
    );
    expect(contextTarget(item('delegation', { delegation: 'del-1', pinned_by: BY }))).toBe(
      'the outcome of delegation del-1',
    );
    expect(
      contextTarget(item('entry', { entry: 'e', handle: 'A-2', revision: 1, pinned_by: BY })),
    ).toBe('ledger entry A-2 revision 1');
    expect(contextTarget(item('artifact', { artifact: 'a', pinned_by: BY }))).toBe('');
  });

  it('leads to each block, the entry pinned and the entry that pins it', () => {
    expect(contextLinks('p 1', documentItem())).toEqual([
      { text: 'block b3', href: '#/projects/p%201/documents/doc-1/revisions/2/blocks/b3' },
      { text: 'block b5', href: '#/projects/p%201/documents/doc-1/revisions/2/blocks/b5' },
      { text: 'pinned by C-4 r3', href: '#/projects/p%201/ledger/C-4/revisions/3' },
    ]);
    expect(
      contextLinks('p1', item('entry', { entry: 'e', handle: 'A-2', revision: 1, pinned_by: BY })),
    ).toEqual([
      { text: 'A-2 r1', href: '#/projects/p1/ledger/A-2/revisions/1' },
      { text: 'pinned by C-4 r3', href: '#/projects/p1/ledger/C-4/revisions/3' },
    ]);
    // Names the workspace would not take lead nowhere.
    expect(
      contextLinks(
        'p1',
        documentItem({ blocks: ['../x', HOSTILE], pinned_by: { handle: HOSTILE, revision: 3 } }),
      ),
    ).toEqual([]);
  });

  it("reads a delegation's outcome as the run tree's card shows a delegation", () => {
    const outcome = outcomeOf(
      JSON.stringify({
        delegation: 'del-1',
        kind: 'check',
        brief: { task: 'Check C-4.' },
        ended: 'returned',
        stopped: 'final',
        result: { verdict: 'refuted', status: 'complete', refutation: 'Step 3.' },
        read_entry: true,
        read_proofs: true,
        refused: null,
        alias: 'checker-b',
        models: ['m'],
        report: 'R',
        report_clipped: false,
      }),
    );
    expect(outcome).toMatchObject({
      delegation_id: 'del-1',
      ended: 'returned',
      result: { verdict: 'refuted' },
      alias: 'checker-b',
      read_entry: true,
    });
    expect(outcomeOf('{"cut')).toBeNull();
    expect(outcomeOf('{}')).toBeNull();
  });

  const review: Review = {
    review_id: 'review-1',
    kind: 'checkpoint',
    state: 'passed',
    summary: '',
    created: 1_792_000_000,
    alias: null,
    resolved_model: null,
    rubric: 'r',
    calls: 1,
    input_tokens: 1,
    output_tokens: 1,
    estimated_tokens: 1,
    cap_reached: false,
    coverage_complete: true,
    start_seq: 1,
    end_seq: 2,
    config_revision: 'c',
    classifier: {},
  };

  it('are shown in the item list as text, every link leading into the workspace', () => {
    const panel = emptyReviewPanel();
    panel.evidence.items = [
      documentItem({ document: HOSTILE, blocks: ['b3', HOSTILE] }),
      item('delegation', { delegation: HOSTILE, pinned_by: { ...BY, handle: HOSTILE } }, 2),
      item('entry', { entry: HOSTILE, handle: 'A-2', revision: 1, pinned_by: BY }, 3),
      item('ledger', { entry: 'e', handle: 'C-4', revision: 3 }, 4),
    ];
    const browser = new ReviewBrowser(panel, () => ({ project: 'p1', session: 's1' }));
    const html = render(ReviewDetail, {
      props: { panel, browser, review, busy: false, projectId: 'p1', sessionId: 's1' },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect([...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1])).toEqual([
      `#/projects/p1/documents/${encodeURIComponent(HOSTILE)}/revisions/2/blocks/b3`,
      '#/projects/p1/ledger/C-4/revisions/3',
      '#/projects/p1/ledger/A-2/revisions/1',
      '#/projects/p1/ledger/C-4/revisions/3',
    ]);
    expect(html.match(/not reviewed material/g)).toHaveLength(3);
  });

  const cases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(cases)(
    '$name, in a pinned text read: each hidden character as its code point',
    ({ text, hidden }) => {
      const html = render(PinnedEvidencePreview, { props: { text } }).body;
      for (const [, code] of hidden) expect(html).toContain(`⟦${code}⟧`);
    },
  );

  it.each(cases)(
    '$name, in a context target: each hidden character as its code point',
    ({ text, hidden }) => {
      const shown = contextTarget(item('delegation', { delegation: text, pinned_by: BY }));
      for (const [, code] of hidden) expect(shown).toContain(`⟦${code}⟧`);
    },
  );
});
