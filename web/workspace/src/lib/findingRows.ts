// SPDX-License-Identifier: MIT
/**
 * The reviewer's findings as rows of the conversation (B10, item 4; consistency-review.md,
 * CR-41, CR-46): a `reviews` (or `math`) notice names the open findings the author was
 * offered, as `<finding>-r<revision>`, and each becomes a compact card. The card reads the
 * finding as it is now (`…/findings/{f}`), so a status changed after the notice, by a
 * re-check, the author or the user, shows on the row already there. The statement is the
 * reviewer's text, shown as text with every hidden character as its code point.
 */
import type { Json } from './api';
import { anchorView, statusClass, statusLabel } from './findingView';
import type { Finding } from './reviews';
import { findingHref, ledgerEntryHref } from './router';
import { visible } from './text';

/** A finding a notice named, at the revision it was offered. */
export interface FindingNotice {
  finding: string;
  revision: number;
}

/** The notice sources whose IDs name findings (`<finding>-r<revision>`). */
export const FINDING_SOURCES: ReadonlySet<string> = new Set(['reviews', 'math']);

const NOTICE_ID = /^(.+)-r([1-9][0-9]{0,8})$/;

/**
 * The findings a notice's IDs name, in the notice's order, each once (at the latest
 * revision named); an ID of another shape is left out.
 */
export function noticeFindings(ids: readonly string[]): FindingNotice[] {
  const found = new Map<string, number>();
  for (const id of ids) {
    const match = NOTICE_ID.exec(id);
    if (!match) continue;
    const [, finding, revision] = match;
    found.set(finding!, Math.max(found.get(finding!) ?? 0, Number(revision)));
  }
  return [...found].map(([finding, revision]) => ({ finding, revision }));
}

/** The most characters of a statement the row shows; the full card has the rest. */
export const STATEMENT_CHARS = 600;

/** The most anchors the row names; the full card lists them all. */
export const ROW_ANCHORS = 3;

/** One of a row's anchors in words, leading to its document block or ledger entry. */
export interface RowAnchor {
  text: string;
  href: string | null;
}

/** What a finding's row shows, from the finding as it is now. */
export interface FindingRowView {
  id: string;
  chip: string;
  chipClass: string;
  severity: string;
  category: string;
  /** The reviewer's statement, its hidden characters as code points, cut to a length. */
  statement: string;
  cut: boolean;
  anchors: RowAnchor[];
  moreAnchors: number;
  /** The finding that replaced it, when one did. */
  successor: { id: string; href: string } | null;
  /** The latest re-check's answer in words, or null when none ran. */
  recheck: string | null;
  /** The full card, in the session's review panel. */
  card: string;
  /** The finding moved on since the revision the notice offered. */
  revised: boolean;
}

const text = (value: Json | undefined): string =>
  typeof value === 'string' || typeof value === 'number' ? String(value) : '';

/** An anchor in words, leading to a document block or to a ledger entry it names. */
export function rowAnchor(project: string, anchor: Finding['anchors'][number]): RowAnchor {
  const view = anchorView(project, anchor);
  const locator = anchor.locator ?? {};
  const entry = text(locator['entry']);
  const revision = locator['revision'];
  const href =
    view.href ??
    (entry
      ? ledgerEntryHref(project, entry, typeof revision === 'number' ? revision : undefined)
      : null);
  return { text: visible(view.text), href };
}

/** The latest re-check's answer: resolved, unresolved, or "open, last re-check inconclusive". */
export function recheckLine(finding: Pick<Finding, 'status' | 'latest_recheck'>): string | null {
  const last = finding.latest_recheck;
  if (!last) return null;
  const why = last.why.trim() ? `: ${visible(last.why.trim())}` : '';
  if (last.answer === 'inconclusive')
    return finding.status === 'open'
      ? `open, last re-check inconclusive${why}`
      : `Last re-check inconclusive${why}`;
  return `Last re-check ${last.answer}${why}`;
}

