// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { anchorOnShown, commentAnchor, newComment, startComment } from './commentAnchors';
import type { DocumentRevision } from './api';
const found = {
  revision: { number: 3 },
  blocks: [
    { id: 'b1', source: '😀 For every $x$.', math: [{ id: 'm1', tex: 'x' }] },
    { id: 'b2', source: 'Again again again', math: [] },
  ],
} as unknown as DocumentRevision;
describe('comment anchors', () => {
  it('counts range offsets in code points and keeps revision/block identity', () => {
    expect(commentAnchor(found, 'b1', 'range', '', 'For every')).toEqual({
      revision: 3,
      block: 'b1',
      start: 2,
      end: 11,
    });
  });
  it('requires the exact formula to belong to this block', () => {
    expect(commentAnchor(found, 'b1', 'math', 'm1', '')).toEqual({
      revision: 3,
      block: 'b1',
      math: 'm1',
    });
    expect(() => commentAnchor(found, 'b2', 'math', 'm1', '')).toThrow('Choose a formula');
  });
  it('refuses missing, repeated or oversized ranges and missing blocks', () => {
    expect(() => commentAnchor(found, 'missing', 'block', '', '')).toThrow('Choose a block');
    expect(() => commentAnchor(found, 'b2', 'range', '', 'again')).toThrow('exactly once');
    expect(() => commentAnchor(found, 'b1', 'range', '', '')).toThrow('exactly once');
    const long = {
      revision: { number: 1 },
      blocks: [{ id: 'b1', source: 'x'.repeat(2049), math: [] }],
    } as unknown as DocumentRevision;
    expect(() => commentAnchor(long, 'b1', 'range', '', 'x'.repeat(2049))).toThrow('2048');
  });
});

describe('a new comment', () => {
  const shown = {
    ...found,
    revision: { number: 5 },
    document: { session_id: 's1' },
  } as unknown as DocumentRevision;
  it("starts on the shown revision's first block without sending a work request", () => {
    expect(startComment(shown, null)).toEqual({
      base: 5,
      block: 'b1',
      body: '',
      kind: 'block',
      math: '',
      quote: '',
      session: '',
    });
    const stored = { ...startComment(shown, null), body: 'kept', base: 3 };
    expect(startComment(shown, stored)).toBe(stored);
  });
  it('moves its anchor to the shown revision, keeping its text and address', () => {
    const draft = { ...startComment(shown, null), base: 3, block: 'b2', math: 'm1', quote: 'q' };
    expect(anchorOnShown({ ...draft, body: 'text' }, shown)).toEqual({
      ...draft,
      body: 'text',
      base: 5,
      block: 'b1',
      math: '',
      quote: '',
    });
  });
  it('sends the anchor in its own revision, and the session only when addressed', () => {
    const draft = { ...startComment(shown, null), base: 3, body: 'Why?', session: 's1' };
    expect(JSON.stringify(newComment(found, draft))).toBe(
      '{"action":"create","anchor":{"revision":3,"block":"b1"},"body":"Why?","addressed_to":"s1"}',
    );
    expect(newComment(found, { ...draft, session: '' })).not.toHaveProperty('addressed_to');
    expect(() => newComment(found, { ...draft, block: 'gone' })).toThrow('Choose a block');
  });
});
