// SPDX-License-Identifier: MIT
import type { DocumentRevision } from './api';
import { commentAnchor } from './commentAnchors';

export interface ReaderAnchor {
  documentId: string;
  revision: number;
  block: string;
  kind: 'block' | 'math' | 'range';
  math: string;
  quote: string;
  /** Why a visible selection could only be attached to its block. */
  note: string;
}

function enclosing(node: Node, attribute: string): Element | null {
  const element = node instanceof Element ? node : node.parentElement;
  return element?.closest(`[${attribute}]`) ?? null;
}

/** Anchor a selection in the rendered document to the server's revision map. */
export function anchorFromSelection(
  selection: Selection | null,
  article: HTMLElement,
  found: DocumentRevision,
): ReaderAnchor | null {
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return null;
  const range = selection.getRangeAt(0);
  if (!article.contains(range.startContainer) || !article.contains(range.endContainer)) return null;
  const start = enclosing(range.startContainer, 'data-block');
  const end = enclosing(range.endContainer, 'data-block');
  const block = start?.getAttribute('data-block');
  if (!block || block !== end?.getAttribute('data-block')) return null;
  const item = found.blocks.find((candidate) => candidate.id === block);
  if (!item) return null;
  const origin = { documentId: found.document.document_id, revision: found.revision.number };

  const startMath = enclosing(range.startContainer, 'data-math')?.getAttribute('data-math');
  const endMath = enclosing(range.endContainer, 'data-math')?.getAttribute('data-math');
  if (startMath && startMath === endMath && item.math.some((span) => span.id === startMath)) {
    return { ...origin, block, kind: 'math', math: startMath, quote: '', note: '' };
  }

  const quote = selection.toString();
  if (quote && quote.length <= 4096) {
    try {
      commentAnchor(found, block, 'range', '', quote);
      return { ...origin, block, kind: 'range', math: '', quote, note: '' };
    } catch {
      // Rendered Markdown can normalize whitespace or show a formula differently from
      // source. Keep the user's selected block instead of sending a false range.
    }
  }
  return {
    ...origin,
    block,
    kind: 'block',
    math: '',
    quote: '',
    note: 'The selected text does not match a unique source range, so the comment will attach to this block.',
  };
}
