// SPDX-License-Identifier: MIT
// A review window's actions as the review panel shows them (CR-2 as amended for lab 40):
// grouped by run in order, a refused or failed call marked, what each made leading where
// the workspace has a page, and the agent's arguments as text with every hidden character
// as its code point (hidden.fixtures.json).
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ReviewActions from '../views/ReviewActions.svelte';
import { hostileMarkup } from './approvalTesting';
import fixtures from './hidden.fixtures.json';
import {
  actionsOf,
  actionsText,
  blockLink,
  madeLink,
  marked,
  runLink,
  targetText,
} from './reviewActions';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const payload = (fields: Record<string, unknown> = {}) =>
  JSON.stringify({
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
        run: 'run-b',
      },
      {
        tool: 'execute',
        call: 'c3',
        target: { path: 'out/table.csv' },
        outcome: 'error',
        made: ['execution exe-1 (failed)', 'file out/table.csv'],
        run: 'run-a',
      },
    ],
    total: 5,
    shown: 3,
    documents: [
      {
        document: 'doc-1',
        revision: 3,
        since: 2,
        changed: ['b2', 'b4'],
        changed_total: 3,
        removed: ['b9'],
        removed_total: 1,
      },
    ],
    ...fields,
  });

describe("a window's actions", () => {
  it('are named in the item list by the feed entries they span', () => {
    expect(
      actionsText({ kind: 'actions', locator: { actions: 'window', from: 4, to: 9 } }),
    ).toMatch(/^What the runs did, feed entries 4 to 9: their calls that change something/);
    expect(actionsText({ kind: 'ledger', locator: {} })).toBe('');
  });

  it('are read by run, in the order each run first acts, with how many are left out', () => {
    const found = actionsOf(payload())!;
    expect(found.runs.map((run) => [run.run, run.actions.map((a) => a.tool)])).toEqual([
      ['run-a', ['document_patch', 'execute']],
      ['run-b', ['ledger_change']],
    ]);
    expect([found.shown, found.total]).toEqual([3, 5]);
    expect(found.documents[0]).toEqual({
      document: 'doc-1',
      revision: 3,
      since: 2,
      changed: ['b2', 'b4'],
      changedTotal: 3,
      removed: ['b9'],
      removedTotal: 1,
    });
    expect(actionsOf('{"actions": "no"}')).toBeNull();
    expect(actionsOf('{"cut')).toBeNull();
  });

  it('mark what was refused or failed, and lead to what was made where a page exists', () => {
    expect(['ok', 'refused', 'error'].map(marked)).toEqual([false, true, true]);
    expect(madeLink('p 1', 's1', 'document doc-1 r3')).toEqual({
      text: 'document doc-1 r3',
      href: '#/projects/p%201/documents/doc-1/revisions/3',
    });
    expect(madeLink('p1', 's1', 'ledger C-4 r2').href).toBe('#/projects/p1/ledger/C-4/revisions/2');
    expect(madeLink('p1', 's1', 'ledger C-4/../x r2').href).toBeNull();
    expect(madeLink('p1', 's 1', 'delegation del-1').href).toBe(
      '#/projects/p1/sessions/s%201/tree',
    );
    for (const named of [
      'work record work-1',
      'artifact art-1 r2',
      'execution exe-1 (ok)',
      'file out/x.csv',
    ]) {
      expect(madeLink('p1', 's1', named).href, named).toBeNull();
    }
    const change = actionsOf(payload())!.documents[0]!;
    expect(blockLink('p1', change, 'b4').href).toBe(
      '#/projects/p1/documents/doc-1/revisions/3/blocks/b4',
    );
    expect(blockLink('p1', change, '../b').href).toBeNull();
    expect(runLink('p1', 's1', 'run-a').href).toBe('#/projects/p1/sessions/s1/runs/run-a');
    expect(runLink('p1', 's1', '').href).toBeNull();
    expect(targetText({ target: [['path', 'a‮b']] })).toBe('path a⟦U+202E⟧b');
  });

  it('show as a table for each run, as text, every link leading into the workspace', () => {
    const hostile = actionsOf(
      payload({
        actions: [
          {
            tool: HOSTILE,
            target: { [HOSTILE]: HOSTILE },
            outcome: 'refused',
            made: [`document ${HOSTILE} r2`, `file ${HOSTILE}`, `ledger ${HOSTILE} r1`],
            run: HOSTILE,
          },
        ],
        documents: [
          { document: HOSTILE, revision: 1, changed: ['b1', HOSTILE], removed: [HOSTILE] },
        ],
      }),
    )!;
    const html = render(ReviewActions, {
      props: { projectId: 'p1', sessionId: 's1', actions: hostile },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect([...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1])).toEqual([
      `#/projects/p1/sessions/s1/runs/${encodeURIComponent(HOSTILE)}`,
      `#/projects/p1/documents/${encodeURIComponent(HOSTILE)}/revisions/2`,
      `#/projects/p1/documents/${encodeURIComponent(HOSTILE)}/revisions/1/blocks/b1`,
    ]);
    expect(html).toContain('<strong class="problem">refused</strong>');
  });

  it('say how many calls are listed, and when a run changed nothing', () => {
    const html = render(ReviewActions, {
      props: { projectId: 'p1', sessionId: 's1', actions: actionsOf(payload())! },
    }).body;
    expect(html).toContain("The first 3 of the window's 5 calls are listed.");
    expect(html.match(/class="problem"/g)).toHaveLength(2);
    expect(html).toContain('<table');
    const none = render(ReviewActions, {
      props: {
        projectId: 'p1',
        sessionId: 's1',
        actions: actionsOf(payload({ actions: [], total: 0, documents: [] }))!,
      },
    }).body;
    expect(none).toContain('No run changed anything in this window.');
  });

  it('render no text as HTML', () => {
    const views = import.meta.glob('../views/ReviewActions.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    expect(Object.keys(views)).toHaveLength(1);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });

  const cases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(cases)(
    '$name, in a target and a file made: each hidden character as its code point',
    ({ text, hidden }) => {
      const found = actionsOf(
        payload({
          actions: [
            { tool: 't', target: { path: text }, outcome: 'ok', made: [`file ${text}`], run: 'r' },
          ],
        }),
      )!;
      const html = render(ReviewActions, {
        props: { projectId: 'p1', sessionId: 's1', actions: found },
      }).body;
      for (const [, code] of hidden) {
        expect(html.split(`⟦${code}⟧`).length - 1).toBeGreaterThanOrEqual(2);
      }
    },
  );
});
