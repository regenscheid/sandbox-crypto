// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { Earlier } from './earlier';
import { afterResponse, findingHistory, findingResponse, startResponse } from './findings';
import { emptyList } from './pagedList';
import type { Finding, Transition } from './reviews';

const finding = { finding_id: 'f1', revision: 4 } as Finding;

describe("a user's response to a finding", () => {
  it("starts as an empty rebuttal on the finding's revision, unless a draft was kept", () => {
    expect(startResponse(finding, null)).toEqual({
      expected: 4,
      explanation: '',
      action: 'rebut',
      references: '',
    });
    const kept = { expected: 2, explanation: 'Kept', action: 'fix', references: '' };
    expect(startResponse(finding, kept)).toBe(kept);
  });
  it('sends the decision with its revision references, in this order', () => {
    const draft = {
      expected: 4,
      explanation: 'Fixed',
      action: 'fix',
      references: 'doc-0123456789abcdef01234567:3',
    };
    expect(JSON.stringify(findingResponse(finding, draft))).toBe(
      '{"finding":"f1","expected":4,"action":"fix","explanation":"Fixed","revisions":[["doc-0123456789abcdef01234567",3]]}',
    );
  });
  it('refuses a reference that names no revision', () => {
    const draft = { expected: 4, explanation: 'Fixed', action: 'fix', references: 'doc-1' };
    expect(() => findingResponse(finding, draft)).toThrow();
  });
  it('empties the draft after saving, on the revision it made, keeping the decision', () => {
    const draft = {
      expected: 4,
      explanation: 'Fixed',
      action: 'fix',
      references: 'doc-0123456789abcdef01234567:3',
    };
    expect(afterResponse(draft, { revision: 5 })).toEqual({
      expected: 5,
      explanation: '',
      action: 'fix',
      references: '',
    });
  });
});

describe("a finding's history, read back", () => {
  it('reads the newest page first, then each earlier one, and shows them oldest first', async () => {
    const asked: (number | null)[] = [];
    const answers = new Map<number | null, Finding>([
      [null, { history: [{ revision: 3 }, { revision: 4 }], history_before: 3 } as Finding],
      [3, { history: [{ revision: 1 }, { revision: 2 }], history_before: null } as Finding],
    ]);
    const state = emptyList<Transition[], number>();
    const history = new Earlier(
      state,
      findingHistory(async (before) => {
        asked.push(before);
        return answers.get(before)!;
      }),
    );
    expect([history.started, history.available()]).toEqual([false, true]);
    await history.read();
    await history.read();
    expect(asked).toEqual([null, 3]);
    expect(history.entries().map((item) => item.revision)).toEqual([1, 2, 3, 4]);
    expect(history.available()).toBe(false);
    history.reset();
    expect([history.started, history.entries()]).toEqual([false, []]);
  });
});
