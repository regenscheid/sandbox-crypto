// SPDX-License-Identifier: MIT
/**
 * A comment thread's decisions, apart from its views (AnnotationThread.svelte,
 * CommentBodyEditor.svelte): the changes a reply, an edit, a status change or a body
 * correction sends, the drafts they start from, and the thread's earlier entries, read a
 * page at a time back from the oldest entry shown.
 */
import type { CommentBodySource, CommentChange, CommentEntry, CommentThread } from './api';
import type { BodyCorrectionDraft, ReplyDraft } from './drafts';
import { PagedList, type ListState } from './pagedList';

/**
 * The change an action on the thread sends. A reply or an edit carries the draft's text
 * and its base revision; resolving or reopening is based on the revision shown.
 */
export function threadChange(
  action: string,
  thread: CommentThread,
  draft: ReplyDraft,
): Record<string, unknown> {
  const writing = action === 'reply' || action === 'edit';
  const { body, session } = draft;
  return {
    action,
    thread: thread.thread_id,
    expected: writing ? draft.expected : thread.revision,
    ...(writing ? { body, ...(session ? { addressed_to: session } : {}) } : {}),
  };
}

/** The reply draft after its text was posted: empty, on the revision the post made. */
export function afterPost(draft: ReplyDraft, result: CommentChange): ReplyDraft {
  return { ...draft, body: '', expected: result.revision };
}

/** The entries to show: the earlier ones read, then the thread's, each once. */
export function threadEntries(earlier: CommentEntry[], current: CommentEntry[]): CommentEntry[] {
  const seen = new Set<string>();
  return [...earlier, ...current].filter((entry) => {
    if (seen.has(entry.entry_id)) return false;
    seen.add(entry.entry_id);
    return true;
  });
}

/**
 * A thread's earlier entries, a page at a time back from the oldest one shown. Each page
 * is one item, newest page first; `entries` puts them back in order. A new thread revision
 * starts again (`reset`), dropping a page still on its way.
 */
export class EarlierEntries {
  readonly list: PagedList<CommentEntry[], number>;

  constructor(
    readonly state: ListState<CommentEntry[], number>,
    thread: () => CommentThread,
    read: (before: number) => Promise<CommentThread>,
  ) {
    this.list = new PagedList(state, async (before) => {
      const start = before ?? thread().entries_before;
      if (start === null) return { items: [], next: null };
      const page = await read(start);
      return { items: [page.entries], next: page.entries_before };
    });
  }

  /** Whether there are earlier entries to read, as the thread shown says. */
  available(thread: CommentThread): boolean {
    return this.state.items.length === 0
      ? thread.entries_before !== null
      : this.state.next !== null;
  }

  /** The next earlier page. */
  read(): Promise<void> {
    return this.state.items.length === 0 ? this.list.reload() : this.list.more();
  }

  /** The earlier entries read so far, oldest first. */
  entries(): CommentEntry[] {
    return [...this.state.items].reverse().flat();
  }

  reset(): void {
    this.list.clear();
  }
}

/** A body correction's draft, on the body and thread revisions shown. */
export function correctionDraft(source: CommentBodySource, expected: number): BodyCorrectionDraft {
  return { expected, body: source.body ?? '', base: source.revision!, sha256: source.sha256! };
}

/** Whether the draft is based on an earlier body or thread revision than the one shown. */
export function correctionOutdated(
  draft: BodyCorrectionDraft,
  source: CommentBodySource,
  expected: number,
): boolean {
  return (
    draft.expected !== expected || draft.base !== source.revision || draft.sha256 !== source.sha256
  );
}

/** The change that corrects an original entry's body. */
export function bodyCorrection(
  thread: string,
  entry: string,
  draft: BodyCorrectionDraft,
): Record<string, unknown> {
  const { expected, base, sha256, body } = draft;
  return { action: 'edit_body', thread, entry, expected, base, sha256, body };
}
