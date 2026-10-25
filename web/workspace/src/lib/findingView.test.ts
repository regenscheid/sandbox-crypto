// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FindingCard from '../views/FindingCard.svelte';
import { findingDrafts } from './drafts';
import {
  actorText,
  anchorView,
  decisions,
  dismissalText,
  raisedText,
  recheckText,
  resolved,
  setStatus,
  severityText,
  statusClass,
  statusLabel,
  statusText,
  successorText,
  transitionText,
} from './findingView';
import { when } from './reviewText';
import type { Finding, Review, Transition } from './reviews';

type Anchor = Finding['anchors'][number];

describe('the finding card', () => {
  it('counts fixed, superseded and dismissed as resolved (CR-41)', () => {
    expect(['fixed', 'superseded', 'dismissed'].map(resolved)).toEqual([true, true, true]);
    for (const status of ['open', 'stale']) expect(resolved(status)).toBe(false);
    expect(statusText('constructor')).toBe('constructor');
  });

  it('gives each of the five statuses its chip, class and text', () => {
    const chips = {
      open: 'Open',
      fixed: 'Fixed',
      dismissed: 'Dismissed',
      superseded: 'Superseded',
      stale: 'Stale',
    } as const;
    for (const [status, chip] of Object.entries(chips)) {
      expect(statusLabel({ status: status as Finding['status'] })).toBe(chip);
      expect(statusClass(status)).toBe(`status-${status}`);
      expect(statusText(status)).toMatch(new RegExp(`^${chip}: `));
    }
    expect(statusClass('claimed fixed')).toBe('status-unknown');
  });

  it('says plainly when an open finding was last re-checked inconclusively', () => {
    const latest = (answer: 'resolved' | 'unresolved' | 'inconclusive') =>
      ({ review: 'rv-2', answer, why: 'w' }) as NonNullable<Finding['latest_recheck']>;
    expect(statusLabel({ status: 'open', latest_recheck: latest('inconclusive') })).toBe(
      'Open, last re-check inconclusive',
    );
    expect(statusLabel({ status: 'open', latest_recheck: latest('unresolved') })).toBe('Open');
    expect(statusLabel({ status: 'stale', latest_recheck: latest('inconclusive') })).toBe('Stale');
    expect(statusText('open', latest('inconclusive'))).toMatch(/last re-check was inconclusive/);
    expect(statusText('open', latest('unresolved'))).toBe(statusText('open'));
  });

  it('offers only the decisions the status allows', () => {
    const actions = (status: string) => decisions(status).map((item) => item.action);
    expect(actions('open')).toEqual(['fix', 'dismiss', 'set:fixed', 'set:superseded', 'set:stale']);
    expect(actions('stale')).toEqual(['fix', 'dismiss', 'set:open', 'set:fixed', 'set:superseded']);
    expect(actions('fixed')).toEqual(['dismiss', 'set:open', 'set:superseded', 'set:stale']);
    expect(actions('superseded')).toEqual(['dismiss', 'set:open', 'set:fixed', 'set:stale']);
    expect(actions('dismissed')).toEqual(['set:open', 'set:fixed', 'set:superseded', 'set:stale']);
    expect(decisions('open')[2]!.label).toBe('Set to fixed, as the user');
    expect(setStatus('set:stale')).toBe('stale');
    expect([setStatus('fix'), setStatus('set:withdrawn'), setStatus('rebut')]).toEqual([
      null,
      null,
      null,
    ]);
  });

  it('tells a dismissal, a successor and a re-check in words', () => {
    const dismissal = {
      by: 'agent',
      actor: 'session:s1',
      reason: 'The bound holds.',
      at: 1791990000,
      review: { review: 'rv-3', decision: 'rejected', reason: 'No proof.', at: 1791990100 },
    } as NonNullable<Finding['dismissal']>;
    expect(dismissalText(dismissal)).toEqual([
      `Dismissed by the main agent of session s1 · ${when(1791990000)}: The bound holds.`,
      'Review rv-3 rejected the dismissal: No proof.',
    ]);
    expect(dismissalText({ ...dismissal, by: 'user', review: null })[0]).toMatch(
      /^Dismissed by the user/,
    );
    expect(successorText({ supersedes: 'f-1', superseded_by: 'f-3' })).toEqual([
      'It replaced finding f-1.',
      'It was replaced by finding f-3.',
    ]);
    expect(successorText({ supersedes: null })).toEqual([]);
    expect(
      recheckText({
        latest_recheck: { review: 'rv-2', answer: 'unresolved', why: 'Still wrong.' } as never,
        recheck: { cause: 'fix' } as never,
        at_bound: true,
      }),
    ).toEqual([
      'Last re-check, review rv-2: unresolved. Still wrong.',
      'A re-check is queued (after a claimed fix).',
      'At its re-flag bound: the agent is not offered it again.',
    ]);
  });

  it('links a document anchor to its revision with the block in focus', () => {
    const anchor = {
      evidence: 'ev-1',
      review: 'rv-1',
      block: 'b2',
      math: 'm1',
      locator: { document: 'doc-1', revision: 3 },
    } as unknown as Anchor;
    expect(anchorView('p1', anchor)).toEqual({
      text: 'document doc-1 revision 3, block b2, formula m1',
      href: '#/projects/p1/documents/doc-1/revisions/3/blocks/b2',
      evidence: { review: 'rv-1', evidence: 'ev-1' },
      check: null,
    });
  });

  it('names transcript, log and math-check anchors, with no link where there is no page', () => {
    const transcript = {
      evidence: 'ev-2',
      review: 'rv-1',
      locator: { run: 'run-1', invocation: 'i', message: 4 },
    } as unknown as Anchor;
    expect(anchorView('p1', transcript)).toMatchObject({
      text: 'message 4 of run run-1',
      href: null,
    });
    const log = {
      evidence: 'ev-3',
      review: 'rv-1',
      stream: 'stdout',
      first_line: 2,
      last_line: 5,
      locator: { execution: 'exe-1', run: 'run-1' },
    } as unknown as Anchor;
    expect(anchorView('p1', log).text).toBe('execution exe-1 in run run-1, stdout lines 2–5');
    const comment = {
      check: 'mc-1',
      math: 'm3',
      block: 'b1',
      locator: { document: 'doc-1', revision: 2, comment: 'cmt-1', body_revision: 1 },
    } as unknown as Anchor;
    expect(anchorView('p1', comment)).toEqual({
      text: 'comment cmt-1 in document doc-1, formula m3',
      href: null,
      evidence: null,
      check: { check: 'mc-1', math: 'm3' },
    });
  });

  it('tells a history step in words', () => {
    const entry = {
      revision: 2,
      status: 'fixed',
      actor: 'agent:run-1',
      created: 1791990000,
    };
    expect(transitionText(entry as Transition)).toBe(
      `Revision 2: fixed, by agent:run-1 · ${when(1791990000)}`,
    );
    expect(
      transitionText({
        ...entry,
        former_status: 'confirmed fixed',
        answer: 'resolved',
      } as Transition),
    ).toBe(
      `Revision 2: fixed (recorded as confirmed fixed), re-check resolved, by agent:run-1 · ${when(1791990000)}`,
    );
  });
});

