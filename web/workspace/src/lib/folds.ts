// SPDX-License-Identifier: MIT
/**
 * Folds in the workspace (docs/specs/context-management.md, CM-15, CM-21, CM-46): the
 * research-state updates a fold's model proposed from folded material, which the user
 * accepts or dismisses; and a run's folds as its record keeps them: each fold or overview
 * (with its level), how it went, the markers retired at the run's last rebuild, and the
 * folded region over its budget. A proposal's text is a model's, from untrusted content:
 * every string is shown as text, with each character a person may not see as its code
 * point.
 */
import type { RunRecord } from './api';
import type { ProposalView } from './api-types';
import { foldText } from './chat';
import { anchorView, type AnchorView, BASIS_WORDS } from './researchState';
import { messageHref } from './router';
import { visible } from './text';

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const MESSAGE_ID = /^msg-[0-9a-f]{16}$/;

/** A waiting proposal, in words. */
export interface ProposalLine {
  id: string;
  kind: string;
  basis: string;
  fields: [string, string][];
  anchors: AnchorView[];
  fold: { text: string; href: string | null };
}

const KINDS: Record<ProposalView['proposal']['op'], (p: ProposalView['proposal']) => string> = {
  issue: (p) => `A new ${p.kind ?? ''} issue`,
  decision: (p) =>
    p.status === 'rejected'
      ? 'A decision: an approach abandoned'
      : 'A decision: a method or scope chosen',
  work: (p) => `A work record on ${p.entry ?? '?'}, ended at a limit (${p.limit ?? '?'})`,
  caveat: (p) => `A caveat on ${p.entry ?? '?'}`,
  assumption: () => 'An assumption',
};

/** A fold's proposal as the page shows it: what it proposes, its basis, its own words,
 * its anchors leading to the archived messages, and the fold that proposed it. */
export function proposalLine(project: string, found: ProposalView): ProposalLine {
  const p = found.proposal;
  const words: [string, string | null][] = [
    ['Title', p.title],
    ['Statement', p.statement],
    ['Reason', p.reason],
    ['Reopen condition', p.reopen_condition],
    ['Tried', p.tried],
    ['Caveat', p.caveat],
    ['Concerns', p.concerns.join(', ') || null],
    ['Reopen on', p.reopen_on.join(', ') || null],
    ['Relied on by', p.relied_on_by.join(', ') || null],
  ];
  return {
    id: visible(found.proposal_id),
    kind: visible(KINDS[p.op](p)),
    basis: BASIS_WORDS[p.basis] ?? visible(p.basis),
    fields: words
      .filter((pair): pair is [string, string] => pair[1] !== null)
      .map(([k, v]) => [k, visible(v)]),
    anchors: found.anchors.map((anchor) => anchorView(project, anchor)),
    fold: {
      text: visible(found.fold),
      href: MESSAGE_ID.test(found.fold) ? messageHref(project, found.fold) : null,
    },
  };
}

/** One fold or overview of a run, as its record keeps it. */
export interface FoldLine {
  text: string;
  /** The fold record's text, read whole from the session's archive. */
  href: string | null;
  proposed: number;
}

/** A run's folds from its record (`summaries.folds`): each fold and overview, the
 * markers its last rebuild retired, by record, and how often the folded region went over
 * its budget. */
export function runFolds(
  project: string,
  run: Pick<RunRecord, 'summaries'>,
): {
  folds: FoldLine[];
  retired: { text: string; href: string | null; count: number }[];
  overs: number;
  weak: number;
} | null {
  const summary = record(run.summaries?.folds);
  if (!Object.keys(summary).length) return null;
  const folds = list(summary.folds).map((raw): FoldLine => {
    const fold = record(raw);
    const id = typeof fold.fold === 'string' ? fold.fold : '';
    const kept = record(fold.proposals).kept;
    return {
      text: foldText(fold),
      href: MESSAGE_ID.test(id) ? messageHref(project, id) : null,
      proposed: typeof kept === 'number' ? kept : 0,
    };
  });
  const rebuilds = list(summary.rebuilds);
  const last = record(rebuilds[rebuilds.length - 1]);
  const retired = Object.entries(record(last.retired))
    .map(([fold, markers]) => ({
      text: visible(fold),
      href: MESSAGE_ID.test(fold) ? messageHref(project, fold) : null,
      count: list(markers).length,
    }))
    .filter((item) => item.count > 0);
  const number = (value: unknown) => (typeof value === 'number' ? value : 0);
  return {
    folds,
    retired,
    overs: number(summary.region_over_budget),
    weak: number(summary.weak_folds),
  };
}
