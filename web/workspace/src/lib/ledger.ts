// SPDX-License-Identifier: MIT
/**
 * The project ledger as the board and the entry view show it (docs/specs/ledger.md,
 * LG-2, LG-5, LG-34; docs/LEDGER.md): its types and their statuses, links in words with
 * where they lead, an entry's fields as rows, and who wrote a revision. Read-only: agents
 * change the ledger through their tools.
 */
import type { Author } from './api';
import type { EntryLink, EntrySummary, EntryView } from './api-types';
import { describeAuthor } from './authors';
import { visible } from './text';
import { PagedList, type ListState } from './pagedList';
import {
  CATEGORY_NAME,
  categoryHref,
  documentHref,
  ENTRY_NAME,
  findingHref,
  ledgerEntryHref,
  pdfHref,
} from './router';

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
  reopen_on: { out: 'Reopen condition watches', in: 'Watched by the reopen condition of' },
};

/** A link's target in words, and where it leads in the workspace, when somewhere. */
export interface LinkTarget {
  text: string;
  href: string | null;
}

/**
 * Where a link leads: an entry (to the pinned revision, when the link pins one), or a
 * document block at its revision, or a review finding in its session. A link toward the
 * entry shown (`in`) names the entry that holds it, at its current revision, and the
 * revision of the shown entry it pins. A finding whose session the read does not name is
 * named only.
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
      return link.session === null
        ? { text: `review finding ${link.target_id}`, href: null }
        : {
            text: `review finding ${link.target_id}, session ${link.session}`,
            href: findingHref(project, link.session, link.target_id),
          };
  }
}

/** Links grouped by kind, in the order of `LINK_WORDS`, each group in stored order. */
export function linksByKind(links: readonly EntryLink[]): [LinkKind, EntryLink[]][] {
  const kinds = Object.keys(LINK_WORDS) as LinkKind[];
  return kinds
    .map((kind): [LinkKind, EntryLink[]] => [kind, links.filter((link) => link.kind === kind)])
    .filter(([, found]) => found.length > 0);
}

/**
 * A page of the links to an entry, read from the `after`-th on, as a paged list takes it:
 * the next page starts after the links read so far, until the count is reached.
 */
export function incomingPage(
  entry: Pick<EntryView, 'incoming' | 'incoming_total'>,
  after: number,
): { items: EntryLink[]; next: number | null } {
  const read = after + entry.incoming.length;
  return {
    items: entry.incoming,
    next: entry.incoming.length > 0 && read < entry.incoming_total ? read : null,
  };
}

/**
 * The links to an entry, read a page at a time from an offset, with their count as the
 * last page read gave it. The first page is the one the entry came with.
 */
export interface IncomingState extends ListState<EntryLink, number> {
  total: number;
}

export function incomingState(
  entry: Pick<EntryView, 'incoming' | 'incoming_total'>,
): IncomingState {
  const first = incomingPage(entry, 0);
  return { ...first, loading: false, error: null, total: entry.incoming_total };
}

/** The list that reads the pages after the first, each from the links read so far. */
export function incomingLinks(
  state: IncomingState,
  read: (after: number) => Promise<Pick<EntryView, 'incoming' | 'incoming_total'>>,
): PagedList<EntryLink, number> {
  return new PagedList(state, async (after) => {
    const found = await read(after ?? 0);
    return {
      ...incomingPage(found, after ?? 0),
      shown: () => {
        state.total = found.incoming_total;
      },
    };
  });
}

/**
 * Each link once, the first time it was read: offset pages can repeat a link when entries
 * change between reads. A link is its kind, the entry that holds it and the revision it pins.
 */
