// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { MacroContext } from './api';
import { adoptCurrent, macroChange, savedDraft } from './macroSettings';

const current = { revision: 4, macros: { '\\R': '\\mathbb{R}' } } as unknown as MacroContext;

describe('the project macro editor', () => {
  it('starts an empty draft from the current setting', () => {
    expect(adoptCurrent({ expected: 0, text: '' }, current)).toEqual({
      expected: 4,
      text: '{\n  "\\\\R": "\\\\mathbb{R}"\n}',
    });
  });
  it('keeps a draft with text on its own base, so a newer revision is a conflict', () => {
    const draft = { expected: 2, text: '{}' };
    expect(adoptCurrent(draft, current)).toBe(draft);
  });
  it('sends the parsed setting with its base, in that order, and refuses text that is not JSON', () => {
    expect(JSON.stringify(macroChange({ expected: 2, text: '{"\\\\N": "\\\\mathbb{N}"}' }))).toBe(
      '{"expected":2,"macros":{"\\\\N":"\\\\mathbb{N}"}}',
    );
    expect(() => macroChange({ expected: 2, text: '{' })).toThrow(SyntaxError);
  });
  it('bases the draft on the saved revision after a save', () => {
    expect(savedDraft({ ...current, revision: 5 })).toEqual({
      expected: 5,
      text: '{\n  "\\\\R": "\\\\mathbb{R}"\n}',
    });
  });
});
