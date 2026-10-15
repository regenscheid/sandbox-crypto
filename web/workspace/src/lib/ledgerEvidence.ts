// SPDX-License-Identifier: MIT
/**
 * An entry's evidence and the review findings anchored to it, as the entry view shows
 * them (docs/specs/ledger.md, LG-8 to LG-11, LG-25; docs/LEDGER.md, "Evidence" and
 * "Reviews"): each link's role, type and scope, and its pinned target with where it leads
 * in the workspace. The target is the platform's record; the role, type and scope are what
 * the author asserts, and are shown as text.
 */
import type { EntryFinding, EvidenceLink } from './api-types';
import type { LinkTarget } from './ledger';
import { statusText } from './ledger';
import { documentHref, ENTRY_NAME, findingHref, ledgerEntryHref } from './router';

/** An evidence link's role, in words. */
export const ROLE_WORDS: Record<EvidenceLink['role'], string> = {
  supports: 'Supports',
  contradicts: 'Contradicts',
  derivation: 'Derivation',
  independent_check: 'Independent check',
};

/** An evidence link's type, in words (LG-9). */
export const EVIDENCE_TYPE_WORDS: Record<EvidenceLink['type'], string> = {
  proof: 'proof',
  exact_computation: 'exact computation',
  finite_test: 'finite test',
  numerical_experiment: 'numerical experiment',
  source_claim: 'source claim',
  synthetic: 'synthetic',
  unclassified: 'unclassified',
};

/** How many evidence links the entry view shows before the reader asks for the rest. */
export const EVIDENCE_SHOWN = 50;

/**
 * Where an evidence link's pinned target leads: an entry at the revision it pins, or a
 * document block at its revision. An artifact revision (named with the start of its
 * SHA-256) and a review are named: the workspace has no page for either by ID alone.
 */
export function evidenceTarget(project: string, link: EvidenceLink): LinkTarget {
  const pinned = link.revision === null ? '' : ` r${link.revision}`;
  switch (link.target) {
    case 'entry': {
      const name = link.handle ?? link.target_id;
      if (!ENTRY_NAME.test(name)) return { text: name, href: null };
      return {
        text: `${name}${pinned}`,
        href: ledgerEntryHref(project, name, link.revision ?? undefined),
      };
    }
    case 'artifact': {
      const digest = link.sha256 === null ? '' : ` (sha256 ${link.sha256.slice(0, 12)}…)`;
      return { text: `artifact ${link.target_id}${pinned}${digest}`, href: null };
    }
    case 'block': {
      const span = link.span === null ? '' : `, math span ${link.span}`;
      return {
        text: `${link.target_id}${pinned}, block ${link.block ?? '?'}${span}`,
        href:
          link.revision === null || link.block === null
            ? null
            : documentHref(project, link.target_id, link.revision, link.block),
      };
    }
    case 'review':
      return { text: `review ${link.target_id}`, href: null };
    case 'quote': {
      const name = link.handle ?? link.target_id;
      const where = quoteWhere(link.locator);
      return {
        text: `quote of ${name}${pinned}${where}`,
        href: ENTRY_NAME.test(name)
          ? ledgerEntryHref(project, name, link.revision ?? undefined)
          : null,
      };
    }
  }
}

/** Where a quote is in its source: a PDF's page and lines, or an ePrint and its date stamp. */
export function quoteWhere(locator: Record<string, unknown> | null): string {
  if (locator === null) return '';
  if (locator.source === 'pdf') {
    const label = typeof locator.label === 'string' ? ` (printed ${locator.label})` : '';
    return `, PDF page ${String(locator.page)}${label}, lines ${String(locator.first_line)}–${String(locator.last_line)}`;
  }
  const named = (locator.named ?? {}) as Record<string, unknown>;
  return `, ePrint ${String(named.eprint ?? '?')} dated ${String(locator.dated)}`;
}

/** What an evidence link is, in words: its role and type, and the revision a check examined. */
export function evidenceKind(link: EvidenceLink): string {
  const checked = link.checked === null ? '' : `, of revision ${link.checked}`;
  return `${ROLE_WORDS[link.role]}, ${EVIDENCE_TYPE_WORDS[link.type]}${checked}`;
}

/**
 * What the entry view says of an entry's evidence: none at all for a type that holds
 * none; "asserted only" for a claim or result without any (LG-11).
 */
export function evidenceState(entry: {
  asserted_only: boolean | null;
  evidence: readonly unknown[];
}): 'none' | 'asserted' | 'held' {
  if (entry.evidence.length > 0) return 'held';
  return entry.asserted_only ? 'asserted' : 'none';
}

/** A review finding anchored to the entry: what it is, and where it is, in words. */
export function findingText(finding: EntryFinding): string {
  const field = finding.field === null ? '' : `, on its ${statusText(finding.field)}`;
  // A math finding's revision is its statement's text revision, not the entry's.
  const revision =
    finding.revision === null
      ? ''
      : finding.category === 'math'
        ? ` at the statement's text revision ${finding.revision}`
        : ` at r${finding.revision}`;
  return `${finding.severity} · ${statusText(finding.category)} · ${statusText(finding.status)}${field}${revision}`;
}

/** A finding anchored to the entry, in its session. */
export function findingLink(project: string, finding: EntryFinding): string {
  return findingHref(project, finding.session, finding.finding_id);
}
