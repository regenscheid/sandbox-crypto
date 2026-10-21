// SPDX-License-Identifier: MIT
/**
 * The document history's decisions, apart from its views (HistoryView.svelte,
 * PatchReview.svelte): the order it lists things in, which patches the user may review,
 * and the change that applies a reviewed patch.
 */
import type {
  DocumentChange,
  DocumentRevision,
  HistoryRevision,
  PatchPreview,
  PatchRecord,
} from './api';
import { textDiff, type Change } from './approval';

/** Newest first: the history lists revisions and patches as the store keeps them, oldest first. */
export function newestFirst<T>(items: readonly T[]): T[] {
  return [...items].reverse();
}

/** A patch the store did not apply on its own, which the user may review and apply. */
export function reviewable(patch: PatchRecord): boolean {
  return ['conflicted', 'refused', 'suggested'].includes(patch.state);
}

/** The change that applies a previewed patch against the revision it was previewed on. */
export function applyPatch(preview: PatchPreview): Record<string, unknown> {
  const patchId = preview.patch.patch_id;
  return {
    action: 'apply_patch',
    patch_id: patchId,
    base: preview.base,
    reason: `User reviewed and applied patch ${patchId}`,
  };
}

/** Whether applying ended the review: the patch is in, or there was nothing to change. */
export function patchApplied(result: DocumentChange): boolean {
  return result.outcome === 'accepted' || result.outcome === 'unchanged';
}

/** The change that declines an agent's suggestion: the document stays as it is (LD-20). */
export function declineSuggestion(patchId: string): Record<string, unknown> {
  return { action: 'decline', patch_id: patchId };
}

/** A revision's changes in words, for the history's list: `Edited 2 passages, added 1`. */
export function summarizeChanges(
  revision: Pick<
    HistoryRevision,
    'tombstone' | 'parent' | 'changed' | 'added' | 'moved' | 'removed'
  >,
): string {
  if (revision.tombstone) return 'Deleted the document';
  if (revision.parent === null) return 'First revision';
  const counts = (
    [
      ['edited', revision.changed.length],
      ['added', revision.added.length],
      ['moved', revision.moved.length],
      ['removed', revision.removed.length],
    ] as const
  ).filter(([, count]) => count > 0);
  if (counts.length === 0) return 'No change to the text';
  const words = counts.map(([verb, count], index) =>
    index === 0 ? `${verb} ${count} ${count === 1 ? 'passage' : 'passages'}` : `${verb} ${count}`,
  );
  const text = words.join(', ');
  return text[0]!.toUpperCase() + text.slice(1);
}

/** What the state of an agent's patch means for the user. */
export function patchState(state: string): string {
  const words: Record<string, string> = {
    suggested: 'Waiting for you',
    accepted: 'Applied',
    applied: 'Applied',
    declined: 'Declined',
    conflicted: 'Conflicts with later edits',
    refused: 'Could not apply',
    pending: 'Pending',
  };
  return words[state] ?? state;
}

type Block = Pick<DocumentRevision['blocks'][number], 'id' | 'kind' | 'source'>;

/** One passage that differs between two revisions, under the heading it falls in. */
export interface PassageChange {
  kind: 'added' | 'removed' | 'changed' | 'moved';
  /** The block's kind: `paragraph`, `heading`, `math`, …. */
  passage: string;
  /** The heading the passage falls under, or null before the first heading. */
  section: string | null;
  before: string;
  after: string;
  /** Word by word, for an edited passage; null when it is too long to compare. */
  diff: Change[] | null;
}

/** Passages that did not change, folded into a count between the changes. */
export interface Unchanged {
  kind: 'same';
  count: number;
}

const headingText = (source: string) =>
  source
    .replace(/^#{1,6}\s+/, '')
    .replace(/\s+#+\s*$/, '')
    .trim();

/**
 * The passages that differ from `before` to `after`, in the order `after` reads, each
 * removed one where it stood; unchanged runs are folded. Blocks keep their IDs across
 * revisions (LD-6), so a passage is matched by its ID. `moved` names the blocks the later
 * revision moved, when `before` is the revision just before it. Null when a revision's
 * blocks came without their text (an older server): compare the whole texts instead.
 */
export function compareRevisions(
  before: { blocks: readonly Block[] },
  after: { blocks: readonly Block[] },
  moved: readonly string[] = [],
): (PassageChange | Unchanged)[] | null {
  if ([...before.blocks, ...after.blocks].some((block) => typeof block.source !== 'string'))
    return null;
  const earlier = new Map(before.blocks.map((block) => [block.id, block]));
  const kept = new Set(after.blocks.map((block) => block.id));
  // Each removed passage, after the nearest earlier passage that remains ('' at the start).
  const removed = new Map<string, PassageChange[]>();
  let anchor = '';
  let section: string | null = null;
  for (const block of before.blocks) {
    if (block.kind === 'heading') section = headingText(block.source!);
    if (kept.has(block.id)) {
      anchor = block.id;
      continue;
    }
    const list = removed.get(anchor) ?? [];
    list.push({
      kind: 'removed',
      passage: block.kind,
      section,
      before: block.source!,
      after: '',
      diff: null,
    });
    removed.set(anchor, list);
  }
  const items: (PassageChange | Unchanged)[] = [];
  let same = 0;
  const push = (change: PassageChange) => {
    if (same > 0) items.push({ kind: 'same', count: same });
    same = 0;
    items.push(change);
  };
  for (const change of removed.get('') ?? []) push(change);
  section = null;
  for (const block of after.blocks) {
    if (block.kind === 'heading') section = headingText(block.source!);
    const old = earlier.get(block.id);
    const now = block.source!;
    if (!old)
      push({ kind: 'added', passage: block.kind, section, before: '', after: now, diff: null });
    else if (old.source !== now)
      push({
        kind: 'changed',
        passage: block.kind,
        section,
        before: old.source!,
        after: now,
        diff: textDiff(old.source!, now),
      });
    else if (moved.includes(block.id))
      push({ kind: 'moved', passage: block.kind, section, before: now, after: now, diff: null });
    else same += 1;
    for (const change of removed.get(block.id) ?? []) push(change);
  }
  if (same > 0) items.push({ kind: 'same', count: same });
  return items;
}
