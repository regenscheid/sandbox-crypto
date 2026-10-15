// SPDX-License-Identifier: MIT
/**
 * The project ledger as the board and the entry view show it (docs/specs/ledger.md,
 * LG-2, LG-5, LG-34; docs/LEDGER.md): its types and their statuses, links in words with
 * where they lead, an entry's fields as rows, and who wrote a revision. Read-only: agents
 * change the ledger through their tools.
 */
import type { Author } from './api';
import type { EntryLink, EntrySummary } from './api-types';
import { describeAuthor } from './authors';
import { documentHref, ENTRY_NAME, ledgerEntryHref } from './router';

export type EntryType = EntrySummary['type'];
export type LinkKind = EntryLink['kind'];

/** The types in the order the board offers them, with their names and statuses (LG-2). */
export const ENTRY_TYPES: readonly EntryType[] = [
  'issue',
  'claim',
  'result',
  'assumption',
  'decision',
  'task',
  'reference',
];

export const TYPE_NAMES: Record<EntryType, string> = {
  issue: 'Issues',
  claim: 'Claims',
  result: 'Results',
  assumption: 'Assumptions',
  decision: 'Decisions',
  task: 'Tasks',
  reference: 'References',
};

/** One entry's type in words. */
export const TYPE_NAME: Record<EntryType, string> = {
  issue: 'Issue',
  claim: 'Claim',
  result: 'Result',
  assumption: 'Assumption',
  decision: 'Decision',
  task: 'Task',
  reference: 'Reference',
};

const CLAIM_STATUSES = [
  'proposed',
  'established',
  'recorded',
  'disputed',
  'refuted',
  'superseded',
  'withdrawn',
] as const;

/** Each type's statuses, as the board's columns, in the order the spec gives them. */
export const STATUSES: Record<EntryType, readonly string[]> = {
  issue: ['open', 'resolved', 'obstructed', 'split', 'withdrawn'],
  claim: CLAIM_STATUSES,
  result: CLAIM_STATUSES,
  assumption: ['active', 'withdrawn'],
  decision: ['proposed', 'in_effect', 'rejected', 'reversed'],
  task: ['todo', 'in_progress', 'blocked', 'done', 'dropped'],
  reference: ['active', 'withdrawn'],
};

/** A status in words: `in_effect` reads `in effect`. */
export function statusText(status: string): string {
  return status.replaceAll('_', ' ');
}

/** Whether an entry has every word of `filter` in its handle or title, ignoring case. */
export function matches(entry: Pick<EntrySummary, 'handle' | 'title'>, filter: string): boolean {
  const text = `${entry.handle} ${entry.title}`.toLowerCase();
  return filter
    .toLowerCase()
    .split(/\s+/)
    .every((word) => text.includes(word));
}

/** What a link of each kind says, read from the entry that holds it and from its target. */
export const LINK_WORDS: Record<LinkKind, { out: string; in: string }> = {
  answers: { out: 'Answers', in: 'Answered by' },
  assumes: { out: 'Assumes', in: 'Assumed by' },
  builds_on: { out: 'Builds on', in: 'Built on by' },
  restates: { out: 'Restates', in: 'Restated by' },
  compares_to: { out: 'Compared to', in: 'Compared with by' },
  supersedes: { out: 'Supersedes', in: 'Superseded by' },
  dominates: { out: 'Dominates', in: 'Dominated by' },
  split_into: { out: 'Split into', in: 'Split from' },
  affects: { out: 'Affects', in: 'Affected by' },
  concerns: { out: 'Concerns', in: 'Concerned by' },
};

/** A link's target in words, and where it leads in the workspace, when somewhere. */
export interface LinkTarget {
  text: string;
  href: string | null;
}

/**
 * Where a link leads: an entry (to the pinned revision, when the link pins one), or a
 * document block at its revision. A link toward the entry shown (`in`) names the entry
 * that holds it, at its current revision, and the revision of the shown entry it pins. A
 * review finding is named, for now without a page of its own in the project: findings
 * are shown in their session.
 */
export function linkTarget(
  project: string,
  link: EntryLink,
  direction: 'out' | 'in' = 'out',
): LinkTarget {
  const pinned = link.revision === null ? '' : ` r${link.revision}`;
  switch (link.target) {
    case 'entry': {
      const name = link.handle ?? link.target_id;
      if (!ENTRY_NAME.test(name)) return { text: name, href: null };
      if (direction === 'in') {
        const pins = link.revision === null ? '' : `, pinning r${link.revision}`;
        return { text: `${name}${pins}`, href: ledgerEntryHref(project, name) };
      }
      return {
        text: `${name}${pinned}`,
        href: ledgerEntryHref(project, name, link.revision ?? undefined),
      };
    }
    case 'block':
      return {
        text: `${link.target_id}${pinned}, block ${link.block ?? '?'}`,
        href:
          link.revision === null || link.block === null
            ? null
            : documentHref(project, link.target_id, link.revision, link.block),
      };
    case 'finding':
      return { text: `review finding ${link.target_id}`, href: null };
  }
}

/** Links grouped by kind, in the order of `LINK_WORDS`, each group in stored order. */
export function linksByKind(links: readonly EntryLink[]): [LinkKind, EntryLink[]][] {
  const kinds = Object.keys(LINK_WORDS) as LinkKind[];
  return kinds
    .map((kind): [LinkKind, EntryLink[]] => [kind, links.filter((link) => link.kind === kind)])
    .filter(([, found]) => found.length > 0);
}

/** One of an entry's own fields, as a row: its name and its value in words. */
export interface FieldRow {
  name: string;
  value: string;
}

const FIELD_NAMES: Record<string, string> = {
  kind: 'Kind',
  category: 'Frontier category',
  conditions: 'Conditions',
  measures: 'Measures',
  identifiers: 'Identifiers',
  authors: 'Authors',
  published: 'Published',
  version: 'Version',
  retrieved: 'Retrieved',
  pdf: 'PDF',
};

function valueText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.map(valueText).join('; ');
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (typeof record.artifact_id === 'string' && typeof record.revision === 'number') {
      return `artifact ${record.artifact_id} r${record.revision}`;
    }
    return Object.entries(record)
      .map(([key, inner]) => `${key}: ${valueText(inner)}`)
      .join('; ');
  }
  return JSON.stringify(value) ?? '';
}

/**
 * The fields an entry's type gives it (claim kinds, a result's conditions and measures, a
 * reference's citation), as rows of text. A field the view does not know yet is still
 * shown, under its own name.
 */
export function fieldRows(fields: Record<string, unknown>): FieldRow[] {
  const known = Object.keys(FIELD_NAMES);
  const names = [
    ...known.filter((name) => name in fields),
    ...Object.keys(fields)
      .filter((name) => !known.includes(name))
      .sort(),
  ];
  return names.map((name) => ({
    name: FIELD_NAMES[name] ?? statusText(name),
    // A claim's kind is a word of the spec's, such as `negative_result`.
    value: name === 'kind' ? statusText(valueText(fields[name])) : valueText(fields[name]),
  }));
}

const AUTHOR_KINDS = new Set(['user', 'agent', 'worker', 'import', 'platform']);

/** Who wrote a revision, in words; the record as stored when it is not one the view reads. */
export function authorText(raw: Record<string, unknown>): string {
  const kind = raw.kind;
  const strings = Object.entries(raw).every(([, value]) => typeof value === 'string');
  if (typeof kind !== 'string' || !AUTHOR_KINDS.has(kind) || !strings) {
    return JSON.stringify(raw);
  }
  return describeAuthor(raw as unknown as Author);
}
