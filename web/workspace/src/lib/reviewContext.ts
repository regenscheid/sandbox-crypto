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
import { actionsText } from './reviewActions';
import {
  documentHref,
  ENTRY_NAME,
  findingHref,
  ledgerEntryHref,
  MESSAGE_ID,
  messageHref,
  runHref,
  sessionHref,
  treeHref,
} from './router';
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

/** How the review list describes an item it labels: its mark, what it is, and its links. */
export interface ItemView {
  /** Where the item comes from, and whether it is reviewed material. */
  mark: string;
  text: string;
  links: LinkTarget[];
}

/**
 * The archived message an item's locator names by its place (CR-14 as amended by the
 * context management spec), as a link to it read whole; null when it names none.
 */
export function archivedLink(project: string, locator: Evidence['locator']): LinkTarget | null {
  const place = record(locator['archive']);
  const message = named(place.id);
  if (message === null || !MESSAGE_ID.test(message)) return null;
  const position = count(place.position);
  return {
    text: `the archived message${position === null ? '' : `, position ${position}`}`,
    href: messageHref(project, message),
  };
}

/** An artifact item's path, or else its artifact and revision. */
function artifactText(locator: Evidence['locator']): string {
  const path = named(locator['path']);
  if (path !== null) return visible(`file ${path}`);
  const pin = record(locator['artifact']);
  const revision = count(pin.revision);
  return visible(
    `artifact ${String(pin.artifact_id ?? '?')}${revision === null ? '' : ` r${revision}`}`,
  );
}

/**
 * How the review list describes an item whose provenance it knows (CR-2, CR-14 as amended
 * for labs 40 and 41): context an entry's evidence pins; a window's actions; a file a
 * claimed fix cites; a delegation's result file; a delegation's outcome brought in with
 * its run's work; null for any other item, which the list names by its locator.
 */
export function describeItem(
  project: string,
  session: string,
  item: Pick<Evidence, 'kind' | 'locator'>,
): ItemView | null {
  const locator = item.locator;
  const context = contextText(item);
  if (context) {
    const target = contextTarget(item) || (item.kind === 'artifact' ? artifactText(locator) : '');
    return { mark: context, text: target, links: contextLinks(project, item) };
  }
  if (item.kind === 'actions') {
    const earlier = Object.keys(record(locator['earlier'])).length
      ? '; with, apart, the run’s earlier windows, as background'
      : '';
    return {
      mark: "Derived from the window's tool records:",
      text: actionsText(item) + earlier,
      links: [],
    };
  }
  if (item.kind === 'plan') {
    const version = count(locator['version']);
    return {
      mark: 'The session plan when the review was dispatched, as context, not reviewed material:',
      text: visible(
        `plan ${String(locator['plan'] ?? '?')}${version === null ? '' : ` version ${version}`}`,
      ),
      links: session ? [{ text: 'the session', href: sessionHref(project, session) }] : [],
    };
  }
  const archived = archivedLink(project, locator);
  if (item.kind === 'transcript' && archived !== null) {
    return {
      mark: "A message of the reviewed run, as the session's archive keeps it:",
      text: '',
      links: [archived],
    };
  }
  const ran = named(locator['delegation']);
  if ((item.kind === 'execution' || item.kind === 'action') && ran !== null) {
    const what =
      item.kind === 'execution'
        ? `execution ${String(locator['execution'] ?? '?')}`
        : `its call ${String(locator['message'] ?? '?')}`;
    return {
      mark:
        item.kind === 'execution'
          ? `An execution of delegation ${visible(ran)}'s run, chosen for its outcome, as context:`
          : `What delegation ${visible(ran)}'s run did, as context:`,
      text: visible(what),
      links: [
        ...(session ? [{ text: 'the run tree', href: treeHref(project, session) }] : []),
        ...(archived === null ? [] : [{ ...archived, text: 'the tool result it came back as' }]),
      ],
    };
  }
  const output = record(locator['output_of']);
  if (item.kind === 'artifact' && Object.keys(output).length) {
    const by = named(record(locator['delegated']).delegation) ?? '?';
    return {
      mark: `The saved output of an execution of delegation ${visible(by)}'s run, cut short in it:`,
      text: visible(
        `${String(output.stream ?? 'output')} of execution ${String(output.execution)}`,
      ),
      links: session ? [{ text: 'the run tree', href: treeHref(project, session) }] : [],
    };
  }
  const cited = record(locator['cited_by']);
  if (item.kind === 'artifact' && Object.keys(cited).length) {
    const finding = named(cited.finding);
    return {
      mark: 'Cited by the fix:',
      text: artifactText(locator),
      links:
        finding !== null && session
          ? [{ text: 'the finding', href: findingHref(project, session, finding) }]
          : [],
    };
  }
  const delegated = record(locator['delegated']);
  const delegation = named(delegated.delegation);
  if (item.kind === 'artifact' && delegation !== null) {
    return {
      mark: `Result file of delegation ${visible(delegation)}:`,
      text: artifactText(locator),
      links: session ? [{ text: 'the run tree', href: treeHref(project, session) }] : [],
    };
  }
  const outcome = named(locator['delegation']);
  if (item.kind === 'delegation' && outcome !== null) {
    const name = named(locator['name']);
    const links: LinkTarget[] = [];
    if (session && name !== null) {
      links.push({ text: 'its run’s events', href: runHref(project, session, name) });
    }
    if (session) links.push({ text: 'the run tree', href: treeHref(project, session) });
    return {
      mark: 'Delegated work, as context, not reviewed material:',
      text: visible(`the outcome of delegation ${outcome}`),
      links,
    };
  }
  return null;
}

/**
 * How many of a delegated run's executions a review holds, how many it left out and how
 * they were chosen (CR-2 as amended for lab 42), from a delegation item's text read whole;
 * empty when the text says nothing of them.
 */
export function heldText(text: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return '';
  }
  const held = record(record(parsed).executions);
  const kept = Number.isSafeInteger(held.held) ? (held.held as number) : null;
  if (kept === null) return '';
  const left = Number.isSafeInteger(held.left_out) ? (held.left_out as number) : 0;
  const how = named(held.chosen);
  return (
    `This review holds ${kept} of its run's executions${left ? ` and left ${left} out` : ''}` +
    `${how ? `, chosen: ${visible(how)}` : ''}.`
  );
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