/** The row of a finding as it is now, offered at `noticed`. */
export function findingRow(
  project: string,
  session: string,
  finding: Finding,
  noticed: number,
): FindingRowView {
  const statement = finding.statement;
  const cut = statement.length > STATEMENT_CHARS;
  const anchors = finding.anchors.map((anchor) => rowAnchor(project, anchor));
  const successor = finding.superseded_by ?? finding.latest_recheck?.successor ?? null;
  return {
    id: visible(finding.finding_id),
    chip: statusLabel(finding),
    chipClass: statusClass(finding.status),
    severity: visible(finding.severity),
    category: visible(finding.category.replaceAll('_', ' ')),
    statement: visible(cut ? `${statement.slice(0, STATEMENT_CHARS - 1)}…` : statement),
    cut,
    anchors: anchors.slice(0, ROW_ANCHORS),
    moreAnchors: Math.max(0, anchors.length - ROW_ANCHORS),
    successor: successor
      ? { id: visible(successor), href: findingHref(project, session, successor) }
      : null,
    recheck: recheckLine(finding),
    card: findingHref(project, session, finding.finding_id),
    revised: finding.revision > noticed,
  };
}

/** What the conversation has read of the findings its rows show, by ID. */
export interface FindingState {
  found: Record<string, Finding>;
  failed: Record<string, string>;
}

export function emptyFindings(): FindingState {
  return { found: {}, failed: {} };
}

/** The most findings one refresh reads again: the latest the conversation shows. */
export const REFRESH_AT_MOST = 20;

/**
 * The findings a session's conversation shows, read and read again: each once when it is
 * first shown, and the latest `REFRESH_AT_MOST` at each refresh while the session is live,
 * or after the review panel changed one (`invalidate`). The view makes the state with
 * `$state`; a test passes a plain object.
 */
export class SessionFindings {
  #wanted: string[] = [];
  #read = new Set<string>();

  constructor(
    readonly state: FindingState,
    private readonly fetch: (finding: string, signal?: AbortSignal) => Promise<Finding>,
    /** Told when a finding not read yet is wanted, so its poller can read it at once. */
    private readonly wanted: () => void = () => {},
  ) {}

  /** The findings the rows show; those new to the list are read at the next refresh. */
  want(ids: Iterable<string>): void {
    let added = false;
    for (const id of ids)
      if (!this.#wanted.includes(id)) {
        this.#wanted.push(id);
        added = true;
      }
    if (added) this.wanted();
  }

  /** Read every finding again at the next refresh: one may have changed. */
  invalidate(): void {
    this.#read.clear();
    this.wanted();
  }

  /** Read those not read yet, and with `live` the latest shown again. */
  async refresh(live: boolean, signal?: AbortSignal): Promise<void> {
    const latest = this.#wanted.slice(-REFRESH_AT_MOST);
    const due = this.#wanted.filter((id) => !this.#read.has(id) || (live && latest.includes(id)));
    await Promise.all(
      due.map(async (id) => {
        try {
          const finding = await this.fetch(id, signal);
          if (signal?.aborted) return;
          this.state.found[id] = finding;
          delete this.state.failed[id];
          this.#read.add(id);
        } catch (caught) {
          if (signal?.aborted) return;
          this.state.failed[id] =
            caught instanceof Error ? caught.message : 'The finding could not be read.';
          this.#read.add(id);
        }
      }),
    );
  }
}

/** Told when the review panel changed a finding of a session, so the rows read it again. */
const changes = new EventTarget();

export function announceFindingChange(project: string, session: string): void {
  changes.dispatchEvent(new CustomEvent('changed', { detail: `${project}\u0000${session}` }));
}

/** Calls `then` whenever a finding of the session changed; returns the cleanup. */
export function onFindingChange(project: string, session: string, then: () => void): () => void {
  const key = `${project}\u0000${session}`;
  const listener = (event: Event) => {
    if ((event as CustomEvent<string>).detail === key) then();
  };
  changes.addEventListener('changed', listener);
  return () => changes.removeEventListener('changed', listener);
}
