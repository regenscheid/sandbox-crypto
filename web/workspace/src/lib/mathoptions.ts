// SPDX-License-Identifier: MIT
/** Shared by browser rendering and the isolated control-plane math checker. */
import type { KatexOptions } from 'katex';

export const KATEX_OPTIONS = {
  throwOnError: true,
  trust: false,
  strict: 'ignore',
  maxSize: 20,
  maxExpand: 1000,
  output: 'htmlAndMathml',
} as const satisfies KatexOptions;

/** KaTeX's output can grow about 150 times its input. */
export const MAX_FORMULA = 5_000;
