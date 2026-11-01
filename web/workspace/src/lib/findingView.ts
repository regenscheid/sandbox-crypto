// SPDX-License-Identifier: MIT
/**
 * What the finding card says and offers (E-11; docs/specs/consistency-review.md, CR-41):
 * what a finding's status means, which decisions the user may make in it, where each of its
 * anchors points and how to open it, and its history in words.
 */
import type { Json } from './api';
import { locatorText, when } from './reviewText';
import type { Finding, Review, ReviewedRevision, Transition } from './reviews';
import { documentHref, findingHref, ledgerEntryHref, messageHref } from './router';
import { visible } from './text';

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

/** A finding's re-check queue in words: a queued re-check, and the re-flag bound. */
export function queuedText(finding: Pick<Finding, 'recheck' | 'at_bound'>): string[] {
  const lines: string[] = [];
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

/** The archive message a locator names, when it names one (`msg-…`). */
function archived(locator: Record<string, Json>): string | null {
  const archive = locator['archive'];
  if (typeof archive !== 'object' || archive === null || Array.isArray(archive)) return null;
  const id = archive['id'];
  return typeof id === 'string' && /^msg-[0-9a-f]{16}$/.test(id) ? id : null;
}

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
  else if (text(locator['entry']))
    href = ledgerEntryHref(
      project,
      text(locator['entry']),
      typeof revision === 'number' ? revision : undefined,
    );
  else if (archived(locator)) href = messageHref(project, archived(locator)!);
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

/** A finding the card links to: its ID and its card. */
export interface FindingLink {
  id: string;
  href: string;
}

/**
 * The finding this one replaced and the one that replaced it (CR-44), each linked to its
 * card; the successor is the one the user set, or the one an unresolved re-check raised.
 */
export function lineage(
  project: string,
  session: string,
  finding: Pick<Finding, 'supersedes' | 'superseded_by' | 'latest_recheck'>,
): { predecessor: FindingLink | null; successor: FindingLink | null } {
  const link = (id: string | null | undefined): FindingLink | null =>
    id ? { id, href: findingHref(project, session, id) } : null;
  return {
    predecessor: link(finding.supersedes),
    successor: link(finding.superseded_by ?? finding.latest_recheck?.successor),
  };
}

/** One thing a finding reviewed, at the revision reviewed and now (CR-46). */
export interface RevisionView {
  /** What it is, in words: a document, an artifact or a ledger entry by its handle. */
  what: string;
  reviewed: number;
  reviewedHref: string | null;
  current: number | null;
  currentHref: string | null;
  /** Its current revision, in words: the same, a later one, or unreadable. */
  state: string;
}

/** What a finding reviewed and where each now stands, as the server read it. */
export function revisionViews(
  project: string,
  revisions: readonly ReviewedRevision[] | undefined,
): RevisionView[] {
  return (revisions ?? []).map((item) => {
    const href = (revision: number) =>
      item.kind === 'document'
        ? documentHref(project, item.id, revision)
        : item.kind === 'entry'
          ? ledgerEntryHref(project, item.id, revision)
          : null;
    const what =
      item.kind === 'entry'
        ? `ledger entry ${item.handle || item.id}`
        : `${item.kind === 'document' ? 'document' : 'artifact'} ${item.id}`;
    const current = item.current;
    const state =
      current === null
        ? 'its current revision could not be read'
        : current > item.reviewed
          ? `now at revision ${current}`
          : 'still the current revision';
    return {
      what: visible(what),
      reviewed: item.reviewed,
      reviewedHref: href(item.reviewed),
      current,
      currentHref: current !== null && current !== item.reviewed ? href(current) : null,
      state,
    };
  });
}

/** The latest re-check as the card shows it (CR-42, CR-46). */
export interface RecheckView {
  /** "resolved", "unresolved", or "open, last re-check inconclusive" for an open finding. */
  answer: string;
  /** Why, as the reviewer (or the controller) gave it: agent text, hidden characters shown. */
  why: string;
  /** Which review answered, with which model, and when. */
  by: string;
  /** The evidence anchors it gave in the new revision or message. */
  anchors: AnchorView[];
  /** The new revisions it looked at, in words, with links where the workspace has a page. */
  places: { text: string; href: string | null }[];
}

const record = (value: Json | undefined): Record<string, Json> =>
  typeof value === 'object' && value !== null && !Array.isArray(value) ? value : {};

/** A place a re-check looked at (a later document, artifact or entry revision), in words. */
function placeView(project: string, place: Json): { text: string; href: string | null } {
  const found = record(place);
  const document = record(found['document']);
  const artifact = record(found['artifact']);
  const entry = record(found['entry']);
  if (text(document['document']) && typeof document['revision'] === 'number')
    return {
      text: visible(locatorText(document)),
      href: documentHref(
        project,
        text(document['document']),
        document['revision'],
        text(document['block']) || undefined,
      ),
    };
  if (text(artifact['artifact_id']))
    return {
      text: visible(
        `artifact ${text(artifact['artifact_id'])} revision ${text(artifact['revision'])}`,
      ),
      href: null,
    };
  if (text(entry['entry']))
    return {
      text: visible(locatorText(entry)),
      href: ledgerEntryHref(
        project,
        text(entry['entry']),
        typeof entry['revision'] === 'number' ? entry['revision'] : undefined,
      ),
    };
  return { text: 'a later record', href: null };
}

/** The latest re-check's answer, reviewer, evidence and places, or null when none ran. */
export function recheckView(
  project: string,
  finding: Pick<Finding, 'status' | 'latest_recheck'>,
): RecheckView | null {
  const last = finding.latest_recheck;
  if (!last) return null;
  const answer =
    last.answer === 'inconclusive' && finding.status === 'open'
      ? INCONCLUSIVE
      : `last re-check ${last.answer}`;
  const model = last.reviewer?.resolved_model ?? 'an unknown model';
  const alias = last.reviewer?.alias ? `, alias ${last.reviewer.alias}` : '';
  const anchors = Array.isArray(last.anchors) ? (last.anchors as Anchor[]) : [];
  const places = Array.isArray(last.places) ? last.places : [];
  return {
    answer,
    why: visible(last.why),
    by: visible(
      `Review ${last.review} · ${model}${alias}${typeof last.at === 'number' ? ` · ${when(last.at)}` : ''}`,
    ),
    anchors: anchors.map((anchor) => {
      const view = anchorView(project, anchor);
      return { ...view, text: visible(view.text) };
    }),
    places: places.map((place) => placeView(project, place)),
  };
}

/** A dismissal as the card shows it: who gave it, when, the reason, and a review's judgement. */
export interface DismissalView {
  who: string;
  at: string;
  /** The reason: the user's own text as written, an agent's with hidden characters shown. */
  reason: string;
  byAgent: boolean;
  review: { text: string; reason: string } | null;
}

export function dismissalView(dismissal: NonNullable<Finding['dismissal']>): DismissalView {
  const byAgent = dismissal.by !== 'user';
  const judged = dismissal.review;
  return {
    who: byAgent ? actorText(dismissal.actor) : 'the user',
    at: when(dismissal.at),
    reason: byAgent ? visible(dismissal.reason) : dismissal.reason,
    byAgent,
    review: judged
      ? {
          text: `Review ${judged.review} ${judged.decision} the dismissal · ${when(judged.at)}`,
          reason: visible(judged.reason),
        }
      : byAgent
        ? { text: 'No review has judged the dismissal yet.', reason: '' }
        : null,
  };
}

const SUCCESSOR = /^finding-[0-9a-f]{24}$/;

/** The successor a user names when setting a finding superseded: empty, or a finding ID. */
export function parseSuccessor(value: string): string | null {
  const found = value.trim();
  if (!found) return null;
  if (!SUCCESSOR.test(found))
    throw new Error('A successor is a finding ID: finding- and 24 hex digits.');
  return found;
}
