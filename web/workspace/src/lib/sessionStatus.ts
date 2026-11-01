// SPDX-License-Identifier: MIT
/**
 * The one status the session header shows: whether anything is running and what the user
 * needs to do. Working counts the subagents (delegated tasks and independent checks) that
 * run now, and says nothing of them when none do.
 */
import type { RunTree, StatusPage } from './api-types';

/** What the session header needs from the chat panel's reads. */
export interface ChatSignals {
  service: boolean;
  /** The run that holds the session, by name. */
  lease: string | null;
  /** Questions the run holding the session waits on. */
  questions: number;
  held: boolean;
}

export type StatusTone = 'working' | 'waiting' | 'stopped' | 'idle' | 'offline' | 'closed';

export interface SessionStatus {
  tone: StatusTone;
  label: string;
  /** More words after the label, such as "2 active subagents"; null for none. */
  detail: string | null;
}

export interface StatusInput {
  closed: boolean;
  /** Whether the run service is running. */
  service: boolean;
  /** Whether a run holds the session now. */
  running: boolean;
  /** Questions the run holding the session waits on. */
  questions: number;
  /** Messages held until the user resumes. */
  held: boolean;
  recovery: Pick<StatusPage, 'unfinished' | 'active'> | null;
  subagents: number;
}

/** How many subagents run now: delegated runs the tree records as running. */
export function activeSubagents(tree: Pick<RunTree, 'runs'> | null): number {
  return (
    tree?.runs.filter((run) => run.role === 'delegated' && run.state === 'running').length ?? 0
  );
}

export function subagentText(count: number): string | null {
  return count > 0 ? `${count} active ${count === 1 ? 'subagent' : 'subagents'}` : null;
}

export function sessionStatus(input: StatusInput): SessionStatus {
  if (input.closed) return { tone: 'closed', label: 'Closed', detail: null };
  if (!input.service) return { tone: 'offline', label: 'Offline', detail: 'messages will wait' };
  if (input.questions > 0) return { tone: 'waiting', label: 'Needs your answer', detail: null };
  if (input.running || input.recovery?.active || input.subagents > 0)
    return { tone: 'working', label: 'Working', detail: subagentText(input.subagents) };
  if (input.held || (input.recovery?.unfinished ?? 0) > 0)
    return { tone: 'stopped', label: 'Stopped', detail: null };
  return { tone: 'idle', label: 'Idle', detail: null };
}
