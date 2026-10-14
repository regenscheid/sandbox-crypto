// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { PollPhase } from './poller';
import { holdReason, pageBack, pageForward, phaseLabel } from './provisionView';

describe('the Environments panel', () => {
  it('says something for every phase but observed', () => {
    const phases: PollPhase[] = [
      'loading',
      'paused',
      'unavailable',
      'unsupported',
      'missing',
      'changed',
      'signed_out',
    ];
    for (const phase of phases) expect(phaseLabel(phase)).not.toBe('');
    expect(phaseLabel('observed')).toBe('');
  });
  it('names each held input, and an unknown one as such', () => {
    expect(holdReason('blob_missing')).toBe('The recorded lockfile is unavailable or evicted');
    expect(holdReason('something_new')).toBe('Unknown input');
  });
  it("takes no label from an object's inherited names", () => {
    for (const name of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
      expect(holdReason(name)).toBe('Unknown input');
      expect(phaseLabel(name as PollPhase)).toBe('');
    }
  });
  it('pages forward by the next offset and back to exactly the page before', () => {
    const first = pageForward([], { offset: 0, next_offset: 50 });
    expect(first).toEqual({ offset: 50, trail: [0] });
    const second = pageForward(first!.trail, { offset: 50, next_offset: 73 }); // pages differ in size
    expect(second).toEqual({ offset: 73, trail: [0, 50] });
    expect(pageBack(second!.trail)).toEqual({ offset: 50, trail: [0] });
    expect(pageBack([0])).toEqual({ offset: 0, trail: [] });
  });
  it('has no page after the last, nor before the first', () => {
    expect(pageForward([0], { offset: 50, next_offset: null })).toBeNull();
    expect(pageBack([])).toBeNull();
  });
});