describe('the finding card, after review', () => {
  it('names a reviewer anchor on a pinned finding as that finding, not a response', () => {
    const anchor = {
      evidence: 'ev-1',
      review: 'rv-1',
      locator: { finding: 'finding-0123', revision: 2 },
    } as unknown as Anchor;
    expect(anchorView('p1', anchor).text).toBe('finding finding-0123 revision 2');
  });

  it('says who decided, what a severity means and which review raised the finding', () => {
    expect(actorText('review:rv-1')).toBe('review rv-1');
    expect(actorText('session:s1')).toBe('the main agent of session s1');
    expect(actorText('user:user')).toBe('the user');
    expect(actorText('platform:math')).toBe('the math check');
    expect(severityText('warn')).toBe('a claim that needs correction or qualification');
    const finding = { review_id: 'rv-1' } as Finding;
    expect(raisedText(finding, undefined)).toBe('Raised by review rv-1.');
    const review = {
      review_id: 'rv-1',
      kind: 'terminal',
      resolved_model: 'model-r',
      alias: 'reviewer',
      rubric: 'r2',
    } as Review;
    expect(raisedText(finding, review)).toBe(
      'Raised by terminal review rv-1: model-r, alias reviewer, rubric r2.',
    );
  });

  it('keeps a kept decision the status no longer allows, marked, and does not let it be saved', () => {
    findingDrafts.save('p:s:finding-kept', {
      expected: 2,
      explanation: 'It is fixed.',
      action: 'rebut',
      references: '',
    });
    const finding = {
      finding_id: 'finding-kept',
      review_id: null,
      severity: 'warn',
      category: 'math',
      statement: 'S',
      remedy: null,
      status: 'fixed',
      revision: 3,
      reflags: 0,
      anchors: [],
    } as unknown as Finding;
    const html = render(FindingCard, {
      props: { projectId: 'p', sessionId: 's', finding, changed: async () => {} },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('rebut (kept, not allowed now)');
    expect(html).toContain('Your kept decision, rebut, is not allowed');
    expect(html).toMatch(/<button disabled[^>]*>\s*Save user response/);
  });
});

describe('the finding card, for each status', () => {
  const base = {
    finding_id: 'finding-s',
    review_id: null,
    severity: 'warn',
    category: 'math',
    statement: 'S',
    remedy: null,
    revision: 3,
    reflags: 0,
    anchors: [],
  };
  const html = (extra: Record<string, unknown>) =>
    render(FindingCard, {
      props: {
        projectId: 'p',
        sessionId: 's',
        finding: { ...base, ...extra } as unknown as Finding,
        changed: async () => {},
      },
    }).body.replace(/\s+/g, ' ');

  it.each([
    ['open', 'Open'],
    ['fixed', 'Fixed'],
    ['dismissed', 'Dismissed'],
    ['superseded', 'Superseded'],
    ['stale', 'Stale'],
  ])('shows %s as a chip', (status, label) => {
    expect(html({ status })).toMatch(
      new RegExp(`<span class="chip status-${status}[ "][^>]*>${label}</span>`),
    );
  });

  it('shows the inconclusive re-check, the dismissal and the lineage', () => {
    const open = html({
      status: 'open',
      latest_recheck: { review: 'rv-2', answer: 'inconclusive', why: 'No evidence.' },
    });
    expect(open).toContain('>Open, last re-check inconclusive</span>');
    expect(open).toContain('Last re-check, review rv-2: inconclusive. No evidence.');
    const dismissed = html({
      status: 'dismissed',
      dismissal: { by: 'user', actor: 'user:user', reason: 'Not a defect.', at: 1791990000 },
      supersedes: 'finding-old',
      superseded_by: 'finding-new',
    });
    expect(dismissed).toContain('Dismissed by the user');
    expect(dismissed).toContain('Not a defect.');
    expect(dismissed).toContain('It replaced finding finding-old.');
    expect(dismissed).toContain('It was replaced by finding finding-new.');
    expect(dismissed).toContain('Set to open, as the user');
  });

  it('describes the new behaviour and offers the revisions field only for a fix', () => {
    const text = html({ status: 'open' });
    expect(text).toContain('A claimed fix queues a re-check; only a re-check or the user');
    expect(text).not.toContain('unconfirmed until a confirmation review');
    expect(text).not.toContain('Changed document revisions');
    findingDrafts.save('p:s:finding-s', {
      expected: 3,
      explanation: 'Fixed in r2.',
      action: 'fix',
      references: '',
    });
    expect(html({ status: 'open' })).toContain('Changed document revisions');
    findingDrafts.clear('p:s:finding-s');
  });
});
