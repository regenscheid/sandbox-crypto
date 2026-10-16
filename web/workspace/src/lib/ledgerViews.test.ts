// SPDX-License-Identifier: MIT
// The ledger's views: they render no text as HTML, every link they make stays in the
// workspace, the links to an entry are read a page at a time from an offset, and the
// entry route composes its query.
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import IssueRow from '../views/IssueRow.svelte';
import LedgerEntryBody from '../views/LedgerEntryBody.svelte';
import LedgerLinks from '../views/LedgerLinks.svelte';
import { api } from './api';
import type { EntryFinding, EntryFlag, EntryLink, EntryView, EvidenceLink } from './api-types';
import { incomingLinks, incomingState, linksByKind, uniqueLinks } from './ledger';

afterEach(() => vi.unstubAllGlobals());

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const link = (fields: Partial<EntryLink>): EntryLink => ({
  kind: 'concerns',
  target: 'entry',
  target_id: 'entry-0123456789abcdef01234567',
  revision: null,
  block: null,
  note: null,
  handle: null,
  session: null,
  ...fields,
});

const hostileLinks = [
  link({ target: 'block', target_id: 'javascript:alert(1)', revision: 2, block: HOSTILE }),
  link({ target: 'block', target_id: 'a/../../b?x#y', revision: 2, block: 'b1/../x' }),
  link({ handle: 'javascript:alert(1)' }),
  link({ handle: 'C-1/../../x', target_id: 'C-2' }),
  link({ handle: 'I-1', note: HOSTILE }),
  link({ target: 'finding', target_id: HOSTILE }),
  link({ target: 'finding', target_id: HOSTILE, session: HOSTILE }),
  link({ target: 'finding', target_id: 'finding-1', session: 'javascript:alert(1)' }),
  link({ kind: 'assumes', handle: 'A-1', revision: 3 }),
];

/**
 * Every href the HTML holds, and whether its markup (not its text, which shows `<script>`
 * escaped) has an element or an attribute of its own that could run code.
 */
function inspect(html: string): { hrefs: string[]; hostile: boolean } {
  const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((match) => match[1]!);
  // KaTeX keeps a formula's source in an attribute, escaped; markup there is text.
  const markup = html.replace(/data-math-source="[^"]*"/g, '');
  const hostile = [...markup.matchAll(/<([a-zA-Z][\w-]*)([^>]*)>/g)].some(
    ([, tag, attributes]) =>
      /^(script|img|iframe|object|embed|svg|style)$/i.test(tag!) ||
      /\son[a-z]+\s*=|(?:href|src)\s*=\s*"?\s*(?:javascript|data|vbscript):/i.test(attributes!),
  );
  return { hrefs, hostile };
}

/** An entry whose every field an agent could write is hostile. */
function hostileEntry(fields: Partial<EntryView> = {}): EntryView {
  return {
    entry_id: 'entry-0123456789abcdef01234567',
    handle: 'C-1',
    type: 'claim',
    kind: null,
    session: null,
    standing: null,
    origin: 'project',
    revision: 2,
    head: 2,
    title: HOSTILE,
    status: HOSTILE,
    updated: HOSTILE,
    statement: [
      HOSTILE,
      '[a](javascript:alert(1))',
      '[a](JaVaScRiPt:alert(1))',
      '[a](&#106;avascript:alert(1))',
      '[a](data:text/html;base64,PHNjcmlwdD4=)',
      '<javascript:alert(1)>',
      '$\\href{javascript:alert(1)}{x}$',
    ].join('\n\n'),
    fields: {
      [HOSTILE]: HOSTILE,
      kind: HOSTILE,
      conditions: { [HOSTILE]: HOSTILE },
      pdf: { artifact_id: HOSTILE, revision: 1, sha256: 'ab'.repeat(32) },
    },
    asserted_only: false,
    open_flags: 0,
    links: hostileLinks,
    evidence: [],
    incoming: [link({ handle: 'I-9', note: HOSTILE }), link({ handle: HOSTILE })],
    incoming_total: 450,
    findings: [],
    flags: [],
    author: { kind: 'agent', session_id: HOSTILE, run_id: HOSTILE, role: HOSTILE },
    reason: HOSTILE,
    ...fields,
  };
}

