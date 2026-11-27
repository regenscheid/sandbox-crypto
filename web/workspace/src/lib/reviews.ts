// SPDX-License-Identifier: MIT
/** Review text and pinned evidence are untrusted data, never browser HTML. */
import type { FinalReviewSummary, Json, MacroContext } from './api';
import { get, post } from './http';
export interface Review {
  review_id: string;
  kind: string;
  state: string;
  summary: string;
  created: number;
  alias: string | null;
  resolved_model: string | null;
  rubric: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  estimated_tokens: number;
  cap_reached: boolean | number;
  coverage_complete: boolean | number;
  start_seq: number;
  end_seq: number;
  config_revision: string;
  classifier: Json;
  /** The limits the review ran under (lab 38). */
  config?: ReviewLimits;
}
export interface ReviewLimits {
  token_budget: number;
  calls_per_hour: number;
  max_turns: number;
  time_seconds: number;
}
export interface Evidence {
  evidence: string;
  ordinal: number;
  kind: string;
  locator: Record<string, Json>;
  sha256: string;
  complete: boolean;
}
/** The five statuses of a finding in the review store (consistency-review.md, CR-40). */
export type FindingStatus = 'open' | 'fixed' | 'dismissed' | 'superseded' | 'stale';
export type RecheckAnswer = 'resolved' | 'unresolved' | 'inconclusive';
/** The last re-check of a finding: a review's answer, with the reviewer that gave it. */
export interface LatestRecheck {
  review: string;
  answer: RecheckAnswer;
  why: string;
  anchors: Json;
  places: Json;
  messages: Json;
  reviewer: { alias: string | null; resolved_model: string | null; rubric: string };
  at: number;
  successor?: string | null;
}
/** A dismissal, who gave it and why, and the review that judged it, once one did. */
export interface Dismissal {
  by: 'agent' | 'user';
  actor: string;
  reason: string;
  at: number;
  response_revision?: number;
  review: null | {
    review: string;
    decision: 'accepted' | 'rejected';
    reason: string;
    at: number;
  };
}
/** A re-check waiting to run, and why it was queued. */
export interface QueuedRecheck {
  cause: 'revision' | 'fix';
  revision: number;
  response: number;
  places: Json;
  messages: Json;
  queued: number;
  attempts: number;
}
export interface Finding {
  finding_id: string;
  review_id: string | null;
  origin?: 'review' | 'math';
  source?: Record<string, Json>;
  severity: string;
  category: string;
  statement: string;
  remedy: string | null;
  status: FindingStatus;
  /** The finding this one replaced, and the one that replaced it. */
  supersedes?: string | null;
  superseded_by?: string | null;
  latest_recheck?: LatestRecheck | null;
  dismissal?: Dismissal | null;
  /** A queued re-check. */
  recheck?: QueuedRecheck | null;
  /** Open at its re-flag bound: not offered to the agent again. */
  at_bound?: boolean;
  revision: number;
  reflags: number;
  anchors: ({
    evidence?: string;
    review?: string;
    check?: string;
    math?: string;
    locator: Record<string, Json>;
  } & Record<string, Json>)[];
  history?: Transition[];
  history_before?: number | null;
  /** What it reviewed, each at the revision reviewed beside its current one (CR-46). */
  reviewed_revisions?: ReviewedRevision[];
  response_generation?: number;
  macro_context?: MacroContext;
}
/** A document, artifact or ledger entry a finding reviewed, then and now. */
export interface ReviewedRevision {
  kind: 'document' | 'artifact' | 'entry';
  id: string;
  handle?: string | null;
  reviewed: number;
  /** Its current revision, or null when it can no longer be read. */
  current: number | null;
}
export interface ResponseBody {
  source: string;
  finding: string;
  response_revision: number;
  revision: number;
  body: string;
  sha256: string;
  original_author: Record<string, Json>;
  author: Record<string, Json> | null;
  held?: string | null;
  macro_context?: MacroContext;
}
export interface Transition {
  revision: number;
  status: FindingStatus;
  actor: string;
  actor_kind?: 'user' | 'controller' | 'platform' | 'agent';
  /** The old status name of a migrated row, else null. */
  former_status?: string | null;
  /** A re-check's answer, its review and its record, when the step was one. */
  answer?: RecheckAnswer | null;
  review?: string | null;
  recheck?: Json | null;
  reason: string;
  payload: Json;
  created: number;
  response_body?: ResponseBody;
  macro_context?: MacroContext;
}
export interface MathJob {
  check_id: string;
  document: string | null;
  revision: number;
  state: string;
  project_macro_revision?: number;
  macro_context_status?: string;
  kind?: 'document' | 'comment' | 'response' | 'statement';
  comment?: { entry_id: string; macro_revision: number; macro_sha256: string };
  response?: { source: string; finding: string; response_revision: number; session: string };
  statement?: { entry: string; handle: string; project: string };
  cursor: number;
  spans: number | null;
  available: number;
  rejected: number;
  unavailable: number;
  created: number;
}
export interface MathQueue {
  document?: string;
  target: number;
  completed: number;
  blocked: string | null;
  running: boolean | number;
  kind?: 'comment' | 'response' | 'statement';
  entry?: string;
  handle?: string;
  macro_revision?: number;
  macro_target?: number;
  source?: string;
  finding?: string;
  response_revision?: number;
}
const part = encodeURIComponent;
const base = (project: string, session: string) =>
  `/projects/${part(project)}/sessions/${part(session)}`;
