// SPDX-License-Identifier: MIT

/** Bidirectional controls, which reorder the text around them. */
const BIDI_CONTROLS = /[\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g;

/**
 * Text for showing as code, with each bidirectional control shown as its code point, such
 * as `⟦U+202E⟧`: left in, they can make code read differently from what runs.
 */
export function visible(text: string): string {
  return text.replace(BIDI_CONTROLS, (control) => {
    const code = control.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0');
    return `⟦U+${code}⟧`;
  });
}
