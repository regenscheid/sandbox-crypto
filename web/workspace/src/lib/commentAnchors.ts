// SPDX-License-Identifier: MIT
import type { DocumentRevision } from './api';
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
