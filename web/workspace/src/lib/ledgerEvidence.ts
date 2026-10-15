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
import { documentHref, ENTRY_NAME, findingHref, ledgerEntryHref, pdfHref } from './router';
import { visible } from './text';

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
    case 'delegation':
      return { text: `delegation ${link.target_id}${checkedBy(link.locator)}`, href: null };
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

/**
 * What a delegation's check returned, as its link records it: its verdict and status, and
 * the note its parent gave the checker, which is the requester's note, not evidence (LG-17).
 */
export function checkedBy(locator: Record<string, unknown> | null): string {
  if (locator === null) return '';
  const said = [locator.verdict, locator.status].filter((v) => typeof v === 'string');
  const outcome = said.length > 0 ? ` (${said.join(', ')})` : ` (${String(locator.ended)})`;
  const note =
    typeof locator.note === 'string'
      ? `; the requester's note, not evidence: “${locator.note}”`
      : '';
  return `${outcome}${note}`;
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

/** A quote evidence link as the entry view shows it (LG-8, CR-4). */
export interface QuoteView {
  /** The reference quoted, at the revision the quote pins, leading to it. */
  reference: LinkTarget;
  /**
   * Where the quote is, each character a person may not see as its code point: a PDF's
   * page, printed label and lines, with the start of its SHA-256, or an ePrint and the
   * date stamp the e-print tools gave it.
   */
  where: string;
  /** The PDF viewer at the quote's page, when the link names a PDF revision and its hash. */
  pdf: string | null;
  /** The page `pdf` opens at. */
  page: number | null;
  /** The quoted text exactly, each character a person may not see as its code point. */
  text: string;
}

const count = (value: unknown): number | null =>
  Number.isSafeInteger(value) && (value as number) >= 1 ? (value as number) : null;

/** A quote evidence link's quote; null for another link. */
export function quoteView(project: string, link: EvidenceLink): QuoteView | null {
  if (link.target !== 'quote') return null;
  const locator = link.locator ?? {};
  const name = link.handle ?? link.target_id;
  const pinned = link.revision === null ? '' : ` r${link.revision}`;
  const reference: LinkTarget = {
    text: `${visible(name)}${pinned}`,
    href: ENTRY_NAME.test(name) ? ledgerEntryHref(project, name, link.revision ?? undefined) : null,
  };
  const text = visible(typeof locator.text === 'string' ? locator.text : '');
  const named = (
    typeof locator.named === 'object' && locator.named !== null ? locator.named : {}
  ) as Record<string, unknown>;
  if (locator.source === 'pdf') {
    const page = count(locator.page);
    const label = typeof locator.label === 'string' ? ` (printed ${visible(locator.label)})` : '';
    const lines = `lines ${String(count(locator.first_line) ?? '?')}–${String(count(locator.last_line) ?? '?')}`;
    const digest = link.sha256 === null ? '' : `, sha256 ${link.sha256.slice(0, 12)}…`;
    const artifact = typeof named.artifact_id === 'string' ? named.artifact_id : '';
    const revision = count(named.revision);
    const pdf =
      artifact !== '' &&
      revision !== null &&
      page !== null &&
      link.sha256 !== null &&
      /^[0-9a-f]{64}$/.test(link.sha256)
        ? pdfHref(project, artifact, revision, link.sha256, page)
        : null;
    return {
      reference,
      where: `PDF page ${page ?? '?'}${label}, ${lines}${digest}`,
      pdf,
      page,
      text,
    };
  }
  const eprint = typeof named.eprint === 'string' ? visible(named.eprint) : '?';
  const dated = typeof locator.dated === 'string' ? visible(locator.dated) : '?';
  return { reference, where: `ePrint ${eprint}, dated ${dated}`, pdf: null, page: null, text };
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
