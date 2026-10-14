// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { Evidence, MathJob, MathQueue, Review } from './reviews';
import {
  coverageText,
  evidenceText,
  locatorText,
  mathCheckText,
  outcomeText,
  queueText,
  recordText,
  reviewLine,
  unreviewedText,
  usageText,
  when,
} from './reviewText';

const review = (fields: Partial<Review> = {}): Review =>
  ({
    review_id: 'rv-1',
    kind: 'checkpoint',
    state: 'passed',
    summary: '',
    created: 1791990000,
    alias: 'reviewer',
    resolved_model: 'model-r',
    rubric: 'r2',
    calls: 1,
    input_tokens: 1200,
    output_tokens: 80,
    estimated_tokens: 4000,
    cap_reached: false,
    coverage_complete: true,
    start_seq: 4,
    end_seq: 9,
    config_revision: 'c1',
    classifier: {},
    ...fields,
  }) as Review;

describe('what the reviews panel says', () => {
  it('says what each outcome means, and that only a pass reads as one', () => {
    expect(outcomeText('passed')).toMatch(/^passed: no inconsistency found .*not a proof/);
    expect(outcomeText('capped')).toMatch(/^capped/);
    expect(outcomeText('constructor')).toBe('constructor');
  });
  it('names a review by its kind, outcome and time, from epoch seconds or ISO text', () => {
    expect(reviewLine(review())).toBe(`Checkpoint review, passed · ${when(1791990000)}`);
    expect(when(1791990000)).toBe(when('2026-10-14T15:00:00.000Z'));
    expect(reviewLine(review({ kind: 'confirmation', state: 'findings' }))).toMatch(
      /^Confirmation review, findings/,
    );
  });
  it('says what a review covered, and what it used', () => {
    expect(coverageText(review())).toBe(
      'Covered feed entries 4 to 9; coverage complete within the available evidence; the finding cap was not reached.',
    );
    expect(
      coverageText(review({ kind: 'confirmation', cap_reached: 1, coverage_complete: 0 })),
    ).toBe(
      'Covered the pinned finding and its fix; coverage incomplete; the finding cap was reached.',
    );
    expect(usageText(review())).toBe(
      'Reviewed by model-r (alias reviewer) with rubric r2: 1 model call, 1200 input and 80 output tokens (4000 reserved).',
    );
  });
  it('says where evidence points, for each kind of locator', () => {
    expect(locatorText({ document: 'doc-1', revision: 3, block: 'b2' })).toBe(
      'document doc-1 revision 3, block b2',
    );
    expect(locatorText({ execution: 'exe-1', attempt: 'a1', run: 'run-1' })).toBe(
      'execution exe-1 in run run-1',
    );
    expect(locatorText({ run: 'run-1', invocation: 'i', message: 4 })).toBe(
      'message 4 of run run-1',
    );
    expect(locatorText({ artifact: '{}', run: 'run-1' })).toBe(
      'an artifact published by run run-1',
    );
    expect(locatorText({})).toBe('a pinned record');
    const item = { kind: 'document', locator: { document: 'doc-1', revision: 3 }, complete: false };
    expect(evidenceText(item as unknown as Evidence)).toBe(
      'document: document doc-1 revision 3 (incomplete: some content is not pinned)',
    );
  });
  it('says what was left unreviewed, and names audit records', () => {
    expect(unreviewedText({ start: 4, end: 9, failures: 3 })).toBe(
      'Feed entries 4 to 9 were left unreviewed after 3 failed reviews.',
    );
    expect(unreviewedText({ type: 'confirmation', failures: 3 })).toMatch(/fix stays unconfirmed/);
    expect(recordText({ sequence: 2, kind: 'tool', created: 1791990000 })).toBe(
      `#2 tool · ${when(1791990000)}`,
    );
  });
  it('says what a math check found and what waits for one', () => {
    const check = { kind: 'comment', revision: 2, state: 'complete', rejected: 1, unavailable: 0 };
    expect(mathCheckText(check as MathJob)).toBe(
      'Comment body revision 2: complete, 1 rejected, 0 unavailable',
    );
    const queued = { document: 'doc-1', target: 3, completed: 2, blocked: null, running: 0 };
    expect(queueText(queued as MathQueue)).toBe('document doc-1 to revision 3 (2 checked): queued');
    expect(queueText({ ...queued, blocked: 'quota' } as MathQueue)).toMatch(/held: quota$/);
  });
});
