// SPDX-License-Identifier: MIT
/**
 * The user's record that they checked a claim, a result or a reference at one revision
 * (docs/specs/ledger.md, LG-16, LG-17; docs/LEDGER.md, "The gate"): an independent check
 * of the user's, which the gate counts for the main agent's establishing, and which for a
 * reference vouches for the PDF that revision names, by its SHA-256. The user attests to exactly
 * the text they read, so the view shows it as `ApprovalText` does, and the check names
 * the revision read: a newer one is a conflict, never a check of text the user did not see.
 */
import type { LedgerChangeAnswer } from './api';
import { ApiError } from './api';
import type { EntryLink, EntryView, EvidenceLink } from './api-types';
import { evidenceKind, quoteWhere } from './ledgerEvidence';
import { guardedWrite } from './mutation';
import { submit, type Submitting } from './submit';

/**
 * Whether the user can record a check of the entry shown: a claim, result or reference, at
 * its current revision. A reference's check vouches for the PDF that revision names (LG-16).
 */
export function checkable(entry: Pick<EntryView, 'type' | 'revision' | 'head'>): boolean {
  return (
    (entry.type === 'claim' || entry.type === 'result' || entry.type === 'reference') &&
    entry.revision === entry.head
  );
}

/**
 * The PDF a reference's revision names, as the exact text the user vouches for by checking
 * it: the artifact revision and the PDF's whole SHA-256; null when it names none, or names
 * one the view cannot read.
 */
export function pdfText(fields: Record<string, unknown>): string | null {
  const pdf = fields.pdf;
  if (pdf === null || typeof pdf !== 'object') return null;
  const { artifact_id, revision, sha256 } = pdf as Record<string, unknown>;
  if (typeof artifact_id !== 'string' || typeof sha256 !== 'string') return null;
  return `artifact ${artifact_id}, revision ${String(revision)}\nsha256 ${sha256}`;
}

/** An entry's fields as the exact text the user attests to; empty for none. */
export function fieldsText(fields: Record<string, unknown>): string {
  return Object.keys(fields).length === 0 ? '' : JSON.stringify(fields, null, 2);
}

/** An evidence link's pinned target, exactly: its full SHA-256, block and math span. */
function exactTarget(link: EvidenceLink): string {
  const at = link.revision === null ? '' : ` r${link.revision}`;
  switch (link.target) {
    case 'entry':
      return `entry ${link.handle ?? link.target_id} (${link.target_id})${at}`;
    case 'artifact':
      return `artifact ${link.target_id}${at}${link.sha256 === null ? '' : `, sha256 ${link.sha256}`}`;
    case 'block':
      return (
        `document ${link.target_id}${at}, block ${link.block ?? '?'}` +
        (link.span === null ? '' : `, math span ${link.span}`)
      );
    case 'review':
      return `review ${link.target_id}`;
    case 'delegation':
      return `delegation ${link.target_id}${at}${link.sha256 === null ? '' : `, sha256 ${link.sha256}`}`;
    case 'quote':
      return (
        `quote of ${link.handle ?? link.target_id} (${link.target_id})${at}` +
        (link.sha256 === null ? '' : `, PDF sha256 ${link.sha256}`) +
        `${quoteWhere(link.locator)}: ${JSON.stringify(String(link.locator?.text ?? ''))}`
      );
  }
}

/**
 * Every evidence link the revision holds, as the exact text the user attests to: its
 * role and type, its pinned target in full and its scope note as written; empty for none.
 */
export function evidenceText(links: readonly EvidenceLink[]): string {
  return links
    .map(
      (link, index) =>
        `${index + 1}. ${evidenceKind(link)}: ${exactTarget(link)}` +
        (link.scope === null ? '' : `\nScope: ${link.scope}`),
    )
    .join('\n\n');
}

/** Every `assumes` link the revision holds, as exact text, with its note; empty for none. */
export function assumesText(links: readonly EntryLink[]): string {
  return links
    .filter((link) => link.kind === 'assumes')
    .map(
      (link) =>
        `Assumes ${link.handle ?? link.target_id}` +
        (link.revision === null ? '' : ` r${link.revision}`) +
        (link.note === null ? '' : `: ${link.note}`),
    )
    .join('\n');
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
