// SPDX-License-Identifier: MIT
/**
 * Context in a review's snapshot (docs/specs/consistency-review.md, CR-14 as amended for
 * lab 40's B3): what the evidence of a ledger entry the snapshot holds pins, brought in
 * beside it: a document revision with the blocks its proofs cite, a delegation's outcome,
 * an entry at the revision pinned, or an artifact revision. It makes no review due and is
 * not reviewed material: the item names the entry revision that pins it (`pinned_by`).
 * The view says so, and leads to each target where the workspace has a page.
 */
import type { Delegation } from './api-types';
import type { LinkTarget } from './ledger';
import type { Json } from './api';
import type { Evidence } from './reviews';
import { documentHref, ENTRY_NAME, ledgerEntryHref } from './router';
import { visible } from './text';

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const count = (value: unknown): number | null =>
  Number.isSafeInteger(value) && (value as number) >= 1 ? (value as number) : null;
const named = (value: unknown): string | null =>
  typeof value === 'string' && value !== '' ? value : null;

/** The entry revision whose evidence pinned an item; null when the item is not context. */
export function pinnedBy(
  item: Pick<Evidence, 'locator'>,
): { name: string; revision: number | null } | null {
  const by = record(item.locator['pinned_by']);
  const name = named(by.handle) ?? named(by.entry);
  return name === null ? null : { name, revision: count(by.revision) };
}

/** What a context item is, and that it is not reviewed material; empty for another item. */
export function contextText(item: Pick<Evidence, 'locator'>): string {
  const by = pinnedBy(item);
  if (by === null) return '';
  const at = by.revision === null ? '' : ` r${by.revision}`;
  return `Context pinned by the evidence of ${visible(by.name)}${at}, not reviewed material.`;
}

/** Where a context item points, in words, for the kinds the snapshot adds as context. */
export function contextTarget(item: Pick<Evidence, 'kind' | 'locator'>): string {
  const locator = item.locator;
  const revision = count(locator['revision']);
  const at = revision === null ? '' : ` revision ${revision}`;
  switch (item.kind) {
    case 'document': {
      const blocks = Array.isArray(locator['blocks'])
        ? (locator['blocks'] as Json[]).filter((b): b is string => typeof b === 'string')
        : [];
      const which = blocks.length ? `, the blocks its proofs cite: ${blocks.join(', ')}` : '';
      return visible(`document ${String(locator['document'])}${at}${which}`);
    }
    case 'delegation':
      return visible(`the outcome of delegation ${String(locator['delegation'])}`);
    case 'entry':
      return visible(`ledger entry ${String(locator['handle'] ?? locator['entry'])}${at}`);
    default:
      return '';
  }
}

/**
 * The pages a context item leads to: each block it names in its document at the revision
 * pinned, the entry at the revision pinned, and the entry revision whose evidence pinned
 * it. A delegation's outcome has no page of its own here: its session's run tree shows it.
 */
export function contextLinks(
  project: string,
  item: Pick<Evidence, 'kind' | 'locator'>,
): LinkTarget[] {
  const locator = item.locator;
  const links: LinkTarget[] = [];
  const revision = count(locator['revision']);
  const document = named(locator['document']);
  if (item.kind === 'document' && document !== null && revision !== null) {
    const blocks = Array.isArray(locator['blocks']) ? locator['blocks'] : [];
    for (const block of blocks) {
      if (typeof block === 'string' && /^b[0-9]{1,9}$/.test(block)) {
        links.push({
          text: `block ${block}`,
          href: documentHref(project, document, revision, block),
        });
      }
    }
  }
  const entry = named(locator['handle']) ?? named(locator['entry']);
  if (item.kind === 'entry' && entry !== null && ENTRY_NAME.test(entry)) {
    links.push({
      text: `${entry}${revision === null ? '' : ` r${revision}`}`,
      href: ledgerEntryHref(project, entry, revision ?? undefined),
    });
  }
  const by = pinnedBy(item);
  if (by !== null && ENTRY_NAME.test(by.name)) {
    links.push({
      text: `pinned by ${by.name}${by.revision === null ? '' : ` r${by.revision}`}`,
      href: ledgerEntryHref(project, by.name, by.revision ?? undefined),
    });
  }
  return links;
}

/**
 * A delegation item's outcome, as the run tree's card shows a delegation, from the text
 * read whole; null when the text is not one. The item keeps no start, parent or files.
 */
export function outcomeOf(text: string): Delegation | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  const outcome = record(parsed);
  const id = named(outcome.delegation);
  if (id === null) return null;
  const ended = named(outcome.ended);
  return {
    delegation_id: id,
    parent: '',
    run: '',
    kind: (named(outcome.kind) ?? 'check') as Delegation['kind'],
    brief: record(outcome.brief) as Delegation['brief'],
    started_at: '',
    ended: ended as Delegation['ended'],
    stopped: named(outcome.stopped),
    report: typeof outcome.report === 'string' ? outcome.report : null,
    ended_at: null,
    refused: named(outcome.refused),
    acknowledged: null,
    result: outcome.result === null ? null : (record(outcome.result) as Delegation['result']),
    errors: [],
    alias: named(outcome.alias),
    models: Array.isArray(outcome.models)
      ? outcome.models.filter((m): m is string => typeof m === 'string')
      : [],
    read_entry: typeof outcome.read_entry === 'boolean' ? outcome.read_entry : null,
    read_proofs: typeof outcome.read_proofs === 'boolean' ? outcome.read_proofs : null,
    files: null,
  };
}