describe('the ledger views', () => {
  it('are inspected by a check that sees markup, not escaped text', () => {
    expect(inspect('<p>&lt;img src=x onerror=alert(1)&gt; javascript:x</p>').hostile).toBe(false);
    expect(inspect('<p><img src=x></p>').hostile).toBe(true);
    expect(inspect('<a href="javascript:alert(1)">x</a>').hostile).toBe(true);
    expect(inspect('<span onclick="x">x</span>').hostile).toBe(true);
  });

  it('render no text as HTML: the Markdown component is their only renderer', () => {
    const views = import.meta.glob(['../views/Ledger*.svelte', '../views/Issue*.svelte'], {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    const names = Object.keys(views).map((path) => path.replace('../views/', ''));
    expect(names).toEqual(
      expect.arrayContaining([
        'IssueList.svelte',
        'IssueRow.svelte',
        'LedgerBoard.svelte',
        'LedgerCheck.svelte',
        'LedgerCheckText.svelte',
        'LedgerColumn.svelte',
        'LedgerEntryBody.svelte',
        'LedgerEntryView.svelte',
        'LedgerEvidence.svelte',
        'LedgerFindings.svelte',
        'LedgerFlags.svelte',
        'LedgerGate.svelte',
        'LedgerGatePanel.svelte',
        'LedgerHistory.svelte',
        'LedgerIncoming.svelte',
        'LedgerLinks.svelte',
      ]),
    );
    for (const [path, source] of Object.entries(views)) {
      expect(String(source), path).not.toContain('{@html');
    }
  });

  it('show hostile link targets and notes as text, every link leading into the workspace', () => {
    const html = render(LedgerLinks, {
      props: { projectId: 'p"><x', groups: linksByKind(hostileLinks), direction: 'out' },
    }).body;
    const found = inspect(html);
    expect(found.hostile).toBe(false);
    expect(found.hrefs.length).toBeGreaterThan(0);
    for (const href of found.hrefs) expect(href.startsWith('#/projects/'), href).toBe(true);
    expect(html).toContain('&lt;script>');
  });

  it('show a hostile entry as text, its statement through Markdown', () => {
    const entry = hostileEntry();
    const html = render(LedgerEntryBody, { props: { projectId: 'p1', entry } }).body;
    const found = inspect(html);
    expect(found.hostile).toBe(false);
    for (const href of found.hrefs) expect(href.startsWith('#/projects/'), href).toBe(true);
    expect(html).toContain('Links to it');
    expect(html).toContain('(450)');
    expect(html).toContain('More links to it');
    // The PDF and the findings in their sessions lead somewhere, each encoded.
    expect(found.hrefs.some((href) => href.includes('/artifacts/'))).toBe(true);
    expect(found.hrefs.some((href) => href.includes('/findings/'))).toBe(true);
  });
});

describe('the issue list', () => {
  it('shows a hostile issue as text, its handle leading into the workspace', () => {
    const html = render(IssueRow, {
      props: {
        projectId: 'p1',
        issue: {
          entry_id: 'entry-0123456789abcdef01234567',
          handle: 'I-1',
          type: 'issue',
          kind: null,
          session: null,
          standing: null,
          origin: null,
          revision: 1,
          title: HOSTILE,
          status: 'open',
          asserted_only: null,
          open_flags: 0,
          updated: HOSTILE,
        },
      },
    }).body;
    const found = inspect(html);
    expect(found.hostile).toBe(false);
    expect(found.hrefs).toEqual(['#/projects/p1/ledger/I-1']);
  });
});

describe('the links to an entry', () => {
  const page = (from: number, count: number) =>
    Array.from({ length: count }, (_, i) =>
      link({ handle: `I-${from + i + 1}`, target_id: `e${from + i}` }),
    );

  it('start with the entry page, then read from the offset of the links read so far', async () => {
    const state = incomingState({ incoming: page(0, 200), incoming_total: 450 });
    expect(state.next).toBe(200);
    const read = vi.fn(async (after: number) => ({
      incoming: page(after, Math.min(200, 460 - after)),
      incoming_total: 460, // more were made since the first page
    }));
    const list = incomingLinks(state, read);
    await list.more();
    expect(read).toHaveBeenLastCalledWith(200);
    expect(state.items).toHaveLength(400);
    expect(state.total).toBe(460);
    expect(state.next).toBe(400);
    await list.more();
    expect(read).toHaveBeenLastCalledWith(400);
    expect(state.items).toHaveLength(460);
    expect(state.next).toBeNull();
    await list.more();
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('stop at an empty page, even short of the count', async () => {
    const state = incomingState({ incoming: page(0, 200), incoming_total: 450 });
    const list = incomingLinks(state, async () => ({ incoming: [], incoming_total: 180 }));
    await list.more();
    expect(state.next).toBeNull();
    expect(state.total).toBe(180);
    expect(state.items).toHaveLength(200);
  });

  it('keep the count and the links read when a page fails', async () => {
    const state = incomingState({ incoming: page(0, 200), incoming_total: 450 });
    const list = incomingLinks(state, async () => {
      throw new Error('no answer');
    });
    await list.more();
    expect(state.error).toBeInstanceOf(Error);
    expect(state.total).toBe(450);
    expect(state.next).toBe(200);
  });

  it('show each link once when pages overlap', () => {
    const first = page(0, 3);
    const overlap = [first[2]!, link({ ...first[2]!, revision: 2 }), ...page(3, 1)];
    const shown = uniqueLinks([...first, ...overlap]);
    expect(shown.map((l) => [l.target_id, l.revision])).toEqual([
      ['e0', null],
      ['e1', null],
      ['e2', null],
      ['e2', 2],
      ['e3', null],
    ]);
    expect(uniqueLinks([link({ kind: 'answers' }), link({ kind: 'concerns' })])).toHaveLength(2);
  });
});

describe('the entry route', () => {
  it('composes its revision and incoming_after query', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ entry: {} })));
    vi.stubGlobal('fetch', fetch);
    await api.ledgerEntry('p/1', 'C-1');
    await api.ledgerEntry('p/1', 'C-1', 3);
    await api.ledgerEntry('p/1', 'C-1', null, 200);
    await api.ledgerEntry('p/1', 'C-1', 3, 200);
    await api.ledgerEntry('p/1', 'entry-0123456789abcdef01234567', null, 0);
    expect(fetch.mock.calls.map((call) => (call as unknown[])[0])).toEqual([
      '/api/v0/projects/p%2F1/ledger/C-1',
      '/api/v0/projects/p%2F1/ledger/C-1?revision=3',
      '/api/v0/projects/p%2F1/ledger/C-1?incoming_after=200',
      '/api/v0/projects/p%2F1/ledger/C-1?revision=3&incoming_after=200',
      '/api/v0/projects/p%2F1/ledger/entry-0123456789abcdef01234567',
    ]);
  });
});

