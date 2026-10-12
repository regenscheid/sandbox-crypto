// SPDX-License-Identifier: MIT
/** Job output is plain untrusted text. It never enters Markdown or HTML rendering. */
export const finishedStates = new Set(['succeeded', 'failed', 'timed_out', 'cancelled', 'lost']);

export function jobElapsed(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${value % 60}s`;
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
}
