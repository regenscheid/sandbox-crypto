// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { DocumentRevision } from './api';
import { documentAction, draftChange, resolveAgainst, saved, startDraft } from './documentEdit';

function revision(number: number, text: string, blocks: [string, string][]): DocumentRevision {
  return {
    document: { document_id: 'doc-1', head: number, name: 'notes' },
    revision: { number },
    text,
    blocks: blocks.map(([id, source]) => ({ id, source })),
  } as unknown as DocumentRevision;
}

const first = revision(3, 'A\n\nB', [
  ['b1', 'A'],
  ['b2', 'B'],
]);

describe('document editing', () => {
  it('starts a source draft or a block draft on the shown revision', () => {
    expect(startDraft(first, null)).toEqual({
      base: 3,
      text: 'A\n\nB',
      reason: 'User edit',
      block: null,
      retained: '',
    });
    expect(startDraft(first, 'b2')).toMatchObject({ base: 3, text: 'B', block: 'b2' });
    expect(startDraft(first, 'gone').text).toBe('');
  });
  it('sends a source edit, or a patch that replaces only its block', () => {
    expect(draftChange({ ...startDraft(first, null), text: 'new' })).toEqual({
      action: 'source',
      base: 3,
      reason: 'User edit',
      text: 'new',
    });
    expect(draftChange({ ...startDraft(first, 'b1'), text: 'A2' })).toEqual({
      action: 'patch',
      base: 3,
      reason: 'User edit',
      operations: [{ op: 'replace', block: 'b1', markdown: 'A2' }],
    });
  });
  it('ends the draft only when the change was applied or changed nothing', () => {
    for (const outcome of ['applied', 'unchanged']) expect(saved({ outcome } as never)).toBe(true);
    for (const outcome of ['conflict', 'proposed']) expect(saved({ outcome } as never)).toBe(false);
  });
  it('resolves a block conflict on the same block, keeping the user text visible', () => {
    const newer = revision(5, 'A\n\nB changed', [
      ['b1', 'A'],
      ['b2', 'B changed'],
    ]);
    const draft = { ...startDraft(first, 'b1'), text: 'A mine' };
    expect(resolveAgainst(draft, newer)).toEqual({
      ...draft,
      base: 5,
      block: 'b1',
      text: 'A mine',
      retained: 'A mine',
    });
  });
  it('resolves a source draft, or one whose block is gone, from the current source', () => {
    const newer = revision(6, 'A\n\nC', [
      ['b1', 'A'],
      ['b3', 'C'],
    ]);
    for (const draft of [
      { ...startDraft(first, null), text: 'old whole text' },
      { ...startDraft(first, 'b2'), text: 'B mine' },
    ]) {
      const resolved = resolveAgainst(draft, newer);
      expect(resolved).toMatchObject({ base: 6, block: null, text: 'A\n\nC' });
      expect(resolved.retained).toBe(draft.text);
    }
  });
  it('promotes or restores against the document head', () => {
    const shown = revision(2, 'old', []);
    (shown.document as { head: number }).head = 4;
    expect(documentAction(shown, 'promote', 'notes')).toEqual({
      action: 'promote',
      base: 4,
      reason: 'Move workbook document to project',
      name: 'notes',
    });
    expect(documentAction(shown, 'revert', 'notes')).toEqual({
      action: 'revert',
      base: 4,
      reason: 'Restore revision 2',
      target: 2,
    });
  });
});
