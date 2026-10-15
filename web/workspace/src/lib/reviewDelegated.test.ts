// SPDX-License-Identifier: MIT
// A review's delegated work and a fix's cited files, as the review panel labels them
// (CR-2 as amended for lab 41's B5; the confirmation check of a claimed fix): a file the
// fix cites, a delegation's result file, a delegation's outcome brought in with its run's
// work, and a delegated run's actions under its delegation.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ReviewActions from '../views/ReviewActions.svelte';
import { hostileMarkup } from './approvalTesting';
import { actionsOf } from './reviewActions';
import { describeItem } from './reviewContext';
import type { Evidence } from './reviews';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const artifact = (fields: Record<string, unknown>) => ({
  kind: 'artifact',
  locator: {
    artifact: { artifact_id: 'art-1', revision: 2, sha256: 'ab'.repeat(32) },
    run: 'run-a',
    path: 'results/ell.json',
    ...fields,
  } as Evidence['locator'],
});

describe("a review's delegated work and a fix's files", () => {
  it('labels a file the fix cites, leading to the finding', () => {
    expect(describeItem('p1', 's1', artifact({ cited_by: { finding: 'finding-1' } }))).toEqual({
      mark: 'Cited by the fix:',
      text: 'file results/ell.json',
      links: [{ text: 'the finding', href: '#/projects/p1/sessions/s1/findings/finding-1' }],
    });
  });

  it("labels a delegation's result file, leading to the run tree", () => {
    expect(
      describeItem('p1', 's1', artifact({ delegated: { run: 'run-c', delegation: 'del-2' } })),
    ).toEqual({
      mark: 'Result file of delegation del-2:',
      text: 'file results/ell.json',
      links: [{ text: 'the run tree', href: '#/projects/p1/sessions/s1/tree' }],
    });
    // Without a path, the artifact and its revision name it.
    const unnamed = artifact({ path: undefined, delegated: { delegation: 'del-2' } });
    expect(describeItem('p1', 's1', unnamed)!.text).toBe('artifact art-1 r2');
  });

  it("labels a delegation's outcome brought in with its run's work, as context", () => {
    expect(
      describeItem('p1', 's1', {
        kind: 'delegation',
        locator: { run: 'run-c', delegation: 'del-2', name: '2026-10-15T00:00:00Z-run-c' },
      }),
    ).toEqual({
      mark: 'Delegated work, as context, not reviewed material:',
      text: 'the outcome of delegation del-2',
      links: [
        {
          text: 'its run’s events',
          href: '#/projects/p1/sessions/s1/runs/2026-10-15T00%3A00%3A00Z-run-c',
        },
        { text: 'the run tree', href: '#/projects/p1/sessions/s1/tree' },
      ],
    });
  });

  it('leaves other items to their locators, and shows hostile names as text', () => {
    expect(describeItem('p1', 's1', artifact({}))).toBeNull();
    expect(describeItem('p1', 's1', { kind: 'ledger', locator: { entry: 'e' } })).toBeNull();
    const hostile = describeItem(
      'p1',
      's1',
      artifact({ path: `a‮${HOSTILE}`, delegated: { delegation: `d‮` } }),
    )!;
    expect(hostile.mark).toBe('Result file of delegation d⟦U+202E⟧:');
    expect(hostile.text).toContain('a⟦U+202E⟧');
  });

  it("groups a delegated run's actions under its delegation, apart from its parent's", () => {
    const found = actionsOf(
      JSON.stringify({
        actions: [
          { tool: 'delegate', target: {}, outcome: 'ok', made: ['delegation del-2'], run: 'run-a' },
          {
            tool: 'execute',
            target: { path: 'x.py' },
            outcome: 'error',
            made: [],
            run: 'run-c',
            delegation: 'del-2',
          },
          { tool: 'publish', target: {}, outcome: 'ok', made: [], run: 'run-a' },
        ],
        total: 3,
        documents: [],
      }),
    )!;
    expect(found.runs.map((group) => [group.run, group.delegation, group.actions.length])).toEqual([
      ['run-a', null, 2],
      ['run-c', 'del-2', 1],
    ]);
    const html = render(ReviewActions, {
      props: { projectId: 'p1', sessionId: 's1', actions: found },
    }).body;
    expect(html.replace(/<!--[^>]*-->/g, '').replace(/\s+/g, ' ')).toMatch(
      /Delegated run <a href="#\/projects\/p1\/sessions\/s1\/runs\/run-c">run-c<\/a>, of delegation <code>del-2<\/code>/,
    );
    expect(hostileMarkup(html)).toBe(false);
  });
});
