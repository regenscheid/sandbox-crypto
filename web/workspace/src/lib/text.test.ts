// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { visible } from './text';

describe('visible', () => {
  it('shows each bidirectional control as its code point', () => {
    expect(visible('access\u202E \u2066// admin\u2069 \u200E\u200F\u061C')).toBe(
      'access⟦U+202E⟧ ⟦U+2066⟧// admin⟦U+2069⟧ ⟦U+200E⟧⟦U+200F⟧⟦U+061C⟧',
    );
  });

  it('leaves other text as it is, but spaces other than U+0020', () => {
    const text = 'x = 1 # שלום, 😀, ';
    expect(visible(text)).toBe(text);
    expect(visible('a\u00a0b')).toBe('a⟦U+00A0⟧b');
  });
});
