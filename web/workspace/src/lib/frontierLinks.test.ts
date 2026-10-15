// SPDX-License-Identifier: MIT
// What dominates a result on the frontier, by the rule or by a link, and the links that
// count for nothing: shown as text, every link leading to an entry; and what the user's
// drop of a link sends and says when it fails.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FrontierLink from '../views/FrontierLink.svelte';
import FrontierResult from '../views/FrontierResult.svelte';
import { ApiError } from './api';
import type { DominatesLink, EntryLink, FrontierResult as Result } from './api-types';
import { hostileMarkup } from './approvalTesting';
import { dropFailure, dropProblem, heldLink, recordedText, ruleDominators } from './frontierLinks';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';
const done = async () => {};

const link = (fields: Partial<DominatesLink> = {}): DominatesLink => ({
  entry_id: 'entry-0123456789abcdef0123456a',
  handle: 'R-3',
  reason: 'R-3 is a tighter bound under a stronger model',
  author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
  revision: 4,
  target: 'R-5',
  void: null,
  ...fields,
});

const result = (fields: Partial<Result> = {}): Result => ({
  entry_id: 'entry-0123456789abcdef0123456b',
  handle: 'R-5',
  revision: 1,
  title: 'Dual attack',
  statement: 'S',
  status: 'established',
  origin: 'project',
  disputed: false,
  unverified: false,
  author: { kind: 'user', user: 'owner' },
  conditions: { attack: 'dual' },
  measures: { time: 160 },
  evidence_types: [],
  sources: [],
  artifacts: [],
  open_findings: 0,
  open_flags: 0,
  dominated_by: ['R-1', 'R-3'],
  linked_by: [link()],
  would_dominate: [],
  ...fields,
});

const entryLink = (fields: Partial<EntryLink> = {}): EntryLink => ({
  kind: 'dominates',
  target: 'entry',
  target_id: 'entry-0123456789abcdef0123456b',
  revision: null,
  block: null,
  note: 'why',
  handle: 'R-5',
  session: null,
  ...fields,
});

const hrefs = (html: string) => [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]!);

describe('what dominates a result', () => {
  it('tells the rule from the links', () => {
    expect(ruleDominators(result())).toEqual(['R-1']);
    expect(ruleDominators(result({ linked_by: [] }))).toEqual(['R-1', 'R-3']);
    expect(recordedText(link())).toBe(
      'recorded in R-3 r4 by agent run run-1 of session s1, role main',
    );
  });

  it("finds the holder's dominates link to the target, by handle or ID, and no other kind", () => {
    const links = [entryLink({ kind: 'compares_to' }), entryLink({ note: 'the one' })];
    expect(heldLink({ links }, 'R-5')?.note).toBe('the one');
    expect(heldLink({ links }, 'entry-0123456789abcdef0123456b')?.note).toBe('the one');
    expect(heldLink({ links: [entryLink({ kind: 'compares_to' })] }, 'R-5')).toBeNull();
  });

  it('finds the link to the target among two dominates links one holder holds', () => {
    const links = [
      entryLink({ handle: 'R-6', target_id: 'entry-0123456789abcdef0123456f', note: 'to R-6' }),
      entryLink({ note: 'to R-5' }),
    ];
    expect(heldLink({ links }, 'R-5')?.note).toBe('to R-5');
    expect(heldLink({ links }, 'R-6')?.note).toBe('to R-6');
    expect(heldLink({ links }, 'entry-0123456789abcdef0123456f')?.note).toBe('to R-6');
    expect(heldLink({ links }, 'R-7')).toBeNull();
  });

  it('shows the rule and each link, its reason, author and revision, as text', () => {
    const html = render(FrontierResult, {
      props: {
        projectId: 'p1',
        measures: [{ name: 'time', unit: 'bits', direction: 'lower' }],
        result: result({
          dominated_by: ['R-1', 'javascript:alert(1)', HOSTILE],
          linked_by: [
            link({
              handle: 'javascript:alert(1)',
              target: 'R-5',
              reason: HOSTILE,
              author: { kind: 'agent', role: HOSTILE, run_id: HOSTILE },
            }),
            link({ handle: HOSTILE, entry_id: 'entry-x', reason: '' }),
          ],
        }),
        done,
      },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect(hrefs(html)).toEqual([
      '#/projects/p1/ledger/R-5/revisions/1',
      '#/projects/p1/ledger/R-1',
      '#/projects/p1/ledger/R-5',
      '#/projects/p1/ledger/R-5',
    ]);
    expect(html).toContain('Dominated by the rule');
    expect(html).toContain('it gives no reason');
    expect(html.match(/Drop this link…/g)).toHaveLength(2);
  });

  it('says why a void link counts for nothing, and offers to drop it', () => {
    const html = render(FrontierLink, {
      props: { projectId: 'p1', link: link({ void: 'the rule decides' }), done },
    }).body;
    expect(html).toContain('It counts for nothing: the rule decides.');
    expect(html).toContain('Drop this link…');
  });
});

describe("the user's drop of a link", () => {
  it('needs a reason the server takes', () => {
    expect(dropProblem('  ')).toBe('Give the reason: why the link should not count.');
    expect(dropProblem('x'.repeat(4097))).toBe('The reason is longer than 4096 characters.');
    expect(dropProblem('the measures are now comparable')).toBeNull();
  });

  it('says what a conflict and a missing link mean, and leaves other failures to the view', () => {
    expect(dropFailure(new ApiError(409, 'conflict', 'x'), 'R-3', 4)).toMatch(
      /^R-3 has a newer revision than the r4 you read, so nothing was dropped/,
    );
    expect(dropFailure(new ApiError(404, 'not_found', 'x'), 'R-3', 4)).toBe(
      'R-3 no longer holds this link: nothing was dropped.',
    );
    expect(dropFailure(new ApiError(503, 'unavailable', 'x'), 'R-3', 4)).toBeNull();
    expect(dropFailure(new Error('x'), 'R-3', 4)).toBeNull();
  });
});
