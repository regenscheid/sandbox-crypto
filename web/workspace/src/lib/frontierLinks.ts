// SPDX-License-Identifier: MIT
/**
 * What dominates a result on the frontier, and the user's dropping of a `dominates` link
 * (docs/specs/ledger.md, LG-22; docs/LEDGER.md, "Dominates links"): by the rule, or by a
 * link that its holder records with a reason. The user drops a link as a change to its
 * holder at the revision read, with a reason; the view reads the holder first and shows
 * the link's reason exactly, as `ApprovalText` does, since the user acts on an agent's text.
 */
import type { LedgerChangeAnswer } from './api';
import { ApiError } from './api';
import type { DominatesLink, EntryLink, EntryView, FrontierResult } from './api-types';
import { writerText } from './frontier';
import { MAX_REASON } from './ledgerFlags';
import { guardedWrite } from './mutation';
import { submit, type Submitting } from './submit';

/** The results that dominate `result` by the rule: those that hold no link to it. */
export function ruleDominators(
  result: Pick<FrontierResult, 'dominated_by' | 'linked_by'>,
): string[] {
  const linked = new Set(result.linked_by.map((link) => link.handle));
  return result.dominated_by.filter((handle) => !linked.has(handle));
}

/** Who recorded a link, in which revision of its holder. */
export function recordedText(link: Pick<DominatesLink, 'handle' | 'revision' | 'author'>): string {
  return `recorded in ${link.handle} r${link.revision} by ${writerText(link.author)}`;
}

/** The `dominates` link `holder` holds to `target`, as the holder reads now; null when none. */
export function heldLink(holder: Pick<EntryView, 'links'>, target: string): EntryLink | null {
  return (
    holder.links.find(
      (link) =>
        link.kind === 'dominates' &&
        link.target === 'entry' &&
        (link.handle === target || link.target_id === target),
    ) ?? null
  );
}

/** Why a reason for dropping a link cannot be sent as it is; null when it can. */
export function dropProblem(reason: string): string | null {
  if (!reason.trim()) return 'Give the reason: why the link should not count.';
  if (reason.length > MAX_REASON) return `The reason is longer than ${MAX_REASON} characters.`;
  return null;
}

/** The user's dropping of a link, as sent: the target, the holder's revision read and why. */
export interface LinkDrop {
  target: string;
  revision: number;
  reason: string;
}

/** What to say after a drop failed, for the failures the user can act on; null for others. */
export function dropFailure(error: unknown, holder: string, revision: number): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 409) {
    return (
      `${holder} has a newer revision than the r${revision} you read, so nothing was dropped. ` +
      'Cancel, then open the link again to read it as it is now.'
    );
  }
  if (error.status === 404) return `${holder} no longer holds this link: nothing was dropped.`;
  return null;
}

/** Send the user's drop of a link, then `done`; after a failure, nothing more. */
export function dropLink(
  saving: Submitting,
  project: string,
  holder: string,
  drop: LinkDrop,
  send: (body: LinkDrop & { operation_id: string }) => Promise<LedgerChangeAnswer>,
  done: () => Promise<void>,
): Promise<boolean> {
  return submit(saving, async () => {
    await guardedWrite('ledger-drop', `${project}:${holder}`, drop, send);
    await done();
  });
}
