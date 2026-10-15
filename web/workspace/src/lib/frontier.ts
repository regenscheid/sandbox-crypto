// SPDX-License-Identifier: MIT
/**
 * A frontier category and its best known results, as the frontier view shows them
 * (docs/specs/ledger.md, LG-18 to LG-23; docs/LEDGER.md, "Frontier categories" and "The
 * best known results"): the category's measures and condition keys, the main agent's
 * revision of it, each result's measures, marks and indicators, and the warning about
 * groups that look alike. Everything an agent wrote is shown as text.
 */
import type { CategoryMeasure, CategoryRevised, FrontierGroup, FrontierResult } from './api-types';
import { formatTime } from './format';
import { authorText } from './ledger';
import { EVIDENCE_TYPE_WORDS } from './ledgerEvidence';
import { codePoint, visible } from './text';

/** The most proposed results one read of a frontier takes as candidates. */
export const CANDIDATES_READ = 500;

/** A measure in words: its name, unit and direction. */
export function measureText(measure: CategoryMeasure): string {
  return `${measure.name}, in ${measure.unit}, ${measure.direction} is better`;
}

/**
 * A group's conditions in words, as the group spells them, each character a person may not
 * see shown as its code point, such as `⟦U+200B⟧`.
 */
export function conditionsText(conditions: Record<string, string>): string {
  const pairs = Object.entries(conditions);
  return pairs.length === 0
    ? 'No conditions'
    : pairs.map(([k, v]) => `${visible(k)}: ${visible(v)}`).join('; ');
}

/**
 * A result's measures in words: each of the category's, in its order, as a number in its
 * unit, as written when it is not a number, or missing; then any the category no longer has.
 */
export function measuresText(
  result: Pick<FrontierResult, 'measures'>,
  measures: readonly CategoryMeasure[],
): string[] {
  const named = new Set(measures.map((measure) => measure.name));
  return [
    ...measures.map((measure) => {
      const value = result.measures[measure.name];
      if (value === undefined) return `${measure.name}: missing`;
      return typeof value === 'number'
        ? `${measure.name}: ${value} ${measure.unit}`
        : `${measure.name}: ${value} (as written, not a number)`;
    }),
    ...Object.entries(result.measures)
      .filter(([name]) => !named.has(name))
      .map(([name, value]) => `${name}: ${value} (no longer a measure of the category)`),
  ];
}

/** Who wrote a revision, with an agent's role. */
export function writerText(author: Record<string, unknown>): string {
  const role = author.kind === 'agent' && typeof author.role === 'string' ? author.role : null;
  return role === null ? authorText(author) : `${authorText(author)}, role ${role}`;
}

/** The mark on an agent's published result that the user has not confirmed. */
export function unverifiedText(result: Pick<FrontierResult, 'revision' | 'author'>): string {
  return (
    `Unverified: published by ${writerText(result.author)}, and not confirmed by you as ` +
    `r${result.revision} reads, so it is a candidate.`
  );
}

/** The mark on a result that became disputed while it was eligible. */
export function disputedText(result: Pick<FrontierResult, 'revision'>): string {
  return `Disputed: shown as r${result.revision}, the revision before it was disputed.`;
}

/** A result's indicators in words: its evidence types, pinned artifacts, open findings and flags. */
export function indicatorsText(result: FrontierResult): string[] {
  const types = result.evidence_types.map(
    (type) => EVIDENCE_TYPE_WORDS[type as keyof typeof EVIDENCE_TYPE_WORDS] ?? type,
  );
  const pinned = result.artifacts.map((artifact) => {
    const { artifact_id, revision } = artifact;
    return `${String(artifact_id)} r${String(revision)}`;
  });
  const count = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;
  return [
    types.length ? `Evidence: ${types.join(', ')}` : 'No evidence',
    ...(pinned.length ? [`Artifacts: ${pinned.join(', ')}`] : []),
    `${count(result.open_findings, 'open finding')}, ${count(result.open_flags, 'open flag')}`,
  ];
}

/**
 * How many of a kind of result a group leaves out of the answer, in words; empty for none.
 * The answer lists a group's newest candidates, so those it leaves out are older.
 */
export function moreText(group: Pick<FrontierGroup, 'more'>, kind: string): string {
  const count = group.more[kind] ?? 0;
  if (count === 0) return '';
  return kind === 'candidates'
    ? `And ${count} older, not listed.`
    : `And ${count} more, not listed.`;
}

/**
 * Each group once, the first time it was read, by its key (the conditions as the frontier
 * folds them): a page read again after a change can repeat one.
 */
export function uniqueGroups(groups: readonly FrontierGroup[]): FrontierGroup[] {
  const seen = new Set<string>();
  return groups.filter((group) => {
    if (seen.has(group.key)) return false;
    seen.add(group.key);
    return true;
  });
}

/** Which proposed results the read took as candidates, in words; empty when it took them all. */
export function windowText(before: number | null, truncated: boolean): string {
  const newest = `the ${CANDIDATES_READ} newest`;
  if (before === null) {
    return truncated ? `Candidates: of the proposed results, ${newest} are read.` : '';
  }
  return (
    `Candidates: the proposed results made before R-${before}` +
    `${truncated ? `, ${newest} of them` : ''}, are read. Unverified published results are always read.`
  );
}

/**
 * A condition value as written, with the code point of each character outside printable
 * ASCII, so spellings that look alike can be told apart.
 */
export function spelling(value: string): string {
  const odd = [...new Set([...value].filter((c) => c < ' ' || c > '~'))];
  return odd.length === 0 ? value : `${value} (${odd.map(codePoint).join(', ')})`;
}

/** A set of conditions, each value spelled so that look-alike characters show. */
export function lookalikeText(conditions: Record<string, string>): string {
  return Object.entries(conditions)
    .map(([key, value]) => `${key}: ${spelling(value)}`)
    .join('; ');
}

/** The main agent's revision of the category, which the user has not revised since, in words. */
export function revisedText(revised: CategoryRevised): string {
  return (
    `The main agent revised this category in r${revised.revision} ` +
    `(${formatTime(revised.created)}, ${writerText(revised.author)}), and you have not ` +
    'revised it since: what it compares changed without you.'
  );
}

/** What the main agent's revision changed since the user's latest, in words. */
export function revisedSince(revised: CategoryRevised): string {
  if (revised.since === null) return 'You have written no revision of it.';
  return revised.changed.length
    ? `What changed since your r${revised.since}:`
    : `Nothing it compares changed since your r${revised.since}.`;
}
