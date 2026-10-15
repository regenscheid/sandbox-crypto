// SPDX-License-Identifier: MIT
// Context in a review, mounted in the review panel: each item marked as context pinned by
// an entry's evidence, a delegation's outcome read whole shown as the run tree shows a
// delegation, and a document read as text with its hidden characters as code points.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ReviewsPanel from '../views/ReviewsPanel.svelte';

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

const BY = { entry: 'entry-0123456789abcdef01234567', handle: 'C-4', revision: 3 };

const items = [
  {
    evidence: 'ev-doc',
    ordinal: 1,
    kind: 'document',
    locator: {
      document: 'doc-1',
      revision: 2,
      sha256: 'cd'.repeat(32),
      blocks: ['b3'],
      pinned_by: BY,
    },
    sha256: 'ab'.repeat(32),
    complete: true,
  },
  {
    evidence: 'ev-entry',
    ordinal: 3,
    kind: 'entry',
    locator: { entry: 'entry-0123456789abcdef01234599', handle: 'A-2', revision: 1, pinned_by: BY },
    sha256: 'ab'.repeat(32),
    complete: true,
  },
  {
    evidence: 'ev-del',
    ordinal: 2,
    kind: 'delegation',
    locator: { delegation: 'del-1', pinned_by: BY },
    sha256: 'ab'.repeat(32),
    complete: true,
  },
];

const outcome = {
  delegation: 'del-1',
  kind: 'check',
  brief: { task: 'Check C-4 at revision 3.', note: 'Mind lemma 2.' },
  ended: 'returned',
  stopped: 'final',
  result: {
    verdict: 'refuted',
    status: 'complete',
    summary: 'Lemma 2 fails.',
    refutation: 'For p = 2 the bound is off by one.',
    coverage: { examined: ['lemma 2'], not_examined: [] },
  },
  read_entry: true,
  read_proofs: true,
  refused: null,
  alias: 'checker-b',
  models: ['model-x'],
  report: 'Refuted: see lemma 2.',
  report_clipped: false,
};

function server(): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      urls.push(url);
      const reply = (value: unknown) => new Response(JSON.stringify(value));
      if (url.includes('/evidence/ev-del')) {
        return reply({
          text: JSON.stringify(outcome),
          next_start: null,
          locator: {},
          complete: true,
        });
      }
      if (url.includes('/evidence/ev-entry')) {
        // An entry's text that happens to name a delegation is not a delegation's outcome.
        const text = JSON.stringify({ delegation: 'del-9', statement: 'An entry.' });
        return reply({ text, next_start: null, locator: {}, complete: true });
      }
      if (url.includes('/evidence/ev-doc')) {
        return reply({ text: 'Lemma 2.‮ Proof.', next_start: null, locator: {}, complete: true });
      }
      if (url.includes('/reviews/review-1?')) {
        return reply({
          review: {
            review_id: 'review-1',
            kind: 'checkpoint',
            state: 'findings',
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
          },
          evidence: items,
          next_after: null,
        });
      }
      if (url.includes('/reviews')) return reply({ reviews: [], next_after: null, unreviewed: [] });
      if (url.includes('/math-checks')) return reply({ checks: [], next_after: null, queue: [] });
      return reply({ findings: [], next_after: null });
    }),
  );
  return urls;
}

const read = (root: HTMLElement, index: number) =>
  [...root.querySelectorAll('[aria-label="Review detail"] li')][index]!.querySelector('button')!;

describe('context in a review', () => {
  it('is marked in the item list, and a delegation read whole shows its outcome', async () => {
    server();
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(ReviewsPanel, {
        target,
        props: { projectId: 'p1', sessionId: 's1', reviewId: 'review-1' },
      }),
    );
    await settle();
    const detail = target.querySelector('[aria-label="Review detail"]')!;
    const rows = [...detail.querySelectorAll('li')];
    expect(rows[0]!.textContent).toContain(
      'Context pinned by the evidence of C-4 r3, not reviewed material.',
    );
    expect(rows[0]!.textContent).toContain(
      'document doc-1 revision 2, the blocks its proofs cite: b3',
    );
    expect([...rows[0]!.querySelectorAll('a')].map((a) => a.getAttribute('href'))).toEqual([
      '#/projects/p1/documents/doc-1/revisions/2/blocks/b3',
      '#/projects/p1/ledger/C-4/revisions/3',
    ]);
    expect(rows[1]!.textContent).toContain('ledger entry A-2 revision 1');
    expect(rows[2]!.textContent).toContain('the outcome of delegation del-1');
    read(target, 1).click();
    await settle();
    expect(detail.querySelector('.delegation')).toBeNull();
    expect(detail.querySelector('[aria-label="Pinned evidence preview"]')!.textContent).toContain(
      'An entry.',
    );
    read(target, 2).click();
    await settle();
    const card = detail.querySelector('.delegation')!;
    expect(card.textContent).toContain('Verdict: refuted');
    expect(card.textContent).toContain('For p = 2 the bound is off by one.');
    expect(detail.querySelector('p.context')!.textContent!.trim()).toBe(
      'Context pinned by the evidence of C-4 r3, not reviewed material.',
    );
    read(target, 0).click();
    await settle();
    expect(detail.querySelector('.delegation')).toBeNull();
    expect(detail.querySelector('[aria-label="Pinned evidence preview"]')!.textContent).toBe(
      'Lemma 2.⟦U+202E⟧ Proof.',
    );
  });
});
