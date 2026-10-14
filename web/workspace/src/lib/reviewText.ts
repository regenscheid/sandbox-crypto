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
  if (typeof value !== 'number') return formatTime(value);
  const time = new Date(value * 1000);
  return Number.isNaN(time.getTime()) ? String(value) : formatTime(time.toISOString());
}

const OUTCOMES: Readonly<Record<string, string>> = {
  pending: 'waiting to run',
  running: 'running',
  passed: 'passed: no inconsistency found in the reviewed evidence (not a proof)',
  findings: 'reported findings',
  inconclusive: 'inconclusive: the main claims could not be evaluated',
  capped: 'capped: it reached its budget or its finding cap; the findings it recorded stand',
  failed: 'failed: reviewer error',
  dropped: 'dropped: lost to a restart',
};

/**
 * What a review's state means (CR-33); only `passed` reads as a pass. A failed or dropped
 * review's window is reviewed again, at most twice, and then left unreviewed (CR-34):
 * `unreviewed` says which.
 */
export function outcomeText(state: string, unreviewed = false): string {
  const said = Object.hasOwn(OUTCOMES, state) ? OUTCOMES[state]! : state;
  if (state !== 'failed' && state !== 'dropped') return said;
  return unreviewed
    ? `${said}; its window was left unreviewed after bounded retries`
    : `${said}; its window is due to be reviewed again`;
}

/** Whether the windows left unreviewed name this review. */
export function leftUnreviewed(windows: readonly Json[], review: string): boolean {
  return windows.some(
    (window) =>
      typeof window === 'object' &&
      window !== null &&
      !Array.isArray(window) &&
      window['review'] === review,
  );
}

/** States in which a review recorded what it covered. */
const COMPLETED = new Set(['passed', 'findings', 'inconclusive', 'capped']);

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
  if (!COMPLETED.has(review.state))
    return `Its window was ${scope}; nothing was recorded as covered.`;
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
  if (text(locator['finding']) && locator['response_revision'] === undefined)
    return `finding ${text(locator['finding'])} revision ${text(locator['revision'])}`;
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
  const head = `#${text(found['sequence'])} ${text(found['kind'])}`;
  const time = when(found['created'] as number | string | undefined);
  // What the reviewer checked and found consistent (CR-7).
  const payload = found['payload'];
  const checked =
    typeof payload === 'object' && payload !== null && !Array.isArray(payload)
      ? payload['checked']
      : undefined;
  const statement =
    typeof checked === 'object' && checked !== null && !Array.isArray(checked)
      ? text(checked['statement'])
      : '';
  return statement ? `${head}: ${statement} · ${time}` : `${head} · ${time}`;
}

/**
 * The configuration a review ran under (CR-35), and the classifier's label for the turn's
 * final message (CR-11), in words.
 */
export function identityText(review: Review): string {
  const classifier =
    typeof review.classifier === 'object' &&
    review.classifier !== null &&
    !Array.isArray(review.classifier)
      ? review.classifier
      : {};
  const config = `Configuration revision ${review.config_revision}.`;
  const label = text(classifier['label']);
  if (!label) return config;
  const reasons = [
    classifier['effects'] ? 'the turn had effects' : '',
    classifier['unresolved'] ? 'findings were unresolved' : '',
    classifier['completion_claim'] ? 'the agent claimed the work complete' : '',
  ].filter(Boolean);
  const why = reasons.length ? `, because ${reasons.join(' and ')}` : '';
  return `${config} The final message was classified ${label} by classifier ${text(classifier['version'])}${why}.`;
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
