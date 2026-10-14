// SPDX-License-Identifier: MIT
/**
 * KaTeX's errors, and any other, in a few words: for the repair ladder (mathrepair.ts),
 * the model's repairs (modelRepairs.ts) and the server-side math checker (mathcheck.ts),
 * which bundles this module alone of the three.
 *
 * Adapted from cscience-crypto's src/web/cscience-math.js, contributed under the MIT
 * License by its author.
 */

/** The longest error message kept, from KaTeX or the server. */
const MAX_ERROR_LENGTH = 300;

function cut(text: string, length: number): string {
  let out = text.slice(0, length);
  const last = out.charCodeAt(out.length - 1);
  if (last >= 0xd800 && last <= 0xdbff) out = out.slice(0, -1); // half a surrogate pair
  return out;
}

/** KaTeX's error, or any other, in a few words. */
export function describeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? 'unknown error');
  const brief = message
    .replace(/^KaTeX parse error:\s*/, '')
    .replace(/\s+/g, ' ')
    .trim();
  return cut(brief, MAX_ERROR_LENGTH);
}
