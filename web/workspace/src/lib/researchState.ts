// SPDX-License-Identifier: MIT
/**
 * The research state on ledger entries and work records (docs/specs/context-management.md,
 * CM-10 to CM-12): how the ledger labels an entry or a record (`standing`: an attempt, a
 * claimed or disputed obstruction, flagged, or impossible, with its scope and who settled
 * it), an issue's kind, a decision meant for one session, a reopen condition and the
 * caveats, a reopening the agent made, and how far a reference's sources were read. All of
 * it is agents' text, shown with each character a person may not see as its code point.
 */
import type { EntrySummary, EntryView, SourceRead, Standing, WorkRecord } from './api-types';
import { type LinkTarget, statusText } from './ledger';
import { ENTRY_NAME, ledgerEntryHref } from './router';
import { visible } from './text';

export const STANDING_WORDS: Record<Standing['label'], string> = {
  attempt: 'Attempt',
  claimed: 'Claimed',
  disputed: 'Disputed',
  flagged: 'Flagged',
  impossible: 'Impossible',
};

/** Who `by` names, for each label: the decision that settled it, or the flag's source. */
const BY_WORDS: Record<Standing['label'], string> = {
  attempt: 'By',
  claimed: 'Claimed by',
  disputed: 'Disputed by',
  flagged: 'Flagged from',
  impossible: 'Settled by',
};

export interface StandingView {
  label: Standing['label'];
  word: string;
  text: string;
  scope: string | null;
  assumes: LinkTarget[];
  by: LinkTarget | null;
  byWord: string;
}

/** An entry named by its handle, leading to it when it is one the ledger names. */
function entryLink(project: string, name: string): LinkTarget {
  return {
    text: visible(name),
    href: ENTRY_NAME.test(name) ? ledgerEntryHref(project, name) : null,
  };
}

/** How the ledger labels an entry or a record; null when it gives no label. */
export function standingView(project: string, standing: Standing | null): StandingView | null {
  if (standing === null) return null;
  return {
    label: standing.label,
    word: STANDING_WORDS[standing.label] ?? visible(String(standing.label)),
    text: visible(standing.text),
    scope: standing.scope === null ? null : visible(standing.scope),
    assumes: standing.assumes.map((handle) => entryLink(project, handle)),
    by: standing.by === null ? null : entryLink(project, standing.by),
    byWord: BY_WORDS[standing.label] ?? 'By',
  };
}

/** The label in one line, as a list's badge explains it. */
export function standingLine(view: StandingView): string {
  const scope = view.scope === null ? '' : `; within ${view.scope}`;
  const by = view.by === null ? '' : `; ${view.byWord.toLowerCase()} ${view.by.text}`;
  return `${view.word}: ${view.text}${scope}${by}`;
}

/** What a list shows beside an entry: an issue's or a claim's kind, a decision's session. */
export function entryTags(entry: Pick<EntrySummary, 'type' | 'kind' | 'session'>): string[] {
  const tags: string[] = [];
  if (entry.kind !== null && entry.type !== 'decision') tags.push(visible(statusText(entry.kind)));
  if (entry.session !== null) tags.push(`for session ${visible(entry.session)} only`);
  return tags;
}

/** The research state's fields, which the entry view shows apart from the type's others. */
export const STATE_FIELDS = ['kind', 'session', 'reopen_condition', 'caveats'] as const;

/** An entry's reopen condition and caveats, and whether the condition's flag is open. */
export function stateFields(entry: Pick<EntryView, 'fields' | 'flags'>): {
  reopen: string | null;
  reopenFlagged: boolean;
  caveats: string[];
} {
  const reopen = entry.fields['reopen_condition'];
  const caveats = entry.fields['caveats'];
  return {
    reopen: typeof reopen === 'string' && reopen ? visible(reopen) : null,
    reopenFlagged: entry.flags.some((flag) => flag.cause === 'reopen_condition'),
    caveats: Array.isArray(caveats)
      ? caveats.filter((c): c is string => typeof c === 'string').map(visible)
      : [],
  };
}

/** A reopening's line: the reason the agent gave, and the decision it reversed. */
export function reopenedText(record: Pick<WorkRecord, 'ending' | 'reason'>): string | null {
  return record.ending === 'reopened' ? `Reopened by the agent: ${visible(record.reason)}` : null;
}

const EXTENT_WORDS: Record<SourceRead['extent'], string> = {
  whole: 'read whole',
  partial: 'read in part',
  abstract_only: 'its abstract only',
  preview_only: 'a preview only',
  unknown: 'read, how far not known',
};

const number = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) ? value : null;

/** Ranges as `a–b, c–d`, with what they are of. */
function rangesText(value: unknown): string {
  if (!Array.isArray(value)) return '';
  return value
    .map((pair) =>
      Array.isArray(pair) && number(pair[0]) !== null && number(pair[1]) !== null
        ? `${pair[0].toLocaleString('en')}–${pair[1].toLocaleString('en')}`
        : null,
    )
    .filter((text): text is string => text !== null)
    .join(', ');
}

/** What a partial read covered: pages, byte ranges or character ranges. */
export function coveredText(covered: SourceRead['covered']): string {
  if (covered === null) return '';
  const parts: string[] = [];
  if (typeof covered.pages === 'string') {
    const of = number(covered.of);
    parts.push(`pages ${visible(covered.pages)}${of === null ? '' : ` of ${of}`}`);
  }
  const bytes = rangesText(covered.ranges);
  if (bytes) {
    const size = number(covered.size);
    parts.push(`bytes ${bytes}${size === null ? '' : ` of ${size.toLocaleString('en')}`}`);
  }
  const chars = rangesText(covered.chars);
  if (chars) parts.push(`characters ${chars}${covered.end === true ? ', to its end' : ''}`);
  return parts.join('; ');
}

/** One source of a reference and how far it was read, in words. */
export function readText(read: SourceRead): string {
  const extent = EXTENT_WORDS[read.extent] ?? visible(String(read.extent));
  const covered = coveredText(read.covered);
  return `${extent}${covered ? `: ${covered}` : ''}`;
}
