// SPDX-License-Identifier: MIT
/**
 * A review window's actions (docs/specs/consistency-review.md, CR-2 as amended for lab
 * 40): the item the snapshot derives from the runs' tool records, each run's calls that
 * change something, in order, with their target, outcome and what they made, a delegated
 * run's naming its delegation, and the blocks each document revision in the window changed. The view lists them by run, marks
 * a refused or failed call, and leads to what was made where the workspace has a page.
 * Targets and paths are the agent's arguments, shown with each character a person may not
 * see as its code point.
 */
import type { LinkTarget } from './ledger';
import type { Evidence } from './reviews';
import { documentHref, ENTRY_NAME, ledgerEntryHref, runHref, treeHref } from './router';
import { visible } from './text';

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
const count = (value: unknown): number | null =>
  Number.isSafeInteger(value) && (value as number) >= 0 ? (value as number) : null;

/** One call that changes something, as the item lists it. */
export interface Action {
  tool: string;
  /** The target as its arguments name it: each argument and its value. */
  target: [string, string][];
  outcome: string;
  /** What it made, in the platform's words, such as `document doc-1 r3`. */
  made: string[];
}

/** A document revision in the window and the blocks it changed since the one before. */
export interface DocumentChange {
  document: string;
  revision: number | null;
  since: number | null;
  changed: string[];
  changedTotal: number;
  removed: string[];
  removedTotal: number;
}

/** One run's actions; a delegated run's name the delegation it ran for. */
export interface RunActions {
  run: string;
  delegation: string | null;
  actions: Action[];
}

export interface WindowActions {
  /**
   * The runs in the order their first action comes, each with its actions in order; a
   * delegated run's apart from its parent's, under its delegation.
   */
  runs: RunActions[];
  shown: number;
  total: number;
  documents: DocumentChange[];
}

/** The actions item's window, in words, for the item list; empty for another item. */
export function actionsText(item: Pick<Evidence, 'kind' | 'locator'>): string {
  if (item.kind !== 'actions') return '';
  const from = count(item.locator['from']);
  const to = count(item.locator['to']);
  const span = from === null || to === null ? '' : `, feed entries ${from} to ${to}`;
  return (
    `What the runs did${span}: their calls that change something, derived from the ` +
    'platform’s tool records, and the blocks each document revision changed'
  );
}

/** The actions item read whole; null when its text is not one. */
export function actionsOf(text: string): WindowActions | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  const payload = record(parsed);
  if (!Array.isArray(payload.actions)) return null;
  const runs: RunActions[] = [];
  for (const raw of payload.actions) {
    const action = record(raw);
    const run = typeof action.run === 'string' ? action.run : '';
    const delegation = typeof action.delegation === 'string' ? action.delegation : null;
    let group = runs.find((found) => found.run === run && found.delegation === delegation);
    if (group === undefined) {
      group = { run, delegation, actions: [] };
      runs.push(group);
    }
    group.actions.push({
      tool: typeof action.tool === 'string' ? action.tool : '?',
      target: Object.entries(record(action.target)).map(([key, value]) => [key, String(value)]),
      outcome: typeof action.outcome === 'string' ? action.outcome : '?',
      made: strings(action.made),
    });
  }
  const documents = (Array.isArray(payload.documents) ? payload.documents : []).map((raw) => {
    const change = record(raw);
    const changed = strings(change.changed);
    const removed = strings(change.removed);
    return {
      document: typeof change.document === 'string' ? change.document : '?',
      revision: count(change.revision),
      since: count(change.since),
      changed,
      changedTotal: count(change.changed_total) ?? changed.length,
      removed,
      removedTotal: count(change.removed_total) ?? removed.length,
    };
  });
  const shown = runs.reduce((sum, run) => sum + run.actions.length, 0);
  return { runs, shown, total: count(payload.total) ?? shown, documents };
}

/** Whether a call's outcome is one to mark: the store or a gate refused it, or it failed. */
export function marked(outcome: string): boolean {
  return outcome !== 'ok';
}

/** A call's target in words, each value as the agent gave it. */
export function targetText(action: Pick<Action, 'target'>): string {
  return action.target.map(([key, value]) => `${visible(key)} ${visible(value)}`).join(', ');
}

/**
 * What a call made, leading to its page where the workspace has one: a document revision,
 * a ledger entry's revision, or a delegation, in the session's run tree. Work records,
 * artifact revisions, executions and files are named.
 */
export function madeLink(project: string, session: string, made: string): LinkTarget {
  const text = visible(made);
  const document = /^document (.+) r([1-9][0-9]{0,8})$/.exec(made);
  if (document) {
    return { text, href: documentHref(project, document[1]!, Number(document[2])) };
  }
  const entry = /^ledger (\S+) r([1-9][0-9]{0,8})$/.exec(made);
  if (entry && ENTRY_NAME.test(entry[1]!)) {
    return { text, href: ledgerEntryHref(project, entry[1]!, Number(entry[2])) };
  }
  if (/^delegation \S+$/.test(made)) return { text, href: treeHref(project, session) };
  return { text, href: null };
}

/** A block a document revision changed, leading to it at that revision. */
export function blockLink(project: string, change: DocumentChange, block: string): LinkTarget {
  return {
    text: visible(block),
    href:
      change.revision !== null && /^b[0-9]{1,9}$/.test(block)
        ? documentHref(project, change.document, change.revision, block)
        : null,
  };
}

/** A run's events, when the item names the run. */
export function runLink(project: string, session: string, run: string): LinkTarget {
  return run
    ? { text: visible(run), href: runHref(project, session, run) }
    : { text: 'a run the record does not name', href: null };
}
