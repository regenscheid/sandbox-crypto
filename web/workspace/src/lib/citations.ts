// SPDX-License-Identifier: MIT
/**
 * The consistency reviewer's checks of a quote (docs/specs/consistency-review.md, CR-4;
 * docs/LEDGER.md, "The best known results"), as the entry view shows them under the quote:
 * each check's verdict, the review that made it, the revision whose material it covered,
 * when, what it read and why. The explanation is the reviewer's text, shown as text.
 */
import type { CitationCheckView, EntryCitations, QuoteChecks } from './api-types';
import { formatTime } from './format';
import { visible } from './text';

/** A verdict in words. */
export const VERDICT_WORDS: Record<CitationCheckView['verdict'], string> = {
  supported: 'Supported',
  unsupported: 'Unsupported',
  inconclusive: 'Inconclusive',
};

/** A quote's checks, by its evidence ID; null when the read has none for it. */
export function quoteChecks(
  citations: EntryCitations | null,
  evidence: string,
): QuoteChecks | null {
  return citations?.quotes.find((quote) => quote.evidence_id === evidence) ?? null;
}

/**
 * What a check covered and when, in words: the revision whose material it checked (said
 * plainly when that is not the revision shown's material), the time, and what it read.
 */
export function checkText(check: CitationCheckView): string {
  const covered = check.matches
    ? `checked r${check.entry_revision}, the material shown`
    : `checked r${check.entry_revision}, whose material is not the revision shown's`;
  return `${covered}, ${formatTime(check.created)}; read ${visible(check.source)}`;
}

/** How many older checks of a quote the read leaves out, in words; empty for none. */
export function olderChecksText(quote: Pick<QuoteChecks, 'checks' | 'total'>): string {
  const more = quote.total - quote.checks.length;
  if (more <= 0) return '';
  return `And ${more} older ${more === 1 ? 'check' : 'checks'}, not listed.`;
}
