// SPDX-License-Identifier: MIT
// How a run's review ended, in the run's section of the session view (CR-48): the reason
// no final review ran, or what it could not read, what is left unreviewed and why, the
// open findings, and how its findings were answered and re-checked, in plain words.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import FinalReviewEnd from '../views/FinalReviewEnd.svelte';
import type { FinalReviewSummary } from './api';
import { finalReviewEnd } from './reviews';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
});

function shown(summary: Parameters<typeof finalReviewEnd>[0]): string[] {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(FinalReviewEnd, { target, props: { summary } }));
  flushSync();
  return [...target.querySelectorAll('li')].map((li) => li.textContent ?? '');
}

const FINAL: FinalReviewSummary = {
  mode: 'hold',
  outcome: 'findings',
  finished: true,
  answered: true,
  rechecked: true,
  recheck: { review: 'rev-0123456789abcdef01234567', outcome: 'passed' },
  open: { fail: 1, warn: 0 },
  unreviewed: [{ start: 3, end: 9, items: 4, reasons: ['final_budget'] }],
  unreviewed_ranges: 2,
  items: { total: 42, reviewed: 34, read_in_part: 2, unreviewed: 6, not_recorded: 0 },
  working_files_deleted: 1,
  unreviewed_reasons: { final_budget: 1, answering_turn: 1 },
  unread: [
    {
      evidence: 'ev-0123456789abcdef01234567',
      kind: 'artifact',
      item: 'artifact-0123456789abcdef01234567',
      path: 'sessions/s/plot.png',
      why: 'media type is outside the raw text evidence allowlist',
    },
    { evidence: 'ev-2', kind: 'unobserved', item: 'op-1', path: null, why: 'the feed refused it' },
  ],
  findings: [
    { finding: 'fnd-1', severity: 'fail', status: 'fixed', recheck: 'resolved' },
    { finding: 'fnd-2', severity: 'fail', status: 'dismissed', recheck: 'dismissal_rejected' },
    { finding: 'fnd-3', severity: 'warn', status: 'dismissed', recheck: 'dismissal_accepted' },
    { finding: 'fnd-4', severity: 'warn', status: 'open', recheck: 'not_rechecked' },
  ],
};

describe("how a run's review ended", () => {
  it('names the unread items, the unreviewed ranges, the open findings and the re-check', () => {
    const lines = shown({ final_review: FINAL });
    expect(lines).toEqual([
      'Not read: sessions/s/plot.png, media type is outside the raw text evidence allowlist.',
      'Not read: a step the review could not observe, the feed refused it.',
      "Reviewed whole: 34 of 42 items of the session's work; 2 read in part, 6 not reviewed.",
      '1 working file the run made and deleted had nothing to read.',
      "Unreviewed: 2 ranges of the session's work (1 because the final review's budget left it" +
        " out; 1 because it is the work of the turn that answered the final review's findings).",
      'Open findings: 1 fail.',
      'The agent answered its findings; one re-check ran (passed). Findings: 1 resolved, 1' +
        ' dismissed, the dismissal accepted, 1 dismissed, the dismissal rejected, 1 not re-checked.',
    ]);
    // No internal identifier beyond a file path.
    expect(lines.join(' ')).not.toMatch(/rev-|fnd-|ev-|artifact-|op-1/);
  });

  it('names a skipped backlog and an inconclusive final review in plain words', () => {
    expect(
      finalReviewEnd({
        final_review: {
          open: {},
          unreviewed_ranges: 2,
          unreviewed_reasons: { backlog: 1, final_inconclusive: 1 },
          items: { total: 909, reviewed: 384, unreviewed: 525, not_recorded: 0 },
        },
      }),
    ).toEqual([
      "Reviewed whole: 384 of 909 items of the session's work; 525 not reviewed.",
      "Unreviewed: 2 ranges of the session's work (1 because a checkpoint skipped it to reach" +
        ' the newest work; 1 because the final review could not decide it).',
      'No open findings.',
    ]);
  });

  it('gives the reason when no final review ran', () => {
    const reason = 'No final review: consistency reviews are disabled for the project.';
    expect(shown({ no_final_review: reason })).toEqual([reason]);
  });

  it('shows nothing for a record without a summary', () => {
    expect(shown(undefined)).toEqual([]);
    expect(shown({})).toEqual([]);
  });

  it('says when the findings were not answered and no range is left', () => {
    expect(
      finalReviewEnd({
        final_review: {
          outcome: 'findings',
          answered: false,
          answering_failed: true,
          open: { warn: 2 },
          complete: true,
          unreviewed: [],
          unreviewed_ranges: 0,
          findings: [
            { finding: 'fnd-1', severity: 'warn', status: 'open', recheck: 'not_rechecked' },
          ],
        },
      }),
    ).toEqual([
      'Open findings: 2 warn.',
      'The turn answering its findings failed. Findings: 1 not re-checked.',
    ]);
  });
});