const evidence = (fields: Partial<EvidenceLink>): EvidenceLink => ({
  evidence_id: `ev-${Math.random()}`,
  role: 'supports',
  type: 'proof',
  target: 'entry',
  target_id: 'entry-0123456789abcdef01234567',
  revision: 2,
  block: null,
  span: null,
  sha256: null,
  checked: null,
  locator: null,
  scope: null,
  added: 1,
  handle: 'C-3',
  ...fields,
});

describe('the evidence and findings on an entry', () => {
  it('show hostile targets, scopes and findings as text, every link in the workspace', () => {
    const entry = hostileEntry({
      evidence: [
        evidence({ handle: HOSTILE, scope: HOSTILE }),
        evidence({ handle: 'javascript:alert(1)' }),
        evidence({ target: 'artifact', target_id: HOSTILE, sha256: HOSTILE }),
        evidence({ target: 'block', target_id: 'javascript:x', block: HOSTILE, span: HOSTILE }),
        evidence({ target: 'review', target_id: HOSTILE }),
        evidence({ role: 'independent_check', checked: 2, scope: '\u202eevil' }),
      ],
      findings: [
        {
          finding_id: HOSTILE,
          session: 'javascript:alert(1)',
          status: HOSTILE,
          severity: HOSTILE,
          category: HOSTILE,
          origin: HOSTILE,
          field: HOSTILE,
          revision: 2,
        } satisfies EntryFinding,
      ],
    });
    const html = render(LedgerEntryBody, { props: { projectId: 'p1', entry } }).body;
    const found = inspect(html);
    expect(found.hostile).toBe(false);
    for (const href of found.hrefs) expect(href.startsWith('#/projects/'), href).toBe(true);
    expect(html).toContain('Evidence');
    expect(html).toContain('Independent check, proof, of revision 2');
    expect(html).toContain('Review findings');
    expect(found.hrefs).toContain(
      '#/projects/p1/sessions/javascript%3Aalert(1)/findings/' + encodeURIComponent(HOSTILE),
    );
    expect(found.hrefs).toContain(
      '#/projects/p1/documents/javascript%3Ax/revisions/2/blocks/' + encodeURIComponent(HOSTILE),
    );
  });

  it('say "asserted only" for a claim or result without evidence, and nothing for other types', () => {
    const asserted = render(LedgerEntryBody, {
      props: { projectId: 'p1', entry: hostileEntry({ asserted_only: true }) },
    }).body;
    expect(asserted).toContain('Asserted only');
    const issue = render(LedgerEntryBody, {
      props: { projectId: 'p1', entry: hostileEntry({ type: 'issue', asserted_only: null }) },
    }).body;
    expect(issue).not.toContain('Evidence');
  });

  it('show the first evidence links, and offer the rest', () => {
    const many = Array.from({ length: 60 }, (_, n) =>
      evidence({ evidence_id: `ev-${n}`, handle: `C-${n + 1}` }),
    );
    const html = render(LedgerEntryBody, {
      props: { projectId: 'p1', entry: hostileEntry({ evidence: many }) },
    }).body;
    expect(html).toContain('C-50 r2');
    expect(html).not.toContain('C-51 r2');
    expect(html).toContain('Show all 60 evidence links');
  });
});

