// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { PatchPreview, PatchRecord } from './api';
import {
  applyPatch,
  declineSuggestion,
  newestFirst,
  patchApplied,
  reviewable,
} from './documentHistory';

describe('the document history', () => {
  it('lists newest first without changing what the store sent', () => {
    const kept = [1, 2, 3];
    expect(newestFirst(kept)).toEqual([3, 2, 1]);
    expect(kept).toEqual([1, 2, 3]);
  });
  it('offers review only of patches the store did not apply on its own', () => {
    for (const state of ['conflicted', 'refused', 'suggested'])
      expect(reviewable({ state } as PatchRecord)).toBe(true);
    for (const state of ['accepted', 'pending', 'declined'])
      expect(reviewable({ state } as PatchRecord)).toBe(false);
  });
  it('applies a reviewed patch against the revision it was previewed on, in this order', () => {
    const preview = { patch: { patch_id: 'p-7' }, base: 4 } as PatchPreview;
    expect(JSON.stringify(applyPatch(preview))).toBe(
      '{"action":"apply_patch","patch_id":"p-7","base":4,' +
        '"reason":"User reviewed and applied patch p-7"}',
    );
  });
  it('declines a suggestion by its ID alone, as the strict change schema takes it', () => {
    expect(JSON.stringify(declineSuggestion('patch-1'))).toBe(
      '{"action":"decline","patch_id":"patch-1"}',
    );
  });
  it('ends the review when the patch is in or there was nothing to change', () => {
    for (const outcome of ['accepted', 'unchanged'])
      expect(patchApplied({ outcome } as never)).toBe(true);
    for (const outcome of ['conflicted', 'refused'])
      expect(patchApplied({ outcome } as never)).toBe(false);
  });
});
