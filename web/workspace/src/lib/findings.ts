// SPDX-License-Identifier: MIT
/**
 * A consistency finding's decisions, apart from its view (FindingCard.svelte): the user's
 * response and the draft it starts from, and where the finding's history reads back from.
 */
import type { FindingDraft } from './drafts';
import type { Page } from './pagedList';
import { parseRevisionReferences, type Finding, type Transition } from './reviews';

/** A response's draft: the stored one, or an empty rebuttal on the finding's revision. */
export function startResponse(finding: Finding, stored: FindingDraft | null): FindingDraft {
  return stored ?? { expected: finding.revision, explanation: '', action: 'rebut', references: '' };
}

/** The response a draft sends; it throws when a revision reference is not one. */
export function findingResponse(finding: Finding, draft: FindingDraft): Record<string, unknown> {
  const { expected, action, explanation, references } = draft;
  return {
    finding: finding.finding_id,
    expected,
    action,
    explanation,
    revisions: parseRevisionReferences(references),
  };
}

/** The draft after a response was saved: empty, on the revision it made, same decision. */
export function afterResponse(draft: FindingDraft, saved: { revision: number }): FindingDraft {
  return { ...draft, explanation: '', references: '', expected: saved.revision };
}

/**
 * The page of a finding's history before `before`, for `Earlier` (earlier.ts); null reads
 * the newest page.
 */
export function findingHistory(
  read: (before: number | null) => Promise<Finding>,
): (before: number | null) => Promise<Page<Transition[], number>> {
  return async (before) => {
    const page = await read(before);
    return { items: [page.history ?? []], next: page.history_before ?? null };
  };
}
