// SPDX-License-Identifier: MIT
// A quote's citation checks, mounted: the entry view reads them for the revision shown
// when it holds a quote, and shows a failed read beside the quotes; a check's link opens
// its session with that review chosen.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LedgerEntryView from '../views/LedgerEntryView.svelte';
import ReviewsPanel from '../views/ReviewsPanel.svelte';
import type { EntryView, EvidenceLink, LedgerCitations } from './api-types';

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

function show<P extends Record<string, any>>(component: Component<P>, props: P): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const ID = 'entry-0123456789abcdef01234567';

const quote = (evidence: string): EvidenceLink => ({
  evidence_id: evidence,
  role: 'supports',
  type: 'source_claim',
  target: 'quote',
  target_id: 'entry-0123456789abcdef01234599',
  revision: 1,
  block: null,
  span: null,
  sha256: null,
  checked: null,
  scope: null,
  added: 1,
  handle: 'REF-1',
  locator: {
    source: 'eprint',
    dated: '2015-06-30T18:41:29Z',
    named: { eprint: '2015/625' },
    text: `The quoted passage of ${evidence}, long enough to count.`,
  },
});

const entry = (evidence: EvidenceLink[]): EntryView => ({
  entry_id: ID,
  handle: 'R-1',
  type: 'result',
  kind: null,
  session: null,
  standing: null,
  basis: null,
  anchors: [],
  origin: 'published',
  revision: 3,
  head: 3,
  title: 'A published bound',
  status: 'recorded',
  updated: '2026-10-15T00:00:00Z',
  asserted_only: false,
  open_flags: 0,
  statement: 'S',
  fields: {},
  links: [],
  evidence,
  incoming: [],
  incoming_total: 0,
  findings: [],
  flags: [],
  author: { kind: 'user', user: 'owner' },
  reason: 'r',
});

const citations: LedgerCitations = {
  project_id: 'p1',
  observed_at: 'now',
  citations: {
    entry_id: ID,
    handle: 'R-1',
    revision: 3,
    quotes: [
      {
        evidence_id: 'ev-1',
        total: 3,
        checks: [
          {
            check_id: 'citation-2',
            review_id: 'review-2',
            session: 's1',
            entry_revision: 3,
            verdict: 'supported',
            explanation: 'The passage gives the cost the result states.',
            created: '2026-10-15T01:00:00Z',
            source: 'eprint:2015/625',
            matches: true,
          },
          {
            check_id: 'citation-1',
            review_id: 'review-1',
            session: 's1',
            entry_revision: 2,
            verdict: 'unsupported',
            explanation: 'The cost differs.',
            created: '2026-10-15T00:00:00Z',
            source: 'eprint:2015/625',
            matches: false,
          },
        ],
      },
    ],
  },
};

/** The entry, its gate, and its citations (a status for a failed read). */
function server(shown: EntryView, read: LedgerCitations | number): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      urls.push(url);
      const reply = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), { status });
      if (url.includes('/citations')) {
        return typeof read === 'number'
          ? reply(
              { error: { kind: 'unavailable', message: 'the checks cannot be read now' } },
              read,
            )
          : reply(read);
      }
      // The work records, read with a result: none here.
      if (url.includes('/work?'))
        return reply({
          project_id: 'p1',
          observed_at: 'now',
          work: { entry_id: 'e', handle: 'X-1', records: [], next_before: null },
        });
      if (url.endsWith('/gate')) {
        return reply({
          project_id: 'p1',
          observed_at: 'now',
          gate: {
            entry_id: ID,
            handle: 'R-1',
            revision: 3,
            status: 'recorded',
            applies: false,
            holds: false,
            clauses: [],
            decisions: [],
          },
        });
      }
      return reply({ project_id: 'p1', observed_at: 'now', entry: shown });
    }),
  );
  return urls;
}

describe("the entry view's quotes", () => {
  it("read the revision's citation checks, and show each under its quote", async () => {
    const urls = server(entry([quote('ev-1'), quote('ev-2')]), citations);
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'R-1', revision: null });
    await settle();
    expect(urls.filter((url) => url.includes('/citations'))).toEqual([
      `/api/v0/projects/p1/ledger/${ID}/citations?revision=3`,
    ]);
    const items = [...root.querySelectorAll('ul.evidence > li')];
    expect(items[0]!.textContent).toContain('Supported');
    expect(items[0]!.textContent).toContain('Unsupported');
    expect(items[0]!.textContent).toContain("whose material is not the revision shown's");
    expect(items[0]!.textContent).toContain('And 1 older check, not listed.');
    expect(
      [...items[0]!.querySelectorAll('a')]
        .map((a) => a.getAttribute('href'))
        .filter((href) => href?.includes('/reviews/')),
    ).toEqual([
      '#/projects/p1/sessions/s1/reviews/review-2',
      '#/projects/p1/sessions/s1/reviews/review-1',
    ]);
    expect(items[1]!.textContent).toContain('No citation check of this quote yet.');
  });

  it('read no citation checks for a revision without quotes', async () => {
    const urls = server(entry([]), citations);
    show(LedgerEntryView, { projectId: 'p1', entryName: 'R-1', revision: null });
    await settle();
    expect(urls.some((url) => url.includes('/citations'))).toBe(false);
  });

  it('show a failed read beside the quotes, which stay shown', async () => {
    server(entry([quote('ev-1')]), 503);
    const root = show(LedgerEntryView, { projectId: 'p1', entryName: 'R-1', revision: null });
    await settle();
    expect(root.textContent).toContain("The quotes' citation checks could not be read");
    expect(root.textContent).toContain('the checks cannot be read now');
    expect(root.querySelector('pre.quote')!.textContent).toContain('The quoted passage of ev-1');
  });
});

describe('a link to a review', () => {
  it('opens the review panel with that review chosen', async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(url);
        const reply = (value: unknown) => new Response(JSON.stringify(value));
        if (url.includes('/reviews/review-2')) {
          return reply({
            review: {
              review_id: 'review-2',
              kind: 'checkpoint',
              state: 'passed',
              summary: 'The chosen review.',
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
            evidence: [],
            next_after: null,
          });
        }
        if (url.includes('/reviews'))
          return reply({ reviews: [], next_after: null, unreviewed: [] });
        if (url.includes('/math-checks')) return reply({ checks: [], next_after: null, queue: [] });
        return reply({ findings: [], next_after: null });
      }),
    );
    const root = show(ReviewsPanel, { projectId: 'p1', sessionId: 's1', reviewId: 'review-2' });
    await settle();
    expect(urls).toContain('/api/v0/projects/p1/sessions/s1/reviews/review-2?after=0');
    expect(root.querySelector('[aria-label="Review detail"]')!.textContent).toContain(
      'The chosen review.',
    );
  });
});
