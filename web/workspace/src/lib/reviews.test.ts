// SPDX-License-Identifier: MIT
import { describe, it, expect } from 'vitest';
import { finalReviewLine, parseRevisionReferences } from './reviews';
describe('changed review evidence references', () => {
  it('retains exact document revisions and accepts an empty list', () => {
    const id = `doc-${'a'.repeat(24)}`;
    expect(parseRevisionReferences(`${id}:1 ${id}:42`)).toEqual([
      [id, 1],
      [id, 42],
    ]);
    expect(parseRevisionReferences('')).toEqual([]);
  });
  it.each([
    'doc-x:1',
    `doc-${'a'.repeat(24)}:0`,
    `doc-${'a'.repeat(24)}:1.5`,
    '../escape:1',
    `doc-${'a'.repeat(24)}:1000000000`,
  ])('refuses malformed or unbounded references %s', (value) =>
    expect(() => parseRevisionReferences(value)).toThrow(),
  );
  it('bounds the number of revision pins', () =>
    expect(() =>
      parseRevisionReferences(
        Array(17)
          .fill(`doc-${'a'.repeat(24)}:1`)
          .join(' '),
      ),
    ).toThrow());
});

describe("a run's final review line", () => {
  it('says how the review ended, what is open and what is left unreviewed', () => {
    expect(
      finalReviewLine({
        mode: 'after',
        outcome: 'findings',
        finished: true,
        answered: true,
        complete: false,
        open: { fail: 1, warn: 0 },
        fixes_claimed: 1,
        reviewed_through: 96,
        feed_end: 101,
        items: { total: 90, reviewed: 60, unreviewed: 30, not_recorded: 0 },
        unreviewed: [
          { start: 1, end: 40 },
          { start: 97, end: 101 },
        ],
        unreviewed_ranges: 3,
      }),
    ).toBe(
      'Final review: findings, after the answer; the agent answered its findings. ' +
        'Open: 1 fail. 1 fix claimed, its re-check pending. ' +
        'Unreviewed: positions 1–40, 97–101 and 1 more range (60 of 90 items reviewed whole).',
    );
    expect(finalReviewLine({ mode: 'hold', finished: false, complete: true, open: {} })).toBe(
      'Final review: did not finish, before the answer. No open findings. All work reviewed.',
    );
    expect(
      finalReviewLine({
        mode: 'hold',
        outcome: 'findings',
        finished: true,
        answering_failed: true,
        complete: true,
        open: { fail: 1 },
        not_recorded_through: 12,
      }),
    ).toBe(
      'Final review: findings, before the answer; the turn answering its findings failed, ' +
        'so they were not answered. Open: 1 fail. All work reviewed. ' +
        'Coverage is not recorded through position 12.',
    );
    expect(finalReviewLine(undefined)).toBeNull();
  });
});
