// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { commentAnchor } from './commentAnchors';
import type { DocumentRevision } from './api';
const found = { revision: { number: 3 }, blocks: [
  { id: 'b1', source: '😀 For every $x$.', math: [{ id: 'm1', tex: 'x' }] },
  { id: 'b2', source: 'Again again again', math: [] },
] } as unknown as DocumentRevision;
describe('comment anchors', () => {
  it('counts range offsets in code points and keeps revision/block identity', () => {
    expect(commentAnchor(found, 'b1', 'range', '', 'For every')).toEqual({ revision: 3, block: 'b1', start: 2, end: 11 });
  });
  it('requires the exact formula to belong to this block', () => {
    expect(commentAnchor(found, 'b1', 'math', 'm1', '')).toEqual({ revision: 3, block: 'b1', math: 'm1' });
    expect(() => commentAnchor(found, 'b2', 'math', 'm1', '')).toThrow('Choose a formula');
  });
  it('refuses missing, repeated or oversized ranges and missing blocks', () => {
    expect(() => commentAnchor(found, 'missing', 'block', '', '')).toThrow('Choose a block');
    expect(() => commentAnchor(found, 'b2', 'range', '', 'again')).toThrow('exactly once');
    expect(() => commentAnchor(found, 'b1', 'range', '', '')).toThrow('exactly once');
    const long = { revision: { number: 1 }, blocks: [{ id: 'b1', source: 'x'.repeat(2049), math: [] }] } as unknown as DocumentRevision;
    expect(() => commentAnchor(long, 'b1', 'range', '', 'x'.repeat(2049))).toThrow('2048');
  });
});
