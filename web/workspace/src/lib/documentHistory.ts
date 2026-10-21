// SPDX-License-Identifier: MIT
/**
 * The document history's decisions, apart from its views (HistoryView.svelte,
 * PatchReview.svelte): the order it lists things in, which patches the user may review,
 * and the change that applies a reviewed patch.
 */
import type { DocumentChange, PatchPreview, PatchRecord } from './api';

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
