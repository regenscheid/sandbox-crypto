// SPDX-License-Identifier: MIT
/**
 * A comment thread's decisions, apart from its views (AnnotationThread.svelte,
 * CommentBodyEditor.svelte): the changes a reply, an edit, a status change or a body
 * correction sends, the drafts they start from, and where its earlier entries start.
 */
import type { CommentBodySource, CommentChange, CommentEntry, CommentThread } from './api';
import type { BodyCorrectionDraft, ReplyDraft } from './drafts';
import type { Page } from './pagedList';

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
 * The page of a thread's entries before `before`, for `Earlier` (earlier.ts): the first
 * page back starts at the oldest entry the thread shows, and a thread that shows its first
 * entry has none.
 */
export function earlierEntries(
  thread: () => CommentThread,
  read: (before: number) => Promise<CommentThread>,
): (before: number | null) => Promise<Page<CommentEntry[], number>> {
  return async (before) => {
    const start = before ?? thread().entries_before;
    if (start === null) return { items: [], next: null };
    const page = await read(start);
    return { items: [page.entries], next: page.entries_before };
  };
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
