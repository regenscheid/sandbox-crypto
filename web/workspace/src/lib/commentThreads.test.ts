// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { CommentBodySource, CommentEntry, CommentThread } from './api';
import {
  afterPost,
  bodyCorrection,
  correctionDraft,
  correctionOutdated,
  EarlierEntries,
  threadChange,
  threadEntries,
} from './commentThreads';
import { emptyList } from './pagedList';

const entry = (id: string) => ({ entry_id: id }) as CommentEntry;
const thread = (revision: number, entriesBefore: number | null, entries: string[] = []) =>
  ({
    thread_id: 't1',
    revision,
    entries_before: entriesBefore,
    entries: entries.map(entry),
  }) as unknown as CommentThread;
const draft = { expected: 4, body: 'Thanks', mode: 'reply' as const, session: 's1' };

describe('a comment thread', () => {
  it("sends a reply or an edit on the draft's revision, in this order", () => {
    expect(JSON.stringify(threadChange('reply', thread(5, null), draft))).toBe(
      '{"action":"reply","thread":"t1","expected":4,"body":"Thanks","addressed_to":"s1"}',
    );
    expect(JSON.stringify(threadChange('edit', thread(5, null), { ...draft, session: '' }))).toBe(
      '{"action":"edit","thread":"t1","expected":4,"body":"Thanks"}',
    );
  });
  it('resolves or reopens on the revision shown, without the draft', () => {
    expect(JSON.stringify(threadChange('resolve', thread(5, null), draft))).toBe(
      '{"action":"resolve","thread":"t1","expected":5}',
    );
  });
  it('empties the draft after a post, on the revision the post made', () => {
    const result = { revision: 6 } as never;
    expect(afterPost(draft, result)).toEqual({ ...draft, body: '', expected: 6 });
  });
  it('shows each entry once, earlier ones first', () => {
    const shown = threadEntries([entry('a'), entry('b')], [entry('b'), entry('c')]);
    expect(shown.map((item) => item.entry_id)).toEqual(['a', 'b', 'c']);
  });
});

describe("a thread's earlier entries", () => {
  function earlier(current: CommentThread) {
    const asked: number[] = [];
    const pages: { resolve: (thread: CommentThread) => void }[] = [];
    const state = emptyList<CommentEntry[], number>();
    const read = (before: number) => {
      asked.push(before);
      return new Promise<CommentThread>((resolve) => pages.push({ resolve }));
    };
    return { asked, pages, state, history: new EarlierEntries(state, () => current, read) };
  }

  it('reads back from the oldest entry shown, a page at a time, oldest first', async () => {
    const { asked, pages, history } = earlier(thread(5, 10, ['e10']));
    expect(history.available(thread(5, 10))).toBe(true);
    const first = history.read();
    pages[0]!.resolve(thread(5, 8, ['e8', 'e9']));
    await first;
    const second = history.read();
    pages[1]!.resolve(thread(5, null, ['e6', 'e7']));
    await second;
    expect(asked).toEqual([10, 8]);
    expect(history.entries().map((item) => item.entry_id)).toEqual(['e6', 'e7', 'e8', 'e9']);
    expect(history.available(thread(5, 10))).toBe(false);
  });

  it('has nothing to read when the thread shows its first entry', () => {
    const { history } = earlier(thread(5, null));
    expect(history.available(thread(5, null))).toBe(false);
  });

  it('starts again for a new thread revision, dropping a page on its way', async () => {
    const { pages, history } = earlier(thread(5, 10));
    const late = history.read();
    history.reset();
    pages[0]!.resolve(thread(5, 8, ['e8']));
    await late;
    expect(history.entries()).toEqual([]);
  });
});

describe("a correction of a comment's body", () => {
  const source = {
    entry_id: 'cmt-1',
    revision: 3,
    sha256: 'a'.repeat(64),
    body: 'Current $x$.',
  } as CommentBodySource;

  it('starts from the body and revisions shown', () => {
    expect(correctionDraft(source, 7)).toEqual({
      expected: 7,
      body: 'Current $x$.',
      base: 3,
      sha256: 'a'.repeat(64),
    });
  });
  it('is outdated when the thread or the body moved on', () => {
    const kept = correctionDraft(source, 7);
    expect(correctionOutdated(kept, source, 7)).toBe(false);
    expect(correctionOutdated(kept, source, 8)).toBe(true);
    expect(correctionOutdated(kept, { ...source, revision: 4 }, 7)).toBe(true);
    expect(correctionOutdated(kept, { ...source, sha256: 'b'.repeat(64) }, 7)).toBe(true);
  });
  it('sends the correction with both bases, in this order', () => {
    const sent = bodyCorrection('t1', 'cmt-1', { ...correctionDraft(source, 7), body: 'New' });
    expect(JSON.stringify(sent)).toBe(
      `{"action":"edit_body","thread":"t1","entry":"cmt-1","expected":7,"base":3,"sha256":"${'a'.repeat(64)}","body":"New"}`,
    );
  });
});
