// SPDX-License-Identifier: MIT
/**
 * What the finding card says and offers (E-11; docs/specs/consistency-review.md, CR-27):
 * what a finding's status means, which decisions the user may make in it, where each of its
 * anchors points and how to open it, and its history in words.
 */
import type { Json } from './api';
import { locatorText, when } from './reviewText';
import type { Finding, Review, Transition } from './reviews';
import { documentHref } from './router';

const STATUSES: Readonly<Record<string, string>> = {
  open: 'Open: raised, or re-opened by a rejected fix or rebuttal.',
  'claimed fixed': 'Claimed fixed: the author or the user says it is fixed; not yet checked.',
  'confirmed fixed': 'Confirmed fixed by a confirmation review or the user. Resolved.',
  unaddressed:
    'Unaddressed: the response rounds ended without a confirmed fix or an accepted rebuttal.',
  superseded: 'Superseded: the revisions it is anchored to were replaced; nothing was checked.',
  inconclusive: 'Inconclusive: the reviewer could not decide from the evidence.',
  withdrawn: 'Withdrawn: a rebuttal was accepted, or the user dismissed it. Resolved.',
  challenged:
    'Challenged: the reviewer rejected a rebuttal that cites recorded evidence. The ' +
    "agent's rebuttal stands and the reviewer's reason is kept beside it, with no further " +
    'round; look at both whenever you like.',
};

/** Whether a finding is resolved; every other status stays visible as unresolved. */
export function resolved(status: string): boolean {
  return status === 'confirmed fixed' || status === 'withdrawn';
}

/** What a finding's status means. */
export function statusText(status: string): string {
  return Object.hasOwn(STATUSES, status) ? STATUSES[status]! : status;
}

export interface Decision {
  action: 'rebut' | 'fix' | 'confirm' | 'dismiss' | 'reopen';
  label: string;
}

const DECISIONS: Readonly<Record<Decision['action'], string>> = {
  rebut: 'Rebut with an explanation',
  fix: 'Claim fixed (name the changed revisions)',
  confirm: 'Confirm fixed, as the user',
  dismiss: 'Dismiss, as the user',
  reopen: 'Reopen',
};

/**
 * The decisions the user may make on a finding in this status. A resolved finding is
 * reopened before anything else (the server refuses a fix or a rebuttal on it); an open
 * one has nothing to reopen.
 */
export function decisions(status: string): Decision[] {
  const actions: Decision['action'][] = resolved(status)
    ? ['reopen']
    : status === 'open'
      ? ['rebut', 'fix', 'confirm', 'dismiss']
      : ['rebut', 'fix', 'confirm', 'dismiss', 'reopen'];
  return actions.map((action) => ({ action, label: DECISIONS[action] }));
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
  return `Revision ${entry.revision}: ${entry.status}, by ${actorText(entry.actor)} · ${when(entry.created)}`;
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
