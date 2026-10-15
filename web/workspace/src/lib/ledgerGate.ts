// SPDX-License-Identifier: MIT
/**
 * The gate for a project claim or result, and the decisions that affect an entry, as the
 * entry view shows them (docs/specs/ledger.md, LG-4, LG-17; docs/LEDGER.md, "The gate"):
 * each clause as the main agent's decision to establish the entry as it reads now would
 * meet it, whether it holds and why, read without deciding anything. The user's own
 * decision needs no gate.
 */
import type { GateClause, GateDecision, GateView } from './api-types';

/** What each clause asks, in words, in the gate's order. */
export const CLAUSE_WORDS: Record<GateClause['clause'], string> = {
  evidence: 'Evidence that counts',
  independent_check: 'An independent check, when the evidence is a proof',
  computation: 'Computational evidence pinned to an artifact',
  review: 'A completed review that held the entry whole',
  finding: 'No unresolved fail finding',
  flags: 'No open flag',
};

/** The gate in one sentence: whether it applies, and how much of it holds. */
export function gateSummary(
  gate: Pick<GateView, 'applies' | 'holds' | 'clauses' | 'revision' | 'status'>,
): string {
  if (!gate.applies) {
    return 'The gate applies only to a claim or result of this project: this entry needs none.';
  }
  if (gate.status === 'established') {
    return `It is established. The gate below is read on r${gate.revision} as it is now.`;
  }
  if (gate.holds) {
    return `Every clause holds: the main agent's decision could establish r${gate.revision}.`;
  }
  const held = gate.clauses.filter((clause) => clause.holds).length;
  return `${held} of ${gate.clauses.length} clauses hold: the main agent cannot establish r${gate.revision} yet. Your own decision needs no gate.`;
}

/** Decisions in effect first, then the others, each group newest first as read. */
export function decisionGroups(decisions: readonly GateDecision[]): {
  inEffect: GateDecision[];
  others: GateDecision[];
} {
  return {
    inEffect: decisions.filter((decision) => decision.status === 'in_effect'),
    others: decisions.filter((decision) => decision.status !== 'in_effect'),
  };
}

/** The read lists this many of the decisions that affect an entry, newest first. */
export const DECISIONS_LISTED = 50;
