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
import type { EntryLink, EntryView } from './api-types';
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
        'LedgerColumn.svelte',
        'LedgerEntryBody.svelte',
        'LedgerEntryView.svelte',
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
    const entry: EntryView = {
      entry_id: 'entry-0123456789abcdef01234567',
      handle: 'C-1',
      type: 'claim',
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
      links: hostileLinks,
      evidence: [],
      incoming: [link({ handle: 'I-9', note: HOSTILE }), link({ handle: HOSTILE })],
      incoming_total: 450,
      author: { kind: 'agent', session_id: HOSTILE, run_id: HOSTILE, role: HOSTILE },
      reason: HOSTILE,
    };
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
          origin: null,
          revision: 1,
          title: HOSTILE,
          status: 'open',
          asserted_only: null,
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
