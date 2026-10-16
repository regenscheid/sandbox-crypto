// SPDX-License-Identifier: MIT
/**
 * Flags for reconsideration as the ledger's views show them, and the user's clearing and
 * reopening of them (docs/specs/ledger.md, LG-28, LG-29; docs/LEDGER.md, "Flags"): a
 * flag's cause and source in words, with where the source leads; the user's change, sent
 * with an operation ID kept until its answer, then the entry read again, also after a
 * refusal, so the view shows the flags as they are beside the error.
 */
import type { LedgerChangeAnswer } from './api';
import type { ClearedFlag, EntryFlag } from './api-types';
import type { LinkTarget } from './ledger';
import { statusText } from './ledger';
import { guardedWrite } from './mutation';
import { ENTRY_NAME, ledgerEntryHref } from './router';
import { submit, type Submitting } from './submit';

/** What made a flag, in words (LG-28). */
export const CAUSE_WORDS: Record<EntryFlag['cause'], string> = {
  assumption_revised: 'an assumption it rests on was revised',
  assumption_withdrawn: 'an assumption it rests on was withdrawn',
  dependency_status: 'an entry it rests on was set aside',
  evidence_changed: 'an entry its evidence pins changed its statement',
  spread: 'an entry it rests on is flagged',
  reopen_condition: 'what its reopen condition watches was resolved or changed',
};

/**
 * The entry a flag came from, at the revision that raised it and with its status then
 * when the flag names them, leading there. A source of another shape is shown as stored.
 */
export function flagSource(project: string, source: Record<string, unknown>): LinkTarget {
  const { handle, revision, status } = source;
  if (typeof handle !== 'string' || !ENTRY_NAME.test(handle)) {
    return { text: JSON.stringify(source), href: null };
  }
  const pinned = Number.isSafeInteger(revision) && (revision as number) > 0;
  const at = pinned ? ` r${revision as number}` : '';
  const was = typeof status === 'string' ? ` (${statusText(status)})` : '';
  return {
    text: `${handle}${at}${was}`,
    href: ledgerEntryHref(project, handle, pinned ? (revision as number) : undefined),
  };
}

/** The rest of what a flag says: the revision it was raised on, what it spread from or reopens, and the reviews that held that revision. */
export function flagDetails(
  flag: Pick<EntryFlag, 'revision' | 'via' | 'reviews' | 'reopens'>,
): string {
  const parts = [`raised on r${flag.revision}`];
  if (flag.via) parts.push(`spread from flag ${flag.via}`);
  if (flag.reopens) parts.push(`reopens flag ${flag.reopens}`);
  if (flag.reviews.length) parts.push(`held by reviews ${flag.reviews.join(', ')}`);
  return parts.join('; ');
}

/** The count of open flags a card or a row shows; nothing for none. */
export function openFlagsText(count: number): string {
  if (count <= 0) return '';
  return count === 1 ? '1 open flag' : `${count} open flags`;
}

/** The longest reason the server takes. */
export const MAX_REASON = 4096;

/** Why a reason cannot be sent as it is; null when it can. */
export function reasonProblem(reason: string): string | null {
  if (!reason.trim())
    return 'Give the reason: why the entry still holds, or why the change does not affect it.';
  if (reason.length > MAX_REASON) return `The reason is longer than ${MAX_REASON} characters.`;
  return null;
}

/**
 * The user's clearing of open flags, always by the IDs of the flags the user read: the
 * server's form that clears every flag open when it arrives would also clear a flag
 * raised after the page was read, or one beyond those listed, under a reason written for
 * others. That form is the command line's only.
 */
export interface FlagClearing {
  flags: string[];
  reason: string;
}

/** The user's reopening of cleared flags, each as a new flag. */
export interface FlagReopening {
  flags: string[];
  reason: string;
}

/**
 * Send one clearing or reopening for an entry, then read the entry again; after a
 * failure, read it too, so a conflict or refusal shows beside the flags as they are now.
 * The ledger's answer, or null when the change failed.
 */
export async function changeFlags<B extends FlagClearing | FlagReopening>(
  saving: Submitting,
  kind: 'clear' | 'reopen',
  project: string,
  entry: string,
  change: B,
  send: (body: B & { operation_id: string }) => Promise<LedgerChangeAnswer>,
  reload: () => Promise<void>,
): Promise<LedgerChangeAnswer | null> {
  let answer: LedgerChangeAnswer | null = null;
  const done = await submit(
    saving,
    async () => {
      answer = await guardedWrite(`ledger-flags-${kind}`, `${project}:${entry}`, change, send);
      await reload();
    },
    reload,
  );
  return done ? answer : null;
}

/** What a clearing did, in words: how many flags it cleared, against how many were asked. */
export function clearedSummary(
  answer: Pick<LedgerChangeAnswer, 'cleared_total'>,
  asked: number,
): string {
  const cleared = answer.cleared_total;
  const said = cleared === 1 ? '1 flag' : `${cleared} flags`;
  if (cleared === asked) return `Cleared ${said}.`;
  return `Cleared ${said}, though ${asked} ${asked === 1 ? 'was' : 'were'} selected: read the flags again before going on.`;
}

/** What a reopening did, in words. */
export function reopenedSummary(answer: Pick<LedgerChangeAnswer, 'flags_reopened'>): string {
  const count = answer.flags_reopened.length;
  return `Reopened ${count === 1 ? '1 flag' : `${count} flags`}, each as a new open flag.`;
}

/** The read lists this many open flags, and this many cleared ones, newest first. */
export const LISTED = 50;

/**
 * The open flag, if any, that stands for a cleared one again: the flag that reopened it,
 * or an open flag of the same cause and source, which the ledger keeps one of. A cleared
 * flag with one is not offered for reopening.
 */
export function openAgain(
  cleared: Pick<ClearedFlag, 'flag_id' | 'cause' | 'source'>,
  open: readonly Pick<EntryFlag, 'flag_id' | 'cause' | 'source' | 'reopens'>[],
): string | null {
  const same = JSON.stringify(cleared.source);
  const found = open.find(
    (flag) =>
      flag.reopens === cleared.flag_id ||
      (flag.cause === cleared.cause && JSON.stringify(flag.source) === same),
  );
  return found ? found.flag_id : null;
}

/** Who cleared a flag, when and why, in words; `author` describes who. */
export function clearedText(flag: Pick<ClearedFlag, 'reason'>, who: string, when: string): string {
  return `Cleared ${when} by ${who}: ${flag.reason}`;
}
