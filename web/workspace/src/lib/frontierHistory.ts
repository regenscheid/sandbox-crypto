// SPDX-License-Identifier: MIT
/**
 * A frontier category's history, and its best known results at a past time (docs/specs/
 * ledger.md, LG-24; docs/LEDGER.md, "The history"): each change to the best known under
 * one set of conditions, with what it added and removed and its cause in words, and the
 * time the user picks for the past, sent with its zone.
 */
import type { FrontierChange } from './api-types';

/** Whose operation a cause names (the server's `actor`), in words. */
export function actorText(actor: unknown): string {
  if (typeof actor !== 'string') return 'someone';
  if (actor.startsWith('user:')) return 'the user';
  if (actor.startsWith('session:')) return `an agent of session ${actor.slice('session:'.length)}`;
  return actor;
}

/** A change's cause in words: what it was, its reason, the results it changed and the links. */
export interface CauseText {
  summary: string;
  reason: string | null;
  /** The handles of the results the change made or revised. */
  entries: string[];
  /** The dominates links it added or dropped, in the server's words. */
  links: string[];
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];

export function causeText(cause: Record<string, unknown>): CauseText {
  const none = { reason: null, entries: [], links: [] };
  if ('baseline' in cause) {
    return { ...none, summary: 'The history begins: the best known results the category had.' };
  }
  const operation =
    typeof cause.operation_id === 'string' ? `, operation ${cause.operation_id}` : '';
  if (typeof cause.category_revision === 'number') {
    return {
      ...none,
      summary: `The category's revision r${cause.category_revision}, by ${actorText(cause.author)}${operation}`,
    };
  }
  if ('operation_id' in cause) {
    return {
      summary: `A ledger change by ${actorText(cause.author)}${operation}`,
      reason: typeof cause.reason === 'string' ? cause.reason : null,
      entries: strings(cause.entries),
      links: strings(cause.dominates),
    };
  }
  return { ...none, summary: `A change recorded as ${JSON.stringify(cause)}` };
}

/** How many of a part of a change the answer leaves out, in words; empty for none. */
export function changeMore(change: Pick<FrontierChange, 'more'>, part: string): string {
  const count = change.more[part] ?? 0;
  return count > 0 ? ` and ${count} more` : '';
}

/**
 * The time a `datetime-local` field holds, read in the browser's zone, as the server takes
 * it: with its zone; null when the field holds no time.
 */
export function pastTime(local: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(local)) return null;
  const time = new Date(local);
  return Number.isNaN(time.getTime()) ? null : time.toISOString().replace(/Z$/, '+00:00');
}
