// SPDX-License-Identifier: MIT
/**
 * The Environments panel's decisions, apart from its view (EnvironmentPanel.svelte): what
 * it says for each observation phase and held input, and how it pages back and forth
 * through a lockfile comparison or a revision's packages. Its reads stay on `Poller`
 * (poller.ts): they are polled observations, not lists the user extends.
 */
import type { PollPhase } from './poller';

const PHASE_LABELS: Readonly<Record<PollPhase, string>> = {
  loading: 'Reading environment records…',
  observed: '',
  paused: 'Observations paused.',
  unavailable: 'Observation unavailable. Current state is unknown.',
  unsupported: 'This worker is unconfigured or does not support provisioning observations.',
  missing: 'This recorded environment or selected record is no longer available.',
  changed: 'This selection changed. Refresh the overview or history and select it again.',
  signed_out: 'Observations stopped for sign-out. Reload after signing in.',
};

/** What the panel says for an observation's phase; nothing while observed. */
export function phaseLabel(phase: PollPhase): string {
  return Object.hasOwn(PHASE_LABELS, phase) ? PHASE_LABELS[phase] : '';
}

const HOLD_REASONS: Readonly<Record<string, string>> = {
  lockfile_unrecorded: 'No lockfile was recorded',
  blob_missing: 'The recorded lockfile is unavailable or evicted',
  unsupported_format: 'The retained format is unsupported',
  skipped_entries: 'The retained lockfile omitted package entries',
  input_limit: 'The selected input exceeds this reader’s limits',
};

/** Why a comparison or a package listing held an input back; only its own reasons count. */
export function holdReason(reason: string): string {
  return Object.hasOwn(HOLD_REASONS, reason) ? HOLD_REASONS[reason]! : 'Unknown input';
}

/**
 * A step through a reading shown one page at a time: the offset to show, and the trail of
 * offsets already shown, so that Previous returns exactly to the page before (pages may
 * differ in size).
 */
export interface PageStep {
  offset: number;
  trail: number[];
}

/** The next page after the one shown; null at the end. */
export function pageForward(
  trail: readonly number[],
  shown: { offset: number; next_offset: number | null },
): PageStep | null {
  if (shown.next_offset === null) return null;
  return { offset: shown.next_offset, trail: [...trail, shown.offset] };
}

/** The page shown before this one; null on the first. */
export function pageBack(trail: readonly number[]): PageStep | null {
  if (trail.length === 0) return null;
  return { offset: trail.at(-1)!, trail: trail.slice(0, -1) };
}
