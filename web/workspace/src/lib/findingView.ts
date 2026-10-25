// SPDX-License-Identifier: MIT
/**
 * What the finding card says and offers (E-11; docs/specs/consistency-review.md, CR-41):
 * what a finding's status means, which decisions the user may make in it, where each of its
 * anchors points and how to open it, and its history in words.
 */
import type { Json } from './api';
import { locatorText, when } from './reviewText';
import type { Finding, Review, Transition } from './reviews';
import { documentHref } from './router';

const STATUSES: Readonly<Record<string, string>> = {
  open: 'Open: raised and not settled; it needs a fix, a dismissal or a decision.',
  fixed: 'Fixed: a re-check found it resolved on a later revision, or the user set it. Settled.',
  dismissed:
    'Dismissed: the agent or the user set it aside with a reason, kept beside it. An ' +
    "agent's dismissal stays unresolved for the gate until a review accepts the reason or " +
    'the user dismisses it.',
  superseded:
    'Superseded: a later review replaced it with a finding of its own, or the user set it so.',
  stale:
    'Stale: what it reviewed has a newer revision and no re-check ran within its session; ' +
    'neither fixed nor open. A later re-check or the user settles it.',
};

const LABELS: Readonly<Record<string, string>> = {
  open: 'Open',
  fixed: 'Fixed',
  dismissed: 'Dismissed',
  superseded: 'Superseded',
  stale: 'Stale',
};

type Recheck = Pick<Finding, 'status' | 'latest_recheck'>;
const INCONCLUSIVE = 'open, last re-check inconclusive';

/** Whether an open finding's last re-check could not decide it. */
function inconclusive(finding: Recheck): boolean {
  return finding.status === 'open' && finding.latest_recheck?.answer === 'inconclusive';
}

/** Whether a finding is settled (folded in the list); open and stale stay visible. */
export function resolved(status: string): boolean {
  return status === 'fixed' || status === 'superseded' || status === 'dismissed';
}

/** What a finding's status means; an open finding with an inconclusive re-check says so. */
export function statusText(status: string, latest?: Recheck['latest_recheck']): string {
  const text = Object.hasOwn(STATUSES, status) ? STATUSES[status]! : status;
  return status === 'open' && latest?.answer === 'inconclusive'
    ? `${text} Its last re-check was inconclusive: the reviewer could not decide it.`
    : text;
}

/** The status chip's label: the status, and for an open finding the re-check that could not decide it. */
export function statusLabel(finding: Recheck): string {
  if (inconclusive(finding)) return `Open, last re-check inconclusive`;
  return Object.hasOwn(LABELS, finding.status) ? LABELS[finding.status]! : finding.status;
}

/** The chip's style class: one per status, nothing for one the card does not know. */
export function statusClass(status: string): string {
  return Object.hasOwn(LABELS, status) ? `status-${status}` : 'status-unknown';
}

const SET_STATUSES = ['open', 'fixed', 'superseded', 'stale'] as const;
type SetStatus = (typeof SET_STATUSES)[number];

/** A decision's value in the draft: a fix, a dismissal, or `set:` and the status to set. */
export type DecisionAction = 'fix' | 'dismiss' | `set:${SetStatus}`;

export interface Decision {
  action: DecisionAction;
  label: string;
}

/** The status a decision sets, or null for a fix or a dismissal. */
export function setStatus(action: string): SetStatus | null {
  const status = action.startsWith('set:') ? action.slice(4) : '';
  return (SET_STATUSES as readonly string[]).includes(status) ? (status as SetStatus) : null;
}

const DECISIONS: Readonly<Record<'fix' | 'dismiss', string>> = {
  fix: 'Claim fixed and queue a re-check (name the changed revisions)',
  dismiss: 'Dismiss, as the user',
};

/**
 * The decisions the user may make on a finding in this status: claim a fix (open or stale
 * only; it queues a re-check and leaves the status), dismiss (unless dismissed), and set
 * any other status, with a reason.
 */
export function decisions(status: string): Decision[] {
  const list: Decision[] = [];
  if (status === 'open' || status === 'stale') list.push({ action: 'fix', label: DECISIONS.fix });
  if (status !== 'dismissed') list.push({ action: 'dismiss', label: DECISIONS.dismiss });
  for (const next of SET_STATUSES)
    if (next !== status) list.push({ action: `set:${next}`, label: `Set to ${next}, as the user` });
  return list;
}

/** A dismissal in words: who gave it, why, and what a review made of it. */
export function dismissalText(dismissal: NonNullable<Finding['dismissal']>): string[] {
  const who = dismissal.by === 'user' ? 'the user' : actorText(dismissal.actor);
  const lines = [`Dismissed by ${who} · ${when(dismissal.at)}: ${dismissal.reason}`];
  if (dismissal.review) {
    const { review, decision, reason } = dismissal.review;
    lines.push(`Review ${review} ${decision} the dismissal: ${reason}`);
  }
  return lines;
}

