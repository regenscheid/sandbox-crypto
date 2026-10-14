// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { EvidenceText } from './evidenceText';
import { emptyList } from './pagedList';

describe('a pinned evidence text, read where it was asked for', () => {
  it('reads slice by slice, and keeps the text shown when another fails to load', async () => {
    const asked: [string, number][] = [];
    const texts: Record<string, string[]> = { e1: ['abc', 'def'] };
    const state = emptyList<string, number>();
    const reader = new EvidenceText(state, async (_review, evidence, start) => {
      asked.push([evidence, start]);
      const slices = texts[evidence];
      if (!slices) throw new Error('unavailable');
      const index = start === 0 ? 0 : 1;
      return { text: slices[index]!, next_start: index === 0 ? 3 : null };
    });
    await reader.read('r', 'e1');
    await reader.read('r', 'e2'); // fails: e1 stays, and its next slice continues it
    expect(state.error).not.toBeNull();
    await reader.list.more();
    expect(asked).toEqual([
      ['e1', 0],
      ['e2', 0],
      ['e1', 3],
    ]);
    expect(reader.text()).toBe('abcdef');
  });
});
