// SPDX-License-Identifier: MIT
/**
 * The research state on ledger entries and work records (docs/specs/context-management.md,
 * CM-10 to CM-12): how the ledger labels an entry or a record (`standing`: an attempt, a
 * claimed or disputed obstruction, flagged, or impossible, with its scope and who settled
 * it), an issue's kind, a decision meant for one session, a reopen condition and the
 * caveats, a reopening the agent made, how far a reference's sources were read, and where a
 * revision or a record comes from (CM-9: its basis and anchors, with the user's words a
 * standing constraint quotes, which code found word for word in the user's message). All of
 * it is agents' text, shown with each character a person may not see as its code point.
 */
import type { EntrySummary, EntryView, SourceRead, Standing, WorkRecord } from './api-types';
import { type LinkTarget, statusText } from './ledger';
import { documentHref, ENTRY_NAME, ledgerEntryHref } from './router';
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

type Basis = NonNullable<EntryView['basis']>;

/** Where a revision or a work record comes from (CM-9), in words. */
export const BASIS_WORDS: Record<Basis, string> = {
  user: "the user's own words: the user made the change",
  user_quoted: "the user's words, quoted by the agent",
  observed: 'observed: what a tool result, an artifact or a source shows',
  interpreted: "interpreted: the agent's own reading",
};

/** A place a revision or record comes from, leading to it where the workspace has a page. */
export interface AnchorView extends LinkTarget {
  /** The user's words, as the anchor quotes them; code found them in the message. */
  quote: string | null;
}

const text = (value: unknown): string | null =>
  typeof value === 'string' && value !== '' ? value : null;

/** An anchor as the ledger keeps it, in words. */
export function anchorView(project: string, anchor: Record<string, unknown>): AnchorView {
  const revision = number(anchor.revision);
  const at = revision === null ? '' : ` r${revision}`;
  const message = text(anchor.message);
  if (message !== null) {
    const kind = text(anchor.kind);
    const what = kind === 'tool_result' ? 'tool result' : kind === 'user' ? "user's message" : kind;
    const start = number(anchor.start);
    const end = number(anchor.end);
    const range = start === null || end === null ? '' : `, characters ${start}–${end}`;
    return {
      text: visible(`${what ?? 'message'} ${message}${range}`),
      href: null,
      quote: text(anchor.quote) === null ? null : visible(String(anchor.quote)),
    };
  }
  const artifact = text(anchor.artifact);
  if (artifact !== null) {
    const digest = text(anchor.sha256);
    const sha = digest === null ? '' : ` (sha256 ${digest.slice(0, 12)}…)`;
    return { text: visible(`artifact ${artifact}${at}${sha}`), href: null, quote: null };
  }
  const document = text(anchor.document);
  const block = text(anchor.block);
  if (document !== null && block !== null) {
    return {
      text: visible(`document ${document}${at}, block ${block}`),
      href:
        revision !== null && /^b[0-9]{1,9}$/.test(block)
          ? documentHref(project, document, revision, block)
          : null,
      quote: null,
    };
  }
  const entry = text(anchor.entry);
  if (entry !== null) {
    return {
      text: visible(`entry ${entry}${at}`),
      href: ENTRY_NAME.test(entry) ? ledgerEntryHref(project, entry, revision ?? undefined) : null,
      quote: null,
    };
  }
  return { text: visible(JSON.stringify(anchor)), href: null, quote: null };
}

/** A revision's basis and anchors, and whether it is the user's standing constraint. */
export function provenanceView(
  project: string,
  record: Pick<EntryView, 'basis' | 'anchors'> & Partial<Pick<EntryView, 'type' | 'status'>>,
): { basis: string | null; anchors: AnchorView[]; constraint: boolean } {
  const basis = record.basis === null ? null : (BASIS_WORDS[record.basis] ?? visible(record.basis));
  return {
    basis,
    anchors: record.anchors.map((anchor) => anchorView(project, anchor)),
    // A decision in effect on the user's words is a standing constraint (CM-16).
    constraint:
      record.type === 'decision' &&
      record.status === 'in_effect' &&
      (record.basis === 'user' || record.basis === 'user_quoted'),
  };
}
