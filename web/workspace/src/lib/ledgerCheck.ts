// SPDX-License-Identifier: MIT
/**
 * The user's record that they checked a claim or a result at one revision (docs/specs/
 * ledger.md, LG-17; docs/LEDGER.md, "The gate"): an independent check of the user's,
 * which the gate counts for the main agent's establishing. The user attests to exactly
 * the text they read, so the view shows it as `ApprovalText` does, and the check names
 * the revision read: a newer one is a conflict, never a check of text the user did not see.
 */
import type { LedgerChangeAnswer } from './api';
import { ApiError } from './api';
import type { EntryView } from './api-types';
import { guardedWrite } from './mutation';
import { submit, type Submitting } from './submit';

/** Whether the user can record a check of the entry shown: a claim or result, at its current revision. */
export function checkable(entry: Pick<EntryView, 'type' | 'revision' | 'head'>): boolean {
  return (entry.type === 'claim' || entry.type === 'result') && entry.revision === entry.head;
}

/** An entry's fields as the exact text the user attests to; empty for none. */
export function fieldsText(fields: Record<string, unknown>): string {
  return Object.keys(fields).length === 0 ? '' : JSON.stringify(fields, null, 2);
}

/** The longest note the server takes. */
export const MAX_NOTE = 4096;

/** Why a note cannot be sent as it is; null when it can (it is optional). */
export function noteProblem(note: string): string | null {
  return note.length > MAX_NOTE ? `The note is longer than ${MAX_NOTE} characters.` : null;
}

/** The user's check, as sent: the revision read, and the note when there is one. */
export interface CheckRecord {
  revision: number;
  note?: string;
}

/** The check of `revision` with `note`, its blank note left out. */
export function checkRecord(revision: number, note: string): CheckRecord {
  return note.trim() ? { revision, note } : { revision };
}

/** What to say after a check failed: a conflict means the entry moved on from the text shown. */
export function checkFailure(error: unknown): string | null {
  if (error instanceof ApiError && error.status === 409) {
    return 'The entry has a newer revision than the one you read, so nothing was recorded. Read it, then check it again.';
  }
  return null;
}

/**
 * Send the user's check of an entry, then read the entry again; after a failure, read it
 * too, so a conflict shows the entry as it is now.
 */
export function recordCheck(
  saving: Submitting,
  project: string,
  entry: string,
  record: CheckRecord,
  send: (body: CheckRecord & { operation_id: string }) => Promise<LedgerChangeAnswer>,
  reload: () => Promise<void>,
): Promise<boolean> {
  return submit(
    saving,
    async () => {
      await guardedWrite('ledger-check', `${project}:${entry}`, record, send);
      await reload();
    },
    reload,
  );
}