export const reviews = {
  math: (p: string, s: string, after: string | null = null) =>
    get<{ checks: MathJob[]; next_after: string | null; queue: MathQueue[]; queue_more?: boolean }>(
      `${base(p, s)}/math-checks${after ? `?after=${part(after)}` : ''}`,
    ),
  mathEvidence: (p: string, s: string, id: string, after = 0, span?: string) =>
    get<{ check: Json; observations: Json[]; next_after: number | null }>(
      `${base(p, s)}/math-checks/${part(id)}?after=${after}${span ? `&span=${part(span)}` : ''}`,
    ),
  list: (p: string, s: string, after: string | null = null) =>
    get<{
      reviews: Review[];
      next_after: string | null;
      unreviewed: Json[];
      waiting?: Json;
      off?: Json;
    }>(`${base(p, s)}/reviews${after ? `?after=${part(after)}` : ''}`),
  findings: (p: string, s: string, after: string | null = null) =>
    get<{ findings: Finding[]; next_after: string | null }>(
      `${base(p, s)}/findings${after ? `?after=${part(after)}` : ''}`,
    ),
  manifest: (p: string, s: string, r: string, after = 0) =>
    get<{ review: Review; evidence: Evidence[]; next_after: number | null }>(
      `${base(p, s)}/reviews/${part(r)}?after=${after}`,
    ),
  records: (p: string, s: string, r: string, after = 0) =>
    get<{
      records: { sequence: number; kind: string; payload: Json; created: number }[];
      next_after: number | null;
    }>(`${base(p, s)}/reviews/${part(r)}/records?after=${after}`),
  evidence: (p: string, s: string, r: string, e: string, start = 0) =>
    get<{ text: string; next_start: number | null; locator: Json; complete: boolean }>(
      `${base(p, s)}/reviews/${part(r)}/evidence/${part(e)}?start=${start}`,
    ),
  finding: (p: string, s: string, f: string, before: number | null = null) =>
    get<Finding>(`${base(p, s)}/findings/${part(f)}${before ? `?before=${before}` : ''}`),
  responseBody: (p: string, s: string, f: string, response: number, body?: number) =>
    get<ResponseBody>(
      `${base(p, s)}/findings/${part(f)}/responses/${response}${body === undefined ? '' : `?body_revision=${body}`}`,
    ),
  respond: (p: string, s: string, f: string, body: Record<string, unknown>) =>
    post<{ revision: number; status: string }>(`${base(p, s)}/findings/${part(f)}`, body),
  request: (p: string, s: string) =>
    post<{ queued: boolean; detail: string }>(`${base(p, s)}/reviews`, {}),
};
export function parseRevisionReferences(value: string): [string, number][] {
  if (!value.trim()) return [];
  const values = value.trim().split(/\s+/);
  if (values.length > 16) throw new Error('Use at most 16 changed revision references.');
  return values.map((item) => {
    const match = /^(doc-[0-9a-f]{24}):([1-9][0-9]{0,8})$/.exec(item);
    if (!match) throw new Error('Changed revisions use document-ID:revision, separated by spaces.');
    return [match[1]!, Number(match[2]!)];
  });
}

/**
 * A run's final review as one line (CR-48): how it ended, the open findings by severity,
 * whether the answer went out before it, and what is left unreviewed, by feed position:
 * "Final review: findings, after the answer; the agent answered its findings. Open: 1 fail.
 * Unreviewed: positions 1–40 (60 of 90 items reviewed)." Null for a record without one.
 */
export function finalReviewLine(final: FinalReviewSummary | undefined): string | null {
  if (!final) return null;
  const outcome = final.finished === false ? 'did not finish' : (final.outcome ?? 'pending');
  const parts = [
    `Final review: ${outcome}${final.mode === 'after' ? ', after the answer' : ', before the answer'}` +
      (final.answered
        ? '; the agent answered its findings.'
        : final.answering_failed
          ? '; the turn answering its findings failed, so they were not answered.'
          : '.'),
  ];
  const open = Object.entries(final.open ?? {}).filter(([, n]) => n > 0);
  parts.push(
    open.length
      ? `Open: ${open.map(([severity, n]) => `${n} ${severity}`).join(', ')}.`
      : 'No open findings.',
  );
  if (final.fixes_claimed) parts.push(`${final.fixes_claimed} fix claimed, its re-check pending.`);
  const left = final.unreviewed ?? [];
  if (final.complete || !left.length) parts.push('All work reviewed.');
  else {
    const ranges = left.map((r) => (r.start === r.end ? `${r.start}` : `${r.start}–${r.end}`));
    const more = (final.unreviewed_ranges ?? left.length) - left.length;
    const items = final.items;
    parts.push(
      `Unreviewed: positions ${ranges.join(', ')}${more > 0 ? ` and ${more} more ${more === 1 ? 'range' : 'ranges'}` : ''}` +
        (items ? ` (${items.reviewed} of ${items.total} items reviewed).` : '.'),
    );
  }
  if (final.not_recorded_through)
    parts.push(`Coverage is not recorded through position ${final.not_recorded_through}.`);
  return parts.join(' ');
}

