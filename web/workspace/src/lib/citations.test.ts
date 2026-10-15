// SPDX-License-Identifier: MIT
// A quote's citation checks as the entry view shows them (CR-4): verdicts, the review each
// leads to, the revision whose material it covered, and the reviewer's explanation as
// text, every hidden character as its code point (hidden.fixtures.json).
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import QuoteCitations from '../views/QuoteCitations.svelte';
import type { CitationCheckView, QuoteChecks } from './api-types';
import { hostileMarkup } from './approvalTesting';
import { checkText, olderChecksText, quoteChecks } from './citations';
import fixtures from './hidden.fixtures.json';
import { parseRoute, reviewHref } from './router';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const check = (fields: Partial<CitationCheckView> = {}): CitationCheckView => ({
  check_id: 'citation-1',
  review_id: 'review-1',
  session: 's1',
  entry_revision: 3,
  verdict: 'supported',
  explanation: 'The quote states the cost the result gives.',
  created: '2026-10-15T00:00:00Z',
  source: 'pdf:abababab',
  matches: true,
  ...fields,
});

const quote = (checks: CitationCheckView[], total = checks.length): QuoteChecks => ({
  evidence_id: 'ev-1',
  checks,
  total,
});

const shown = (value: QuoteChecks | null, loading = false) =>
  render(QuoteCitations, { props: { projectId: 'p1', quote: value, loading } }).body;

describe("a quote's citation checks", () => {
  it('say which revision each covered, and when that is not the material shown', () => {
    expect(checkText(check())).toMatch(/^checked r3, the material shown, .*; read pdf:abababab$/);
    expect(checkText(check({ matches: false, entry_revision: 2 }))).toMatch(
      /^checked r2, whose material is not the revision shown's, /,
    );
    expect(checkText(check({ source: 'eprint:2015/‮625' }))).toMatch(
      /read eprint:2015\/⟦U\+202E⟧625$/,
    );
  });

  it('are found by the evidence ID of their quote, with how many older ones are left out', () => {
    const read = { entry_id: 'e', handle: 'R-1', revision: 3, quotes: [quote([check()], 25)] };
    expect(quoteChecks(read, 'ev-1')!.total).toBe(25);
    expect(quoteChecks(read, 'ev-2')).toBeNull();
    expect(quoteChecks(null, 'ev-1')).toBeNull();
    expect(olderChecksText(quote([check()], 25))).toBe('And 24 older checks, not listed.');
    expect(olderChecksText(quote([check()], 2))).toBe('And 1 older check, not listed.');
    expect(olderChecksText(quote([check()]))).toBe('');
  });

  it('show each verdict, leading to its review in its session', () => {
    const html = shown(
      quote([
        check({ verdict: 'unsupported', check_id: 'c2', review_id: 'review-2' }),
        check({ verdict: 'inconclusive', check_id: 'c3', matches: false }),
      ]),
    );
    expect(html).toContain('Unsupported');
    expect(html).toContain('Inconclusive');
    expect([...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1])).toEqual([
      '#/projects/p1/sessions/s1/reviews/review-2',
      '#/projects/p1/sessions/s1/reviews/review-1',
    ]);
  });

  it('say when there is none, and nothing before the read arrives', () => {
    expect(shown(quote([]))).toContain('No citation check of this quote yet.');
    expect(shown(null, true)).toContain('Reading its citation checks');
    expect(shown(null, false)).not.toContain('citation check');
  });

  it('show hostile checks as text, every link leading into the workspace', () => {
    const html = shown(
      quote([
        check({
          review_id: HOSTILE,
          session: '../../evil',
          explanation: `${HOSTILE}\n\n[a](javascript:alert(1))`,
          source: HOSTILE,
          verdict: HOSTILE as CitationCheckView['verdict'],
        }),
      ]),
    );
    expect(hostileMarkup(html)).toBe(false);
    const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]!);
    expect(hrefs).toEqual([
      `#/projects/p1/sessions/..%2F..%2Fevil/reviews/${encodeURIComponent(HOSTILE)}`,
    ]);
  });

  const cases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(cases)(
    '$name, in its explanation: each hidden character as its code point',
    ({ text, hidden }) => {
      const html = shown(quote([check({ explanation: text })]));
      for (const [, code] of hidden) expect(html).toContain(`⟦${code}⟧`);
    },
  );

  it('render no text as HTML', () => {
    const views = import.meta.glob('../views/QuoteCitations.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });
});

describe('a link to a review', () => {
  it('opens its session with the review chosen', () => {
    expect(parseRoute(reviewHref('p 1', 's/1', 'review#2'))).toEqual({
      view: 'review',
      project: 'p 1',
      session: 's/1',
      review: 'review#2',
    });
    expect(parseRoute('#/projects/p1/sessions/s1/reviews/')).toEqual({ view: 'missing' });
    expect(parseRoute('#/projects/p1/sessions/s1/reviews/%E0%A4%A')).toEqual({ view: 'missing' });
  });
});
