// SPDX-License-Identifier: MIT
import type { DocumentRevision } from './api';
import type { CommentDraft } from './drafts';
/** Python and the contract count Unicode code points, rather than UTF-16 units. */
export function commentAnchor(
  found: DocumentRevision,
  block: string,
  kind: string,
  math: string,
  quote: string,
): Record<string, unknown> {
  const item = found.blocks.find((item) => item.id === block);
  if (!item) throw new Error('Choose a block in the anchored revision.');
  const result: Record<string, unknown> = { revision: found.revision.number, block };
  if (kind === 'math') {
    if (!item.math.some((span) => span.id === math))
      throw new Error('Choose a formula in this block.');
    result.math = math;
  } else if (kind === 'range') {
    const text = item.source ?? '';
    const at = quote ? text.indexOf(quote) : -1;
    if (at < 0 || text.indexOf(quote, at + 1) >= 0)
      throw new Error('The quoted range must occur exactly once in the selected block.');
    const start = Array.from(text.slice(0, at)).length;
    const size = Array.from(quote).length;
    if (size > 2048) throw new Error('A range holds at most 2048 characters.');
    result.start = start;
    result.end = start + size;
  }
  return result;
}

/** A new comment's draft: the stored one, or an empty one anchored on `found`. */
export function startComment(found: DocumentRevision, stored: CommentDraft | null): CommentDraft {
  return (
    stored ?? {
      base: found.revision.number,
      block: found.blocks[0]?.id ?? '',
      body: '',
      kind: 'block',
      math: '',
      quote: '',
      session: '',
    }
  );
}

/** The draft anchored on the revision shown instead, at its first block. */
export function anchorOnShown(draft: CommentDraft, found: DocumentRevision): CommentDraft {
  const block = found.blocks[0]?.id ?? '';
  return { ...draft, base: found.revision.number, block, math: '', quote: '' };
}

/**
 * The change that creates the comment, anchored in `anchored` (the draft's base
 * revision); it throws when the anchor is not in that revision. A comment addressed to a
 * session queues a request for its main agent.
 */
export function newComment(
  anchored: DocumentRevision,
  draft: CommentDraft,
): Record<string, unknown> {
  const { block, kind, math, quote, body, session } = draft;
  return {
    action: 'create',
    anchor: commentAnchor(anchored, block, kind, math, quote),
    body,
    ...(session ? { addressed_to: session } : {}),
  };
}