/** Why a range of a session's work is unreviewed, in plain words (CR-48, CR-49). */
const UNREVIEWED_REASONS: Record<string, string> = {
  final_budget: "the final review's budget left it out",
  backlog: 'a checkpoint skipped it to reach the newest work',
  deadline: 'a review was ended at its time limit',
  final_inconclusive: 'the final review could not decide it',
  final_capped: "the final review's budget ended before it",
  answering_turn: "it is the work of the turn that answered the final review's findings",
  not_reached: 'no review has reached it yet',
};

/** What an unread item is, in plain words. */
const UNREAD_KINDS: Record<string, string> = {
  artifact: 'a file',
  execution: "a call's output",
  transcript: 'a message of the run',
  unobserved: 'a step the review could not observe',
  source: 'a source the run read',
  document: 'a document',
};

/** How a final review's findings ended, in plain words, with their count. */
const RECHECKS: [string, string][] = [
  ['resolved', 'resolved'],
  ['dismissal_accepted', 'dismissed, the dismissal accepted'],
  ['dismissal_rejected', 'dismissed, the dismissal rejected'],
  ['dismissed', 'dismissed by the user'],
  ['unresolved', 'unresolved'],
  ['inconclusive', 're-checked without a decision'],
  ['not_rechecked', 'not re-checked'],
];

function reason(name: string): string {
  if (name in UNREVIEWED_REASONS) return UNREVIEWED_REASONS[name]!;
  if (name.startsWith('final_')) return 'the final review ended without covering it';
  return name.replace(/_/g, ' ');
}

/**
 * How a run's review ended, as plain lines for its section in the session view (CR-48):
 * why no final review ran, or the items it could not read by name (a file by its path)
 * with why, the unreviewed ranges as a count with their reasons, the open findings by
 * severity, and whether its findings were answered and re-checked, with how each ended.
 * No internal identifiers beyond file paths. Empty for a record without a summary.
 */
export function finalReviewEnd(
  summary: { no_final_review?: string; final_review?: FinalReviewSummary } | undefined,
): string[] {
  if (!summary) return [];
  if (summary.no_final_review) return [summary.no_final_review];
  const final = summary.final_review;
  if (!final) return [];
  const lines: string[] = [];
  for (const unread of final.unread ?? []) {
    const what = unread.path ?? UNREAD_KINDS[unread.kind] ?? `an item (${unread.kind})`;
    lines.push(`Not read: ${what}${unread.why ? `, ${unread.why}` : ''}.`);
  }
  const items = final.items;
  if (items && items.total > 0)
    lines.push(
      `Reviewed: ${items.reviewed} of ${items.total} items of the session's work` +
        (items.unreviewed ? `; ${items.unreviewed} not reviewed.` : '.'),
    );
  const ranges = final.unreviewed_ranges ?? final.unreviewed?.length ?? 0;
  if (ranges > 0) {
    const why = Object.entries(final.unreviewed_reasons ?? {})
      .filter(([, n]) => n > 0)
      .map(([name, n]) => `${n} because ${reason(name)}`);
    lines.push(
      `Unreviewed: ${ranges} ${ranges === 1 ? 'range' : 'ranges'} of the session's work` +
        (why.length ? ` (${why.join('; ')}).` : '.'),
    );
  }
  const open = Object.entries(final.open ?? {}).filter(([, n]) => n > 0);
  lines.push(
    open.length
      ? `Open findings: ${open.map(([severity, n]) => `${n} ${severity}`).join(', ')}.`
      : 'No open findings.',
  );
  const findings = final.findings ?? [];
  if (findings.length) {
    const answered = final.answered
      ? 'The agent answered its findings'
      : final.answering_failed
        ? 'The turn answering its findings failed'
        : 'Its findings were not answered';
    const rechecked =
      final.rechecked && final.recheck
        ? `; one re-check ran (${final.recheck.outcome})`
        : final.answered
          ? '; no re-check ran'
          : '';
    const ended = RECHECKS.map(
      ([key, words]) => [findings.filter((f) => f.recheck === key).length, words] as const,
    )
      .filter(([n]) => n > 0)
      .map(([n, words]) => `${n} ${words}`);
    lines.push(
      `${answered}${rechecked}.` + (ended.length ? ` Findings: ${ended.join(', ')}.` : ''),
    );
  }
  return lines;
}
