// SPDX-License-Identifier: MIT
/**
 * An entry's work records as the entry view shows them (docs/specs/ledger.md, LG-6, LG-7,
 * LG-12; docs/LEDGER.md, "Work records"): what was tried, how it ended and its detail,
 * its pinned references, leading where the workspace shows them, the record it is part
 * of, and who wrote it. What was tried, the reason and each reference's note are the
 * author's text, shown with each character a person may not see as its code point. On an
 * issue's claimed obstruction, the user's acceptance or rejection (LG-7) shows the claim
 * and the issue as the exact text it holds for.
 */
import { ApiError } from './api';
import type { EntryView, WorkRecord } from './api-types';
import type { LinkTarget } from './ledger';
import { documentHref, ENTRY_NAME, ledgerEntryHref } from './router';
import { visible } from './text';

/** How a record ended, in words. */
export const ENDING_WORDS: Record<WorkRecord['ending'], string> = {
  limit: 'Stopped at a limit',
  obstruction_claimed: 'Obstruction claimed',
  produced: 'Produced',
  assessment: 'Assessment',
};

/** What ran out, in words. */
const LIMIT_WORDS: Record<NonNullable<WorkRecord['limit']>, string> = {
  time: 'time',
  tools: 'the tools',
  context: 'context',
  ideas: 'ideas',
};

/** A verdict on the work a record assesses, in words. */
export const VERDICT_WORDS: Record<NonNullable<WorkRecord['verdict']>, string> = {
  accepted: 'accepted',
  rejected: 'rejected',
  inconclusive: 'inconclusive',
};

/**
 * A record's detail in words: what ran out, or what it assesses and the verdict, naming
 * the assessed record's ending when it is one read; empty for the other endings, whose
 * detail is their produced entries or their references.
 */
export function detailText(record: WorkRecord, read: readonly WorkRecord[] = []): string {
  const check = checkView(record);
  if (check !== null) {
    const outcome =
      check.verdict === null
        ? 'its note gives no verdict; its outcome is in the run tree'
        : `${check.verdict}, ${check.status ?? 'no status'}`;
    return `A check of the entry by delegation ${visible(check.delegation)}: ${outcome}.`;
  }
  if (record.ending === 'limit') {
    const ran =
      record.limit === null
        ? 'something'
        : (LIMIT_WORDS[record.limit] ?? visible(String(record.limit)));
    return `Ran out of ${ran}; the work can go on.`;
  }
  if (record.ending !== 'assessment') return '';
  const verdict =
    record.verdict === null
      ? 'no verdict'
      : (VERDICT_WORDS[record.verdict] ?? visible(String(record.verdict)));
  if (record.assesses === null) return `A critique, ${verdict}.`;
  const assessed = read.find((other) => other.record_id === record.assesses);
  const what = assessed === undefined ? '' : ` (${ENDING_WORDS[assessed.ending].toLowerCase()})`;
  return `Assesses work record ${visible(record.assesses)}${what}: ${verdict}.`;
}

/**
 * A delegated check's record (LG-F8): the platform records a check that returned a result
 * as the delegated run's assessment of the entry, of no other record, citing the
 * delegation, whose note begins with the verdict and status (`confirmed, complete: …`).
 */
export interface CheckView {
  delegation: string;
  verdict: string | null;
  status: string | null;
  /** The session the delegated run ran in, where the run tree shows the delegation. */
  session: string | null;
}

/** A record's delegated check; null for any other record. */
export function checkView(record: WorkRecord): CheckView | null {
  if (record.ending !== 'assessment' || record.assesses !== null) return null;
  const cited = record.references.find((reference) => typeof reference.delegation === 'string');
  if (cited === undefined) return null;
  const note = typeof cited.note === 'string' ? cited.note : '';
  // Only the platform's head of the note: the verdict and status the result's schema allows.
  const said = /^(confirmed|refuted|inconclusive), (complete|partial|blocked)(?::|$)/.exec(note);
  const session = record.author.session_id;
  return {
    delegation: cited.delegation as string,
    verdict: record.verdict ?? said?.[1] ?? null,
    status: said?.[2] ?? null,
    session: typeof session === 'string' ? session : null,
  };
}

/** A record's ending in words: a delegated check's is a check of the entry, not a critique. */
export function endingText(record: WorkRecord): string {
  if (checkView(record) !== null) return 'Check of the entry';
  return ENDING_WORDS[record.ending] ?? visible(String(record.ending));
}

