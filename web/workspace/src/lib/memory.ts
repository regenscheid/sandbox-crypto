// SPDX-License-Identifier: MIT
/**
 * The Memory page's decisions (docs/specs/project-memory.md, PM-4): what the user is told
 * of each entry, proposal and version, and the changes the page sends. A change names the
 * revision the user saw, and keeps its operation ID across retries until it has a result
 * (`guardedWrite`), so a retry after a lost answer is not applied twice.
 */
import type { Author, Memory, MemoryChange, MemoryProposal, MemoryVersion } from './api-types';
import { guardedWrite } from './mutation';
import { submit, type Submitting } from './submit';
import { hiddenCharacters } from './text';

export type MemoryAction =
  | { action: 'add'; text: string }
  | { action: 'edit'; entry: string; expected: number; text: string }
  | { action: 'remove'; entry: string; expected: number }
  | { action: 'move'; entry: string; expected: number; direction: 'up' | 'down' }
  | { action: 'accept'; proposal: string; expected?: number }
  | { action: 'decline'; proposal: string };

/** An entry's text, in characters, as the server counts them (PM-1). */
export const MAX_TEXT = 1000;

/** Who proposed: the run, its role and session, and the model that answered or its alias. */
export function proposerText(author: Author): string {
  if (author.kind !== 'agent') return author.kind === 'user' ? 'the user' : author.kind;
  const model = author.model ?? author.alias;
  return (
    `the ${author.role ?? 'agent'} run ${author.run_id ?? '?'} of session ` +
    `${author.session_id ?? '?'}` +
    (model ? `, model ${model}` : '')
  );
}

export function proposalTitle(proposal: MemoryProposal): string {
  switch (proposal.action) {
    case 'add':
      return 'Add an entry';
    case 'change':
      return `Change ${proposal.entry_id}, as read at revision ${proposal.base}`;
    case 'remove':
      return `Remove ${proposal.entry_id}, as read at revision ${proposal.base}`;
  }
}

/**
 * Whether the user can accept a proposal as it stands, and what to read first: an entry
 * that changed since the proposal shows its current text beside it (PM-2); a removed one
 * leaves only declining.
 */
export function proposalState(proposal: MemoryProposal): {
  acceptable: boolean;
  note: string | null;
} {
  if (proposal.action === 'add') return { acceptable: true, note: null };
  const current = proposal.current;
  if (current === null) {
    return { acceptable: false, note: `${proposal.entry_id} was removed; decline the proposal.` };
  }
  if (current.revision !== proposal.base) {
    return {
      acceptable: true,
      note:
        `${proposal.entry_id} changed after the proposal: it is at revision ` +
        `${current.revision}. Read its current text before accepting.`,
    };
  }
  return { acceptable: true, note: null };
}

/** The decision sent: accepting a change or a removal names the revision shown beside it. */
export function decision(proposal: MemoryProposal, accept: boolean): MemoryAction {
  if (!accept) return { action: 'decline', proposal: proposal.proposal_id };
  return proposal.action === 'add' || proposal.current === null
    ? { action: 'accept', proposal: proposal.proposal_id }
    : { action: 'accept', proposal: proposal.proposal_id, expected: proposal.current.revision };
}

/** The flag for a memory longer than agents receive (PM-7); null when they get it all. */
export function overflowText(memory: Memory, bound: number): string | null {
  const left = memory.entries.length - memory.given;
  if (left <= 0) return null;
  return (
    `Agents receive the first ${memory.given} of ${memory.entries.length} entries: the ` +
    `entries hold ${memory.characters.toLocaleString('en')} characters, more than the ` +
    `${bound.toLocaleString('en')} given. Remove or merge entries.`
  );
}

const CHANGES: Record<MemoryVersion['change'], string> = {
  added: 'added',
  edited: 'edited',
  removed: 'removed',
  moved: 'moved',
};

/** One version of an entry: what changed, and who proposed and accepted it. */
export function versionText(version: MemoryVersion): string {
  const how =
    version.proposal !== null && version.proposer !== null
      ? `by the user, accepting ${version.proposal} from ${proposerText(version.proposer)}`
      : 'by the user';
  if (version.change === 'moved') {
    return `Moved, text unchanged (still revision ${version.revision}), ${how}`;
  }
  return `Revision ${version.revision}: ${CHANGES[version.change]} ${how}`;
}

/** A numeric character reference, which a rendering shows as its character. */
const REFERENCE = /&#(?:[xX]([0-9a-fA-F]{1,6})|([0-9]{1,7}));/g;

/**
 * Why the server would refuse a text, said before sending; null when it would take it.
 * Hidden characters are the shared set (text.ts); named references the server checks.
 */
export function textProblem(text: string): string | null {
  const kept = text.trim();
  if (kept.length === 0) return 'An entry has text.';
  if ([...kept].length > MAX_TEXT) return `An entry is at most ${MAX_TEXT} characters.`;
  const [first] = hiddenCharacters(text);
  if (first) {
    return (
      `The text holds ${first[1]} at character ${first[0] + 1}, a character you may not ` +
      'see; write it without one.'
    );
  }
  for (const match of text.matchAll(REFERENCE)) {
    const code = match[1] !== undefined ? parseInt(match[1], 16) : Number(match[2]);
    if (code > 0x10ffff) continue;
    const [hidden] = hiddenCharacters(String.fromCodePoint(code));
    if (hidden) return `The text writes ${hidden[1]} as ${match[0]}, which you may not see.`;
  }
  return null;
}

/**
 * Send one change, then read the memory again; after a failure, read it too, so that a
 * conflict shows the current revision beside the error.
 */
export function changeMemory(
  saving: Submitting,
  project: string,
  change: MemoryAction,
  send: (body: MemoryAction & { operation_id: string }) => Promise<MemoryChange>,
  reload: () => Promise<void>,
): Promise<boolean> {
  return submit(
    saving,
    async () => {
      await guardedWrite('memory', project, change, send);
      await reload();
    },
    reload,
  );
}
