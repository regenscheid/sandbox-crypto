// SPDX-License-Identifier: MIT
// A quote as the entry view shows it (LG-8, CR-4): its reference at the revision pinned,
// where in the PDF or ePrint it is, the PDF in the viewer at its page, and the quoted text
// exactly, every hidden character as its code point (hidden.fixtures.json).
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import LedgerEvidence from '../views/LedgerEvidence.svelte';
import type { EntryView, EvidenceLink } from './api-types';
import { hostileMarkup } from './approvalTesting';
import fixtures from './hidden.fixtures.json';
import { quoteView } from './ledgerEvidence';
import { atPage } from './pdfAnchors';

const SHA = 'ab'.repeat(32);
const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const pdfQuote = (fields: Partial<EvidenceLink> = {}, locator: Record<string, unknown> = {}) =>
  ({
    evidence_id: 'ev-1',
    role: 'supports',
    type: 'source_claim',
    target: 'quote',
    target_id: 'entry-0123456789abcdef01234567',
    revision: 2,
    block: null,
    span: null,
    sha256: SHA,
    checked: null,
    scope: null,
    added: 1,
    handle: 'REF-1',
    locator: {
      source: 'pdf',
      page: 14,
      label: '12',
      first_line: 3,
      last_line: 5,
      text: 'The attack costs 2^143 operations in the core-SVP model.',
      named: { artifact_id: 'art-1', revision: 2 },
      ...locator,
    },
    ...fields,
  }) as EvidenceLink;

const eprintQuote = (locator: Record<string, unknown> = {}) =>
  pdfQuote(
    { sha256: null },
    {
      source: 'eprint',
      page: undefined,
      label: undefined,
      first_line: undefined,
      last_line: undefined,
      dated: '2015-06-30T18:41:29Z; 2015-06-30T20:25:17Z',
      named: { eprint: '2015/625' },
      ...locator,
    },
  );

const entry = (evidence: EvidenceLink[]): EntryView => ({
  entry_id: 'entry-0123456789abcdef01234599',
  handle: 'R-1',
  type: 'result',
  origin: 'published',
  revision: 1,
  head: 1,
  title: 'T',
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

const quoted = (html: string) =>
  [...html.matchAll(/<pre class="quote[^"]*"[^>]*>([\s\S]*?)<\/pre>/g)].map((m) =>
    m[1]!
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&amp;/g, '&'),
  );

describe('a quote', () => {
  it('of a PDF names its page, label and lines, and opens the PDF at that page', () => {
    expect(quoteView('p 1', pdfQuote())).toEqual({
      reference: { text: 'REF-1 r2', href: '#/projects/p%201/ledger/REF-1/revisions/2' },
      where: 'PDF page 14 (printed 12), lines 3–5, sha256 abababababab…',
      pdf: `#/projects/p%201/artifacts/art-1/revisions/2/${SHA}/pages/14`,
      page: 14,
      text: 'The attack costs 2^143 operations in the core-SVP model.',
    });
  });

  it('of an ePrint names the paper and the date stamp the e-print tools gave', () => {
    expect(quoteView('p1', eprintQuote())).toMatchObject({
      where: 'ePrint 2015/625, dated 2015-06-30T18:41:29Z; 2015-06-30T20:25:17Z',
      pdf: null,
    });
  });

  it('opens no PDF without a PDF revision, a page and a hash it can name', () => {
    expect(quoteView('p1', pdfQuote({}, { named: undefined }))!.pdf).toBeNull();
    expect(quoteView('p1', pdfQuote({}, { page: '14' }))!.pdf).toBeNull();
    expect(
      quoteView('p1', pdfQuote({}, { named: { artifact_id: 'a', revision: 0 } }))!.pdf,
    ).toBeNull();
    expect(quoteView('p1', pdfQuote({ sha256: 'javascript:alert(1)' }))!.pdf).toBeNull();
    expect(quoteView('p1', pdfQuote({ target: 'entry' }))).toBeNull();
  });

  it('shows its label, paper and date stamp with hidden characters as code points', () => {
    expect(quoteView('p1', pdfQuote({}, { label: '1‮2' }))!.where).toContain(
      '(printed 1⟦U+202E⟧2)',
    );
    expect(
      quoteView('p1', eprintQuote({ named: { eprint: '2015/​625' }, dated: 'x⁦y' }))!.where,
    ).toBe('ePrint 2015/⟦U+200B⟧625, dated x⟦U+2066⟧y');
  });

  it('is shown as text, every link leading into the workspace', () => {
    const html = render(LedgerEvidence, {
      props: {
        projectId: 'p1',
        entry: entry([
          pdfQuote(
            { handle: HOSTILE },
            { text: HOSTILE, label: HOSTILE, named: { artifact_id: HOSTILE, revision: 2 } },
          ),
          eprintQuote({ named: { eprint: HOSTILE }, dated: HOSTILE, text: `[a](javascript:x)` }),
        ]),
      },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]!);
    expect(hrefs.every((href) => href.startsWith('#/projects/p1/'))).toBe(true);
    expect(hrefs).toContain(
      `#/projects/p1/artifacts/${encodeURIComponent(HOSTILE)}/revisions/2/${SHA}/pages/14`,
    );
    expect(quoted(html)).toEqual([HOSTILE, '[a](javascript:x)']);
  });

  const cases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(cases)('$name, quoted: each hidden character as its code point', ({ text, hidden }) => {
    const html = render(LedgerEvidence, {
      props: { projectId: 'p1', entry: entry([pdfQuote({}, { text })]) },
    }).body;
    const [shown] = quoted(html);
    for (const [, code] of hidden) expect(shown).toContain(`⟦${code}⟧`);
    if (hidden.length === 0) expect(shown).toBe(text);
  });
});

describe('the viewer opened at a page', () => {
  it('puts the page in the fragment, beside the capability', () => {
    const url = 'http://127.0.0.2:8201/viewer/#c=abc.def&w=http%3A%2F%2F127.0.0.1%3A8200';
    expect(atPage(url, 14)).toBe(`${url}&p=14`);
    expect(atPage(url, null)).toBe(url);
    expect(atPage(url, 0)).toBe(url);
    expect(atPage('http://127.0.0.2:8201/viewer/', 3)).toBe('http://127.0.0.2:8201/viewer/');
  });
});
