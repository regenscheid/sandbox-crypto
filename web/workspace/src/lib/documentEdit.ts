// SPDX-License-Identifier: MIT
/**
 * The document editor's decisions, apart from the view (DocumentEditor.svelte): what a
 * new draft holds, the change a draft sends, when a save is done, and how a conflict is
 * resolved against the current revision.
 */
import type { DocumentChange, DocumentRevision } from './api';
import type { DocumentDraft } from './drafts';

/** A draft of the whole source (`block` null) or of one block, based on `found`. */
export function startDraft(
  found: DocumentRevision,
  block: string | null,
  reason = 'User edit',
): DocumentDraft {
  const text =
    block === null ? found.text : (found.blocks.find((item) => item.id === block)?.source ?? '');
  return { base: found.revision.number, text, reason, block, retained: '' };
}

/** The change a draft sends: a source edit, or a patch that replaces its block. */
export function draftChange(draft: DocumentDraft): Record<string, unknown> {
  const { base, reason, block, text } = draft;
  return block === null
    ? { action: 'source', base, reason, text }
    : { action: 'patch', base, reason, operations: [{ op: 'replace', block, markdown: text }] };
}

/** Whether a save's outcome ends the draft; any other outcome is a conflict to resolve. */
export function saved(result: DocumentChange): boolean {
  return result.outcome === 'applied' || result.outcome === 'unchanged';
}

/**
 * The draft rebased on `current`, keeping the user's text visible in `retained`. A block
 * draft stays on its block when the block still exists. A source draft, or one whose
 * block is gone, starts again from the current source, so that newer changes to other
 * blocks are never overwritten by the old full text.
 */
export function resolveAgainst(draft: DocumentDraft, current: DocumentRevision): DocumentDraft {
  const keep = draft.block !== null && current.blocks.some((item) => item.id === draft.block);
  return {
    ...draft,
    base: current.revision.number,
    retained: draft.text,
    block: keep ? draft.block : null,
    text: keep ? draft.text : current.text,
  };
}

/** Moving a workbook document to the project, or restoring the revision `found` shows. */
export function documentAction(
  found: DocumentRevision,
  action: 'promote' | 'revert',
  name: string,
): Record<string, unknown> {
  const base = found.document.head;
  return action === 'promote'
    ? { action, base, reason: 'Move workbook document to project', name }
    : {
        action,
        base,
        reason: `Restore revision ${found.revision.number}`,
        target: found.revision.number,
      };
}