/** The finding this one replaced and the one that replaced it, in words. */
export function successorText(finding: Pick<Finding, 'supersedes' | 'superseded_by'>): string[] {
  const lines: string[] = [];
  if (finding.supersedes) lines.push(`It replaced finding ${finding.supersedes}.`);
  if (finding.superseded_by) lines.push(`It was replaced by finding ${finding.superseded_by}.`);
  return lines;
}

/** A finding's re-check state in words: the last answer, a queued re-check, the re-flag bound. */
export function recheckText(
  finding: Pick<Finding, 'latest_recheck' | 'recheck' | 'at_bound'>,
): string[] {
  const lines: string[] = [];
  const last = finding.latest_recheck;
  if (last) lines.push(`Last re-check, review ${last.review}: ${last.answer}. ${last.why}`);
  if (finding.recheck)
    lines.push(
      `A re-check is queued (${finding.recheck.cause === 'fix' ? 'after a claimed fix' : 'after a revision'}).`,
    );
  if (finding.at_bound) lines.push('At its re-flag bound: the agent is not offered it again.');
  return lines;
}

/** A finding's anchor, as the card shows it: where it points, and how to open it. */
export interface AnchorView {
  text: string;
  /** The document revision, with the anchored block in focus. */
  href: string | null;
  /** The pinned evidence slice behind it, readable in the panel. */
  evidence: { review: string; evidence: string } | null;
  /** The math check that raised it, and the formula. */
  check: { check: string; math: string | undefined } | null;
}

type Anchor = Finding['anchors'][number];
const text = (value: Json | undefined): string =>
  typeof value === 'string' || typeof value === 'number' ? String(value) : '';

export function anchorView(project: string, anchor: Anchor): AnchorView {
  const locator = anchor.locator ?? {};
  const block = text(anchor['block'] as Json) || text(locator['block']);
  const math = anchor.math ? `, formula ${anchor.math}` : '';
  const lines =
    typeof anchor['first_line'] === 'number'
      ? `, ${text(anchor['stream'] as Json)} lines ${text(anchor['first_line'] as Json)}–${text(anchor['last_line'] as Json)}`
      : '';
  let where = locatorText({ ...locator, ...(block ? { block } : {}) });
  let href: string | null = null;
  const document = text(locator['document']);
  const revision = locator['revision'];
  if (text(locator['comment']))
    where = `comment ${text(locator['comment'])} in document ${document}`;
  else if (text(locator['finding']) && locator['response_revision'] !== undefined)
    where = `the explanation of finding ${text(locator['finding'])}, response ${text(locator['response_revision'])}`;
  else if (document && typeof revision === 'number')
    href = documentHref(project, document, revision, block || undefined);
  return {
    text: `${where}${math}${lines}`,
    href,
    evidence:
      anchor.review && anchor.evidence
        ? { review: anchor.review, evidence: anchor.evidence }
        : null,
    check: anchor.check ? { check: anchor.check, math: anchor.math } : null,
  };
}

/** Who made a decision, in words, from the actor a transition records. */
export function actorText(actor: string): string {
  const [kind, ...rest] = actor.split(':');
  const id = rest.join(':');
  if (kind === 'review') return `review ${id}`;
  if (kind === 'session') return `the main agent of session ${id}`;
  if (kind === 'user') return 'the user';
  if (actor === 'platform:math') return 'the math check';
  if (kind === 'platform') return `the platform (${id})`;
  if (kind === 'harness') return 'the harness';
  return actor;
}

/** One step of a finding's history in words: its new status, who decided and when. */
export function transitionText(entry: Transition): string {
  const former = entry.former_status ? ` (recorded as ${entry.former_status})` : '';
  const answer = entry.answer ? `, re-check ${entry.answer}` : '';
  return `Revision ${entry.revision}: ${entry.status}${former}${answer}, by ${actorText(entry.actor)} · ${when(entry.created)}`;
}

const SEVERITIES: Readonly<Record<string, string>> = {
  fail: 'a materially false or unsupported durable claim, or a claimed action without a record',
  warn: 'a claim that needs correction or qualification',
  note: 'information',
};

/** What a severity means (CR-18). */
export function severityText(severity: string): string {
  return Object.hasOwn(SEVERITIES, severity) ? SEVERITIES[severity]! : severity;
}

/** Which review raised a finding, and how (CR-17), as far as the reviews loaded say. */
export function raisedText(finding: Finding, review: Review | undefined): string {
  if (finding.review_id === null) return 'Raised by the platform.';
  if (!review) return `Raised by review ${finding.review_id}.`;
  const model = review.resolved_model ?? 'an unknown model';
  const alias = review.alias ? `, alias ${review.alias}` : '';
  return `Raised by ${review.kind} review ${review.review_id}: ${model}${alias}, rubric ${review.rubric}.`;
}
