// SPDX-License-Identifier: MIT
/**
 * What the consistency reviews panel says about a review, its evidence and records, the
 * windows left unreviewed and the math checks, in words (E-11; docs/specs/consistency-
 * review.md). Every value comes from the server's records and is shown as text; the raw
 * record stays available behind a toggle in the views.
 */
import type { Json } from './api';
import { formatTime } from './format';
import type { Evidence, MathJob, MathQueue, Review } from './reviews';

/** A record's time: epoch seconds today, ISO text once the answers are typed (E-13). */
export function when(value: number | string | null | undefined): string {
  if (typeof value === 'number') return formatTime(new Date(value * 1000).toISOString());
  return formatTime(value);
}

const OUTCOMES: Readonly<Record<string, string>> = {
  pending: 'waiting to run',
  running: 'running',
  passed: 'passed: no inconsistency found in the reviewed evidence (not a proof)',
  findings: 'reported findings',
  inconclusive: 'inconclusive: the main claims could not be evaluated',
  capped: 'capped: it reached its budget; the findings it recorded stand',
  failed: 'failed: reviewer error; its window is reviewed again',
  dropped: 'dropped: lost to a restart; its window is reviewed again',
};

/** What a review's state means (CR-33); only `passed` reads as a pass. */
export function outcomeText(state: string): string {
  return Object.hasOwn(OUTCOMES, state) ? OUTCOMES[state]! : state;
}

const KINDS: Readonly<Record<string, string>> = {
  checkpoint: 'Checkpoint review',
  terminal: 'Terminal review',
  confirmation: 'Confirmation review',
};

/** A review in one line: its kind, its outcome and when it was recorded. */
export function reviewLine(review: Review): string {
  const kind = Object.hasOwn(KINDS, review.kind) ? KINDS[review.kind]! : `${review.kind} review`;
  return `${kind}, ${review.state} · ${when(review.created)}`;
}

/** What a review covered: its feed range or pinned scope, coverage and cap (CR-14, CR-19). */
export function coverageText(review: Review): string {
  const scope =
    review.kind === 'confirmation'
      ? 'the pinned finding and its fix'
      : `feed entries ${review.start_seq} to ${review.end_seq}`;
  const coverage = review.coverage_complete
    ? 'coverage complete within the available evidence'
    : 'coverage incomplete';
  const cap = review.cap_reached
    ? 'the finding cap was reached'
    : 'the finding cap was not reached';
  return `Covered ${scope}; ${coverage}; ${cap}.`;
}

/** Which model reviewed and what it used. */
export function usageText(review: Review): string {
  const model = review.resolved_model ?? 'an unknown model';
  const alias = review.alias ? ` (alias ${review.alias})` : '';
  return (
    `Reviewed by ${model}${alias} with rubric ${review.rubric}: ${review.calls} model ` +
    `${review.calls === 1 ? 'call' : 'calls'}, ${review.input_tokens} input and ` +
    `${review.output_tokens} output tokens (${review.estimated_tokens} reserved).`
  );
}

const text = (value: Json | undefined): string =>
  typeof value === 'string' || typeof value === 'number' ? String(value) : '';

/**
 * Where a piece of evidence points, in words: a document block, a transcript message, an
 * execution or an artifact, as its locator names it.
 */
export function locatorText(locator: Record<string, Json>): string {
  if (text(locator['document'])) {
    const block = text(locator['block']) ? `, block ${text(locator['block'])}` : '';
    return `document ${text(locator['document'])} revision ${text(locator['revision'])}${block}`;
  }
  if (text(locator['execution'])) {
    const run = text(locator['run']) ? ` in run ${text(locator['run'])}` : '';
    return `execution ${text(locator['execution'])}${run}`;
  }
  if (locator['message'] !== undefined && text(locator['run']))
    return `message ${text(locator['message'])} of run ${text(locator['run'])}`;
  if (locator['artifact'] !== undefined)
    return `an artifact published by run ${text(locator['run']) || 'unknown'}`;
  return 'a pinned record';
}

/** A snapshot item in one line: what it is, where it points and whether it is whole. */
export function evidenceText(item: Evidence): string {
  const complete = item.complete ? 'complete' : 'incomplete: some content is not pinned';
  return `${item.kind}: ${locatorText(item.locator)} (${complete})`;
}

/** A window the reviews could not cover after bounded retries (CR-34). */
export function unreviewedText(window: Json): string {
  const found =
    typeof window === 'object' && window !== null && !Array.isArray(window) ? window : {};
  const failures = text(found['failures']) || 'several';
  if (found['type'] === 'confirmation')
    return `A confirmation review failed ${failures} times; its finding's fix stays unconfirmed.`;
  return `Feed entries ${text(found['start'])} to ${text(found['end'])} were left unreviewed after ${failures} failed reviews.`;
}

/** An audit record in one line. */
export function recordText(record: Json): string {
  const found =
    typeof record === 'object' && record !== null && !Array.isArray(record) ? record : {};
  return `#${text(found['sequence'])} ${text(found['kind'])} · ${when(found['created'] as number | string | undefined)}`;
}

/** A math check in one line: what it checked and what it found. */
export function mathCheckText(check: MathJob): string {
  const what =
    check.kind === 'comment'
      ? 'Comment body'
      : check.kind === 'response'
        ? 'Finding explanation'
        : 'Document';
  return (
    `${what} revision ${check.revision}: ${check.state}, ${check.rejected} rejected, ` +
    `${check.unavailable} unavailable`
  );
}

/** A source waiting for, or held from, its math checks. */
export function queueText(item: MathQueue): string {
  const what =
    item.kind === 'comment'
      ? `comment ${item.entry ?? ''}`.trim()
      : item.kind === 'response'
        ? `finding ${item.finding ?? ''} response`.trim()
        : `document ${item.document ?? ''}`.trim();
  const state = item.blocked ? `held: ${item.blocked}` : item.running ? 'running' : 'queued';
  return `${what} to revision ${item.target} (${item.completed} checked): ${state}`;
}
