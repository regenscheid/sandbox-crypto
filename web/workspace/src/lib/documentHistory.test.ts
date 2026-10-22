// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { PatchPreview, PatchRecord } from './api';
import {
  applyPatch,
  compareRevisions,
  declineSuggestion,
  newestFirst,
  patchApplied,
  patchState,
  reviewable,
  summarizeChanges,
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

describe('what changed between revisions', () => {
  const block = (id: string, kind: string, source: string) => ({ id, kind, source });
  const before = {
    blocks: [
      block('b1', 'heading', '# Scope'),
      block('b2', 'paragraph', 'The rank bounds the cost.'),
      block('b3', 'paragraph', 'An old remark.'),
      block('b4', 'heading', '## Proof'),
      block('b5', 'paragraph', 'Kept as it was.'),
      block('b6', 'paragraph', 'Also kept.'),
    ],
  };
  const after = {
    blocks: [
      block('b1', 'heading', '# Scope'),
      block('b2', 'paragraph', 'The rank bounds the attack cost.'),
      block('b4', 'heading', '## Proof'),
      block('b7', 'math', '$$x^2$$'),
      block('b5', 'paragraph', 'Kept as it was.'),
      block('b6', 'paragraph', 'Also kept.'),
    ],
  };

  it('lists edits, additions and removals where they stand, under their headings', () => {
    const changes = compareRevisions(before, after)!;
    expect(changes.map((item) => item.kind)).toEqual([
      'same',
      'changed',
      'removed',
      'same',
      'added',
      'same',
    ]);
    const edited = changes[1]!;
    expect(edited).toMatchObject({ passage: 'paragraph', section: 'Scope' });
    const added = 'diff' in edited ? edited.diff?.filter((part) => part.kind === 'added') : [];
    expect(added?.map((part) => part.text).join('')).toBe('attack ');
    expect(changes[2]).toMatchObject({ before: 'An old remark.', section: 'Scope' });
    expect(changes[4]).toMatchObject({ passage: 'math', section: 'Proof', after: '$$x^2$$' });
    expect(changes[5]).toEqual({ kind: 'same', count: 2 });
  });

  it('marks moved passages only when told, and gives up without the blocks text', () => {
    expect(compareRevisions(after, after, ['b5'])!.map((item) => item.kind)).toEqual([
      'same',
      'moved',
      'same',
    ]);
    expect(compareRevisions({ blocks: [{ id: 'b1', kind: 'paragraph' }] }, after)).toBeNull();
  });

  it('says how much a revision changed in words, without block names', () => {
    const revision = {
      tombstone: false,
      parent: 3,
      changed: ['b2', 'b3'],
      added: ['b9'],
      moved: [],
      removed: [],
    };
    expect(summarizeChanges(revision)).toBe('Edited 2 passages, added 1');
    expect(summarizeChanges({ ...revision, changed: [], added: [], removed: ['b1'] })).toBe(
      'Removed 1 passage',
    );
    expect(summarizeChanges({ ...revision, parent: null })).toBe('First revision');
    expect(summarizeChanges({ ...revision, tombstone: true })).toBe('Deleted the document');
    expect(summarizeChanges({ ...revision, changed: [], added: [] })).toBe('No change to the text');
    expect(patchState('suggested')).toBe('Waiting for you');
  });
});
