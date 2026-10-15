// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import {
  categoryHref,
  documentHref,
  findingHref,
  frontierHref,
  historyHref,
  issuesHref,
  ledgerEntryHref,
  ledgerHref,
  memoryHref,
  parseRoute,
  pdfHref,
  projectHref,
  sessionHref,
} from './router';

const SHA = 'ab'.repeat(32);

describe('routes', () => {
  it.each([
    ['', { view: 'projects' }],
    ['#', { view: 'projects' }],
    ['#/', { view: 'projects' }],
    ['#/projects/p1', { view: 'project', project: 'p1' }],
    ['#/projects/p1/', { view: 'project', project: 'p1' }],
    ['#/projects/p1/sessions/a', { view: 'session', project: 'p1', session: 'a' }],
    [
      '#/projects/p1/sessions/a/findings/finding-9',
      { view: 'finding', project: 'p1', session: 'a', finding: 'finding-9' },
    ],
    ['#/projects/p1/sessions/a/findings', { view: 'missing' }],
    ['#/projects/p1/sessions/a/findings/', { view: 'missing' }],
    ['#/projects/p1/sessions/a/runs/r1', { view: 'run', project: 'p1', session: 'a', run: 'r1' }],
    ['#/projects/p1/sessions/a/runs/r1/events', { view: 'missing' }],
    ['#/projects/p1/sessions/a/tree', { view: 'tree', project: 'p1', session: 'a' }],
    ['#/projects/p1/sessions/a/tree/x', { view: 'missing' }],
    ['#/projects/p%201', { view: 'project', project: 'p 1' }],
    ['#/projects/%E0%A4%A', { view: 'missing' }],
    ['#/projects', { view: 'missing' }],
    ['#/projects//sessions/a', { view: 'missing' }],
    ['#/projects/p1/runs/a', { view: 'missing' }],
    ['#/elsewhere', { view: 'missing' }],
    [
      '#/projects/p1/documents/doc-1',
      { view: 'document', project: 'p1', document: 'doc-1', revision: null, block: null },
    ],
    [
      '#/projects/p1/documents/doc-1/revisions/3',
      { view: 'document', project: 'p1', document: 'doc-1', revision: 3, block: null },
    ],
    [
      '#/projects/p1/documents/doc-1/revisions/3/blocks/b12',
      { view: 'document', project: 'p1', document: 'doc-1', revision: 3, block: 'b12' },
    ],
    ['#/projects/p1/documents/doc-1/revisions/3/blocks/x', { view: 'missing' }],
    ['#/projects/p1/documents/doc-1/revisions/3/blocks', { view: 'missing' }],
    ['#/projects/p1/documents/doc-1/revisions/3/other/b1', { view: 'missing' }],
    [
      '#/projects/p1/documents/doc-1/history',
      { view: 'history', project: 'p1', document: 'doc-1' },
    ],
    ['#/projects/p1/documents/doc-1/revisions/0', { view: 'missing' }],
    ['#/projects/p1/documents/doc-1/revisions/x', { view: 'missing' }],
    ['#/projects/p1/documents/doc-1/revisions', { view: 'missing' }],
    ['#/projects/p1/documents', { view: 'missing' }],
    ['#/projects/p1/documents/doc-1/elsewhere', { view: 'missing' }],
    [
      `#/projects/p1/artifacts/art-1/revisions/2/${SHA}`,
      { view: 'pdf', project: 'p1', artifact: 'art-1', revision: 2, sha256: SHA, page: null },
    ],
    [
      `#/projects/p1/artifacts/art-1/revisions/2/${SHA}/pages/14`,
      { view: 'pdf', project: 'p1', artifact: 'art-1', revision: 2, sha256: SHA, page: 14 },
    ],
    [`#/projects/p1/artifacts/art-1/revisions/2/${SHA}/pages/0`, { view: 'missing' }],
    [`#/projects/p1/artifacts/art-1/revisions/2/${SHA}/pages`, { view: 'missing' }],
    [`#/projects/p1/artifacts/art-1/revisions/2/${SHA}/lines/3`, { view: 'missing' }],
    [`#/projects/p1/artifacts/art-1/revisions/0/${SHA}`, { view: 'missing' }],
    [`#/projects/p1/artifacts/art-1/revisions/2/${SHA.toUpperCase()}`, { view: 'missing' }],
    ['#/projects/p1/artifacts/art-1/revisions/2/abc', { view: 'missing' }],
    ['#/projects/p1/artifacts/art-1/revisions/2', { view: 'missing' }],
    ['#/projects/p1/memory', { view: 'memory', project: 'p1' }],
    ['#/projects/p1/memory/', { view: 'memory', project: 'p1' }],
    ['#/projects/p1/memory/mem-1', { view: 'missing' }],
    ['#/projects/p1/ledger', { view: 'ledger', project: 'p1' }],
    ['#/projects/p1/ledger/', { view: 'ledger', project: 'p1' }],
    ['#/projects/p1/ledger/issues', { view: 'issues', project: 'p1' }],
    ['#/projects/p1/ledger/issues/2', { view: 'missing' }],
    ['#/projects/p1/ledger/C-12', { view: 'entry', project: 'p1', entry: 'C-12', revision: null }],
    [
      '#/projects/p1/ledger/REF-3/revisions/2',
      { view: 'entry', project: 'p1', entry: 'REF-3', revision: 2 },
    ],
    [
      '#/projects/p1/ledger/entry-0123456789abcdef01234567',
      { view: 'entry', project: 'p1', entry: 'entry-0123456789abcdef01234567', revision: null },
    ],
    ['#/projects/p1/ledger/C-0', { view: 'missing' }],
    ['#/projects/p1/ledger/X-1', { view: 'missing' }],
    ['#/projects/p1/ledger/C-1/revisions/0', { view: 'missing' }],
    ['#/projects/p1/ledger/C-1/revisions', { view: 'missing' }],
    ['#/projects/p1/ledger/C-1/history', { view: 'missing' }],
    ['#/projects/p1/frontier', { view: 'categories', project: 'p1' }],
    [
      '#/projects/p1/frontier/key-recovery_2.v1',
      { view: 'frontier', project: 'p1', category: 'key-recovery_2.v1' },
    ],
    ['#/projects/p1/frontier/-x', { view: 'missing' }],
    ['#/projects/p1/frontier/a%2Fb', { view: 'missing' }],
    ['#/projects/p1/frontier/a/history', { view: 'missing' }],
  ])('%s', (hash, route) => {
    expect(parseRoute(hash)).toEqual(route);
  });

  it('round-trips the links it makes', () => {
    expect(parseRoute(projectHref('a/b?c'))).toEqual({ view: 'project', project: 'a/b?c' });
    expect(parseRoute(sessionHref('p1', 's#1'))).toEqual({
      view: 'session',
      project: 'p1',
      session: 's#1',
    });
    expect(parseRoute(documentHref('p1', 'notes.md', 2))).toEqual({
      view: 'document',
      project: 'p1',
      document: 'notes.md',
      revision: 2,
      block: null,
    });
    expect(parseRoute(documentHref('p1', 'doc-1', 4, 'b3'))).toMatchObject({
      revision: 4,
      block: 'b3',
    });
    expect(parseRoute(pdfHref('p 1', 'art/1', 3, SHA))).toEqual({
      view: 'pdf',
      project: 'p 1',
      artifact: 'art/1',
      revision: 3,
      sha256: SHA,
      page: null,
    });
    expect(parseRoute(pdfHref('p 1', 'art/1', 3, SHA, 7))).toMatchObject({ page: 7 });
    expect(parseRoute(ledgerHref('p 1'))).toEqual({ view: 'ledger', project: 'p 1' });
    expect(parseRoute(memoryHref('p/1'))).toEqual({ view: 'memory', project: 'p/1' });
    expect(parseRoute(findingHref('p 1', 's/1', 'f#2'))).toEqual({
      view: 'finding',
      project: 'p 1',
      session: 's/1',
      finding: 'f#2',
    });
    expect(parseRoute(issuesHref('p 1'))).toEqual({ view: 'issues', project: 'p 1' });
    expect(parseRoute(frontierHref('p 1'))).toEqual({ view: 'categories', project: 'p 1' });
    expect(parseRoute(categoryHref('p 1', 'cost.v2'))).toEqual({
      view: 'frontier',
      project: 'p 1',
      category: 'cost.v2',
    });
    expect(parseRoute(ledgerEntryHref('p 1', 'T-4', 7))).toEqual({
      view: 'entry',
      project: 'p 1',
      entry: 'T-4',
      revision: 7,
    });
    expect(parseRoute(historyHref('p 1', 'doc-1'))).toEqual({
      view: 'history',
      project: 'p 1',
      document: 'doc-1',
    });
  });
});
