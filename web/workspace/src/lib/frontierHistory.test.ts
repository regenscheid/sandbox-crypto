// SPDX-License-Identifier: MIT
// A frontier's history in words: each change's cause, reason and links, shown as text with
// every link leading to an entry; and the past time sent with its zone.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FrontierChange from '../views/FrontierChange.svelte';
import type { FrontierChange as Change } from './api-types';
import { hostileMarkup } from './approvalTesting';
import { actorText, causeText, pastTime } from './frontierHistory';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const change = (fields: Partial<Change> = {}): Change => ({
  seq: 7,
  conditions: { attack: 'dual' },
  best: [{ entry_id: 'entry-1', handle: 'R-1', revision: 2 }],
  added: ['R-1'],
  removed: ['R-4'],
  cause: { operation_id: 'op-1', author: 'session:s1', reason: 'new bound', entries: ['R-1'] },
  created: '2026-10-15T00:00:00Z',
  more: {},
  ...fields,
});

describe('a change to the best known, in words', () => {
  it('names its cause: the history beginning, a category revision or a ledger change', () => {
    expect(causeText({ baseline: 'frontier history begins' }).summary).toBe(
      'The history begins: the best known results the category had.',
    );
    expect(
      causeText({ operation_id: 'op-2', author: 'user:user', category_revision: 3 }).summary,
    ).toBe("The category's revision r3, by the user, operation op-2");
    expect(
      causeText({
        operation_id: 'op-1',
        author: 'session:s1',
        reason: 'why',
        entries: ['R-1', 7],
        dominates: ['R-1 dominates R-4: tighter'],
      }),
    ).toEqual({
      summary: 'A ledger change by an agent of session s1, operation op-1',
      reason: 'why',
      entries: ['R-1'],
      links: ['R-1 dominates R-4: tighter'],
    });
    // A cause cut short past its bytes keeps no reason or links.
    expect(causeText({ operation_id: 'op-1', author: 'user:user' })).toMatchObject({
      reason: null,
      links: [],
    });
    expect(causeText({ other: 1 }).summary).toBe('A change recorded as {"other":1}');
    expect(actorText(3)).toBe('someone');
  });

  it('sends the time picked with its zone, and nothing for no time', () => {
    const at = pastTime('2026-10-15T12:30');
    expect(at).toBe(new Date('2026-10-15T12:30').toISOString().replace('Z', '+00:00'));
    expect(pastTime('2026-10-15T12:30:05')).toMatch(/:05\.000\+00:00$/);
    expect(pastTime('')).toBeNull();
    expect(pastTime('2026-10-15')).toBeNull();
    expect(pastTime('2026-13-45T99:99')).toBeNull();
  });

  it('is shown as text, every link leading to an entry', () => {
    const html = render(FrontierChange, {
      props: {
        projectId: 'p1',
        change: change({
          conditions: { [HOSTILE]: HOSTILE },
          best: [
            { entry_id: 'entry-1', handle: 'R-1', revision: 2 },
            { entry_id: 'x', handle: HOSTILE, revision: 1 },
          ],
          added: ['javascript:alert(1)'],
          removed: ['R-4', HOSTILE],
          cause: {
            operation_id: HOSTILE,
            author: HOSTILE,
            reason: HOSTILE,
            entries: ['R-1', HOSTILE],
            dominates: [HOSTILE],
          },
          created: HOSTILE,
          more: { best: 3 },
        }),
      },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect([...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1])).toEqual([
      '#/projects/p1/ledger/R-4',
      '#/projects/p1/ledger/R-1/revisions/2',
      '#/projects/p1/ledger/R-1',
    ]);
    expect(html).toContain(' and 3 more');
    const hidden = render(FrontierChange, {
      props: { projectId: 'p1', change: change({ conditions: { attack: 'dual\u202Ehybrid' } }) },
    }).body;
    expect(hidden).toContain('attack: dual⟦U+202E⟧hybrid');
    expect(hidden).not.toContain('\u202E');
    expect(html).toContain('Its reason:');
  });
});
