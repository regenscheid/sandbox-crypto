// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { Evidence, MathJob, MathQueue, Review } from './reviews';
import {
  coverageText,
  evidenceText,
  identityText,
  leftUnreviewed,
  locatorText,
  mathCheckText,
  outcomeText,
  queueText,
  recordText,
  reviewLine,
  unreviewedText,
  usageText,
  waitingText,
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
      'Covered feed entries 4 to 9; coverage complete within the available evidence; no limit was reached.',
    );
    expect(
      coverageText(review({ kind: 'confirmation', cap_reached: 1, coverage_complete: 0 })),
    ).toBe('Covered the pinned finding and its fix; coverage incomplete; a limit was reached.');
    expect(usageText(review())).toBe(
      'Reviewed by model-r (alias reviewer) with rubric r2: 1 model call, 1200 input and 80 output tokens (4000 counted against its budget).',
    );
    const limits = { token_budget: 400000, calls_per_hour: 240, max_turns: 24, time_seconds: 300 };
    expect(usageText(review({ calls: 7, config: limits, estimated_tokens: 39988 }))).toBe(
      'Reviewed by model-r (alias reviewer) with rubric r2: 7 model calls of at most 24, ' +
        '1200 input and 80 output tokens; 39988 of its 400000-token budget counted, in at ' +
        "most 300 s. The project's reviewer makes at most 240 calls an hour.",
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
    expect(
      locatorText({ entry: 'entry-1', handle: 'C-3', field: 'statement', body_revision: 2 }),
    ).toBe('the statement of ledger entry C-3');
    expect(locatorText({ entry: 'entry-1', handle: 'C-3', revision: 2, run: 'r1' })).toBe(
      'ledger entry C-3 revision 2',
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
    const statement = { ...check, kind: 'statement', statement: { handle: 'C-3' } };
    expect(mathCheckText(statement as unknown as MathJob)).toBe(
      'Statement of C-3 revision 2: complete, 1 rejected, 0 unavailable',
    );
    const waiting = { ...queued, kind: 'statement', handle: 'C-3' } as MathQueue;
    expect(queueText(waiting)).toBe('statement of C-3 to revision 3 (2 checked): queued');
  });
});

describe('what the reviews panel says, after review', () => {
  it('says a capped review ended at a limit its summary names, and whether a failed window is retried', () => {
    expect(outcomeText('capped')).toMatch(/a limit ended it, which its summary names/);
    expect(outcomeText('capped')).toMatch(/a window a limit cut short is reviewed again$/);
    expect(
      waitingText({ reason: 'calls_per_hour', calls: 240, limit: 240, since: 1791990000 }),
    ).toBe(
      `The project's reviewer used 240 of its 240 calls in the hour before ${when(1791990000)}. ` +
        "The session's work since its last review waits for one review, which starts at the " +
        'next run once calls are left.',
    );
    expect(outcomeText('failed')).toBe(
      'failed: reviewer error; its window is due to be reviewed again',
    );
    expect(outcomeText('dropped', true)).toBe(
      'dropped: lost to a restart; its window was left unreviewed after bounded retries',
    );
    expect(leftUnreviewed([{ review: 'rv-1', start: 1, end: 2 }], 'rv-1')).toBe(true);
    expect(leftUnreviewed([{ review: 'rv-2' }], 'rv-1')).toBe(false);
  });
  it('claims no coverage for a review that did not complete', () => {
    expect(coverageText(review({ state: 'failed', coverage_complete: true }))).toBe(
      'Its window was feed entries 4 to 9; nothing was recorded as covered.',
    );
  });
  it('writes an unreadable time as text instead of throwing', () => {
    expect(when(Number.NaN)).toBe('NaN');
    expect(when(1e20)).toBe('100000000000000000000');
  });
  it('says what a review checked and found consistent (CR-7)', () => {
    const record = {
      sequence: 3,
      kind: 'checked',
      payload: { checked: { statement: 'GF(17) count matches the log.', evidence: ['ev-1'] } },
      created: 1791990000,
    };
    expect(recordText(record)).toBe(
      `#3 checked: GF(17) count matches the log. · ${when(1791990000)}`,
    );
  });
  it('says the configuration and the final-message classifier in words (CR-11, CR-35)', () => {
    const classified = review({
      config_revision: 'cfg-7',
      classifier: { version: 'c2', label: 'terminal', effects: true, completion_claim: true },
    });
    expect(identityText(classified)).toBe(
      'Configuration revision cfg-7. The final message was classified terminal by classifier c2, because the turn had effects and the agent claimed the work complete.',
    );
    expect(identityText(review({ config_revision: 'cfg-7', classifier: {} }))).toBe(
      'Configuration revision cfg-7.',
    );
  });
  it('names a pinned finding as evidence', () => {
    const locator = { finding: 'finding-0123', revision: 2 };
    expect(locatorText(locator)).toBe('finding finding-0123 revision 2');
    const item = { kind: 'finding', locator, complete: true } as unknown as Evidence;
    expect(evidenceText(item)).toBe('finding: finding finding-0123 revision 2 (complete)');
  });
});