/** A pinned reference of a record, in words, and where it leads when somewhere. */
export interface ReferenceView extends LinkTarget {
  /** The reference's note, the author's text; empty for none. */
  note: string;
}

const count = (value: unknown): number | null =>
  Number.isSafeInteger(value) && (value as number) >= 1 ? (value as number) : null;
const name = (value: unknown): string | null =>
  typeof value === 'string' && value !== '' ? value : null;

/**
 * A reference as the view shows it: a delegation's outcome, an artifact revision with
 * the start of its SHA-256, a document block at its revision (leading to it), or another
 * work record, named with its ending when it is one read.
 */
export function referenceView(
  project: string,
  reference: Record<string, unknown>,
  read: readonly WorkRecord[] = [],
): ReferenceView {
  const note = typeof reference.note === 'string' ? visible(reference.note) : '';
  const revision = count(reference.revision);
  const delegation = name(reference.delegation);
  if (delegation !== null) {
    return { text: `the outcome of delegation ${visible(delegation)}`, href: null, note };
  }
  const artifact = name(reference.artifact);
  if (artifact !== null) {
    const digest =
      typeof reference.sha256 === 'string' ? `, sha256 ${reference.sha256.slice(0, 12)}…` : '';
    return {
      text: `artifact ${visible(artifact)} r${revision ?? '?'}${visible(digest)}`,
      href: null,
      note,
    };
  }
  const document = name(reference.document);
  if (document !== null) {
    const block = name(reference.block);
    return {
      text: `document ${visible(document)} r${revision ?? '?'}, block ${visible(block ?? '?')}`,
      href:
        revision !== null && block !== null && /^b[0-9]{1,9}$/.test(block)
          ? documentHref(project, document, revision, block)
          : null,
      note,
    };
  }
  const work = name(reference.work);
  if (work !== null) {
    const found = read.find((other) => other.record_id === work);
    const what = found === undefined ? '' : `, ${ENDING_WORDS[found.ending].toLowerCase()}`;
    return { text: `work record ${visible(work)}${what}`, href: null, note };
  }
  return { text: visible(JSON.stringify(reference)), href: null, note };
}

/** The entries a record produced, each leading to it. */
export function producedLinks(project: string, record: WorkRecord): LinkTarget[] {
  return record.produced.map((handle) => ({
    text: visible(handle),
    href: ENTRY_NAME.test(handle) ? ledgerEntryHref(project, handle) : null,
  }));
}

/** Whether an entry of this type holds work records (LG-6). */
export function holdsWork(type: string): boolean {
  return type === 'issue' || type === 'claim' || type === 'result';
}

/** How many records a page of the entry view reads. */
export const WORK_PAGE = 20;

/** The longest note the server takes with an assessment. */
export const MAX_ASSESSMENT_NOTE = 8192;

/** The user's verdict on a claimed obstruction. */
export type Assessing = 'accepted' | 'rejected';

/** Whether the entry view offers the user's assessment of `record` on `entry` (LG-7). */
export function assessable(
  entry: Pick<EntryView, 'type' | 'revision' | 'head'>,
  record: Pick<WorkRecord, 'ending'>,
): boolean {
  return (
    entry.type === 'issue' &&
    entry.revision === entry.head &&
    record.ending === 'obstruction_claimed'
  );
}

/**
 * A claimed obstruction as the exact text the user assesses: what was tried, then each
 * piece of its evidence, its pinned target and its note as written.
 */
export function claimText(record: WorkRecord): string {
  const evidence = record.references.map((reference, index) => {
    const { note, ...target } = reference;
    const said = typeof note === 'string' ? `\nNote: ${note}` : '';
    return `${index + 1}. ${JSON.stringify(target)}${said}`;
  });
  return evidence.length
    ? `${record.tried}\n\nIts evidence:\n${evidence.join('\n\n')}`
    : `${record.tried}\n\nIt cites no evidence.`;
}

/** Why a note cannot be sent as it is; null when it can. */
export function assessmentProblem(note: string): string | null {
  if (!note.trim()) return 'Say what you read, and why you decide so.';
  if (note.length > MAX_ASSESSMENT_NOTE) {
    return `The note is longer than ${MAX_ASSESSMENT_NOTE} characters.`;
  }
  return null;
}

/** What to say after an assessment failed on a newer revision; null for another failure. */
export function assessmentConflict(
  error: unknown,
  handle: string,
  revision: number,
): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  return (
    `${handle} has a newer revision than the r${revision} you read, so nothing was ` +
    'recorded. Cancel, then open it again to read it as it is now.'
  );
}