export function uniqueLinks(links: readonly EntryLink[]): EntryLink[] {
  const seen = new Set<string>();
  return links.filter((link) => {
    const key = `${link.kind}\u0000${link.target_id}\u0000${link.revision ?? ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * What a column's filter found, in words, when one is set: it searches only the entries
 * read so far, so it says how many those are, and that more may match when more remain.
 */
export function filterText(matched: number, loaded: number, more: boolean): string {
  return `${matched} of ${loaded} loaded match${more ? '; more may match among those not read yet' : ''}`;
}

/**
 * One of an entry's own fields, as a row: its name as stored (`key`), its name in words and
 * its value in words.
 */
export interface FieldRow {
  key: string;
  name: string;
  value: string;
  /** Where the value leads, when somewhere: a reference's PDF, in the viewer, or a result's frontier. */
  href: string | null;
}

/** A reference's PDF in the viewer, when its field pins the artifact revision and its hash. */
function pdfField(project: string, value: unknown): string | null {
  if (value === null || typeof value !== 'object') return null;
  const { artifact_id, revision, sha256 } = value as Record<string, unknown>;
  if (typeof artifact_id !== 'string' || artifact_id === '') return null;
  if (!Number.isSafeInteger(revision) || (revision as number) < 1) return null;
  if (typeof sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(sha256)) return null;
  return pdfHref(project, artifact_id, revision as number, sha256);
}

/** A result's frontier category's frontier, when the field names one as the server takes it. */
function categoryField(project: string, value: unknown): string | null {
  return typeof value === 'string' && CATEGORY_NAME.test(value)
    ? categoryHref(project, value)
    : null;
}

/** The names of the fields the types give their entries (docs/LEDGER.md), in words. */
export const FIELD_NAMES: Record<string, string> = {
  kind: 'Kind',
  caveats: 'Caveats',
  reopen_condition: 'Reopen condition',
  session: 'For session',
  category: 'Frontier category',
  conditions: 'Conditions',
  measures: 'Measures',
  identifiers: 'Identifiers',
  authors: 'Authors',
  published: 'Published',
  version: 'Version',
  retrieved: 'Retrieved',
  pdf: 'PDF',
  excluded_scope: 'Excluded scope',
  assessment: 'Assessment',
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
export function fieldRows(
  fields: Record<string, unknown>,
  project: string,
  links: readonly EntryLink[] = [],
): FieldRow[] {
  const known = Object.keys(FIELD_NAMES);
  const names = [
    ...known.filter((name) => name in fields),
    ...Object.keys(fields)
      .filter((name) => !known.includes(name))
      .sort(),
  ];
  return names.map((name) => {
    if (name === 'assessment') return assessmentField(project, fields[name], links);
    return {
      key: name,
      name: FIELD_NAMES[name] ?? statusText(name),
      // A claim's kind is a word of the spec's, such as `negative_result`. Every value is
      // the author's text, each character a person may not see shown as its code point.
      value: visible(
        name === 'kind' ? statusText(valueText(fields[name])) : valueText(fields[name]),
      ),
      href:
        name === 'pdf'
          ? pdfField(project, fields[name])
          : name === 'category'
            ? categoryField(project, fields[name])
            : null,
    };
  });
}

/**
 * A decision's assessment (LG-7): the work record that accepted the obstruction it rests
 * on, which is on the issue the decision affects; leading to that issue, whose work
 * records list it.
 */
function assessmentField(project: string, value: unknown, links: readonly EntryLink[]): FieldRow {
  const issue =
    links
      .map((link) => (link.kind === 'affects' && link.target === 'entry' ? link.handle : null))
      .find((handle) => handle !== null && /^I-[1-9][0-9]{0,8}$/.test(handle)) ?? null;
  const record = visible(valueText(value));
  return {
    key: 'assessment',
    name: FIELD_NAMES.assessment!,
    value: issue === null ? `work record ${record}` : `work record ${record}, on ${issue}`,
    href: issue === null ? null : ledgerEntryHref(project, issue),
  };
}

/**
 * Where a revision came from, when not straight from a tool call: a results file the agent
 * imported (LG-41), by its path and line, with the start of its SHA-256.
 */
export function provenanceText(provenance: Record<string, unknown> | null): string {
  if (provenance === null) return '';
  const { file, line, sha256 } = provenance;
  if (typeof file === 'string' && typeof line === 'number') {
    const digest = typeof sha256 === 'string' ? ` (sha256 ${sha256.slice(0, 12)}…)` : '';
    return `imported from results file ${file}, line ${line}${digest}`;
  }
  return `imported: ${JSON.stringify(provenance)}`;
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
