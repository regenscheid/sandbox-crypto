// SPDX-License-Identifier: MIT
// A review window's actions, mounted in the review panel: labelled in the item list, and
// read whole as a table for each run, a refused call marked, what was made linked.
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

const actions = {
  about: 'Each run…',
  actions: [
    {
      tool: 'document_patch',
      call: 'c1',
      target: { document: 'notes.md' },
      outcome: 'ok',
      made: ['document doc-1 r3'],
      run: 'run-a',
    },
    {
      tool: 'ledger_change',
      call: 'c2',
      target: { entry: 'C-4' },
      outcome: 'refused',
      made: [],
      run: 'run-a',
    },
  ],
  total: 2,
  shown: 2,
  documents: [
    {
      document: 'doc-1',
      revision: 3,
      since: 2,
      changed: ['b2'],
      changed_total: 1,
      removed: [],
      removed_total: 0,
    },
  ],
};

function server(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const reply = (value: unknown) => new Response(JSON.stringify(value));
      if (url.includes('/evidence/ev-actions')) {
        return reply({
          text: JSON.stringify(actions),
          next_start: null,
          locator: {},
          complete: true,
        });
      }
      if (url.includes('/reviews/review-1?')) {
        return reply({
          review: {
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
            end_seq: 9,
            config_revision: 'c',
            classifier: {},
          },
          evidence: [
            {
              evidence: 'ev-actions',
              ordinal: 1,
              kind: 'actions',
              locator: { actions: 'window', from: 1, to: 9 },
              sha256: 'ab'.repeat(32),
              complete: true,
            },
          ],
          next_after: null,
        });
      }
      if (url.includes('/reviews')) return reply({ reviews: [], next_after: null, unreviewed: [] });
      if (url.includes('/math-checks')) return reply({ checks: [], next_after: null, queue: [] });
      return reply({ findings: [], next_after: null });
    }),
  );
}

describe("a review window's actions", () => {
  it('are labelled in the list, and read whole as a table for each run', async () => {
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
    const row = detail.querySelector('li')!;
    expect(row.textContent).toContain("Derived from the window's tool records:");
    expect(row.textContent).toContain('What the runs did, feed entries 1 to 9');
    row.querySelector('button')!.click();
    await settle();
    const table = detail.querySelector('[aria-label="What the runs did"] table')!;
    const rows = [...table.querySelectorAll('tbody tr')];
    expect(rows.map((r) => r.querySelector('td:nth-child(2)')!.textContent)).toEqual([
      'document_patch',
      'ledger_change',
    ]);
    expect(rows[1]!.classList.contains('marked')).toBe(true);
    expect(rows[1]!.querySelector('strong.problem')!.textContent).toBe('refused');
    expect(rows[0]!.querySelector('a')!.getAttribute('href')).toBe(
      '#/projects/p1/documents/doc-1/revisions/3',
    );
    expect(
      [...detail.querySelectorAll('[aria-label="What the runs did"] a')].map((a) =>
        a.getAttribute('href'),
      ),
    ).toEqual([
      '#/projects/p1/sessions/s1/runs/run-a',
      '#/projects/p1/documents/doc-1/revisions/3',
      '#/projects/p1/documents/doc-1/revisions/3/blocks/b2',
    ]);
    expect(detail.querySelector('[aria-label="Pinned evidence preview"]')).toBeNull();
  });
});