describe('the flags on an entry', () => {
  const flag = (fields: Partial<EntryFlag>): EntryFlag => ({
    flag_id: 'flag-1',
    cause: 'dependency_status',
    source: { entry: 'entry-0', handle: 'A-1', revision: 2, status: 'withdrawn' },
    via: null,
    revision: 3,
    raised: '2026-10-15T00:00:00Z',
    reviews: [],
    reopens: null,
    ...fields,
  });

  it('show hostile sources and flag IDs as text, every link in the workspace', () => {
    const entry = hostileEntry({
      open_flags: 120,
      flags: [
        flag({ flag_id: HOSTILE, source: { handle: HOSTILE, revision: 1 } }),
        flag({ flag_id: 'flag-2', source: { handle: 'javascript:alert(1)' }, via: HOSTILE }),
        flag({ flag_id: 'flag-3', cause: 'spread', reviews: [HOSTILE], reopens: HOSTILE }),
      ],
    });
    const html = render(LedgerEntryBody, { props: { projectId: 'p1', entry } }).body;
    const found = inspect(html);
    expect(found.hostile).toBe(false);
    for (const href of found.hrefs) expect(href.startsWith('#/projects/'), href).toBe(true);
    expect(html).toContain('120 open flags');
    expect(html).toContain('The newest 3 of the 120 open flags are listed');
    expect(html).toContain('Clear all 120 open flags');
    expect(html).toContain('Reason for clearing (required)');
  });

  it('show none and offer nothing when the entry has none', () => {
    const html = render(LedgerEntryBody, {
      props: { projectId: 'p1', entry: hostileEntry() },
    }).body;
    expect(html).not.toContain('Reason for clearing');
  });

  it('count on an issue row', () => {
    const html = render(IssueRow, {
      props: {
        projectId: 'p1',
        issue: {
          entry_id: 'entry-0123456789abcdef01234567',
          handle: 'I-1',
          type: 'issue',
          kind: null,
          session: null,
          standing: null,
          origin: null,
          revision: 1,
          title: 'Q',
          status: 'open',
          updated: '2026-10-15T00:00:00Z',
          asserted_only: null,
          open_flags: 3,
        },
      },
    }).body;
    expect(html).toContain('3 open flags');
  });
});
