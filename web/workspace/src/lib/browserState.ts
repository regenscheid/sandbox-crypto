// SPDX-License-Identifier: MIT
/** Scoped browser choices and memory edits survive navigation in this tab. */
import { DraftStore } from './mutation';

export const browserState = new DraftStore<Record<string, string>>(
  'browser-view',
  (value): value is Record<string, string> =>
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((item) => typeof item === 'string'),
);
export interface MemoryEdit {
  text: string;
  base: number;
}
export const memoryEdits = new DraftStore<MemoryEdit>(
  'memory-entry',
  (value): value is MemoryEdit => {
    if (typeof value !== 'object' || value === null) return false;
    const found = value as Partial<MemoryEdit>;
    return (
      typeof found.text === 'string' && Number.isSafeInteger(found.base) && (found.base ?? -1) >= 0
    );
  },
);
