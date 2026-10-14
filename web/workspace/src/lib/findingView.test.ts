// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FindingCard from '../views/FindingCard.svelte';
import { findingDrafts } from './drafts';
import {
  actorText,
  anchorView,
  decisions,
  raisedText,
  resolved,
  severityText,
  statusText,
  transitionText,
} from './findingView';
import { when } from './reviewText';
import type { Finding, Review, Transition } from './reviews';

type Anchor = Finding['anchors'][number];

describe('the finding card', () => {
  it('counts only confirmed fixed and withdrawn as resolved (CR-27)', () => {
    expect(['confirmed fixed', 'withdrawn'].map(resolved)).toEqual([true, true]);
    for (const status of ['open', 'claimed fixed', 'unaddressed', 'superseded', 'inconclusive'])
      expect(resolved(status)).toBe(false);
    expect(statusText('superseded')).toMatch(/nothing was checked/);
    expect(statusText('constructor')).toBe('constructor');
  });

  it('offers only the decisions the status allows', () => {
    const actions = (status: string) => decisions(status).map((item) => item.action);
    expect(actions('open')).toEqual(['rebut', 'fix', 'confirm', 'dismiss']);
    expect(actions('unaddressed')).toEqual(['rebut', 'fix', 'confirm', 'dismiss', 'reopen']);
    expect(actions('withdrawn')).toEqual(['reopen']);
    expect(actions('confirmed fixed')).toEqual(['reopen']);
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
      status: 'claimed fixed',
      actor: 'agent:run-1',
      created: 1791990000,
    };
    expect(transitionText(entry as Transition)).toBe(
      `Revision 2: claimed fixed, by agent:run-1 · ${when(1791990000)}`,
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
      action: 'confirm',
      references: '',
    });
    const finding = {
      finding_id: 'finding-kept',
      review_id: null,
      severity: 'warn',
      category: 'math',
      statement: 'S',
      remedy: null,
      status: 'confirmed fixed',
      revision: 3,
      reflags: 0,
      anchors: [],
    } as unknown as Finding;
    const html = render(FindingCard, {
      props: { projectId: 'p', sessionId: 's', finding, changed: async () => {} },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('confirm (kept, not allowed now)');
    expect(html).toContain('Your kept decision, confirm, is not allowed');
    expect(html).toMatch(/<button disabled[^>]*>\s*Save user response/);
  });
});
