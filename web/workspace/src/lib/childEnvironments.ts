// SPDX-License-Identifier: MIT
/**
 * An open session's child environments (docs/specs/child-environments.md, CE-8, CE-11):
 * the environments its delegated runs work in when the project gives them their own. Read
 * only; the orchestrator releases them. The words here say what holds each one, what its
 * hand-back brought back and how it is kept or released.
 */
import type { ChildEnvironmentListing, ChildEnvironmentView } from './api-types';
import { duration, formatTime } from './format';
import { get, inScope } from './http';
import { agentName } from './runTree';
import { visible } from './text';

export type { ChildEnvironmentListing, ChildEnvironmentView };

/** A listing holds up to 1,000 environments, each with its delegation's brief. */
export const CHILD_ENVIRONMENTS_MAX_BYTES = 16 * 1024 * 1024;

const path = (project: string, session: string) =>
  `/projects/${encodeURIComponent(project)}/sessions/${encodeURIComponent(session)}/child-environments`;

export async function readChildEnvironments(
  project: string,
  session: string,
  signal: AbortSignal,
): Promise<ChildEnvironmentListing> {
  return inScope(
    await get<ChildEnvironmentListing>(path(project, session), {
      signal,
      maxBytes: CHILD_ENVIRONMENTS_MAX_BYTES,
    }),
    { project_id: project, session_id: session },
  );
}

/** The kernel observation path of one of the session's child environments. */
export function childKernelsPath(project: string, session: string, environment: string): string {
  return `${path(project, session)}/${encodeURIComponent(environment)}/kernels`;
}

/** Whether its kernels can be observed: it is ready, its run's to compute in. */
export const observable = (view: Pick<ChildEnvironmentView, 'phase'>) => view.phase === 'ready';

/** The delegation's brief title, as the run tree names a subagent; agent text, shown visibly. */
export function childTitle(view: Pick<ChildEnvironmentView, 'delegation'>): string {
  return view.delegation
    ? agentName({ role: 'delegated' }, view.delegation)
    : 'A delegation not yet recorded';
}

const PHASES: Record<ChildEnvironmentView['phase'], string> = {
  creating: 'being created',
  preparing: 'being prepared (packages and inputs)',
  ready: 'ready',
  collecting: 'handing back',
  released: 'released',
  retained: 'stopped and retained',
};

/** Its state in words, with why it failed when it did. */
export function phaseText(view: Pick<ChildEnvironmentView, 'phase' | 'failure'>): string {
  const failed = view.failure ? `, failed: ${view.failure.replaceAll('_', ' ')}` : '';
  return `${PHASES[view.phase]}${failed}`;
}

const USES: Record<ChildEnvironmentView['use'], string> = {
  preparing: 'Its delegation waits for it.',
  running: 'Its delegated run works in it.',
  awaiting_review:
    'Its task returned and awaits the parent’s review: it is kept until the parent accepts or cancels the task.',
  idle: 'Its task has settled; nothing uses it.',
  unclaimed: 'No delegation names it yet.',
  ended: '',
};

/** What holds it, and when it goes: retained until a time, released, or the idle sweep's time. */
export function holdText(
  view: Pick<ChildEnvironmentView, 'use' | 'phase' | 'retained_until' | 'idle_release_after'>,
): string {
  if (view.phase === 'released') return 'Released.';
  if (view.phase === 'retained')
    return view.retained_until
      ? `Retained until ${formatTime(view.retained_until)}, then released.`
      : 'Retained.';
  const after = view.idle_release_after
    ? ` Released by the next main run's idle sweep from ${formatTime(view.idle_release_after)}.`
    : '';
  return `${USES[view.use]}${after}`;
}

/** The hand-back's account, or that none was made yet. */
export function handBackText(view: Pick<ChildEnvironmentView, 'hand_back'>): string {
  const back = view.hand_back;
  if (!back) return 'No hand-back yet.';
  const parts = [`${back.copied} copied into ${visible(back.target)}/`];
  if (back.retained) parts.push(`${back.retained} named by their retained revision`);
  if (back.left_count) parts.push(`${back.left_count} left behind`);
  return `Hand-back ${back.complete ? 'complete' : 'incomplete'}: ${parts.join(', ')}.`;
}

/** How long ago it was created, from `now` (ms). */
export function age(view: Pick<ChildEnvironmentView, 'created_at'>, now: number): string {
  return duration(view.created_at, new Date(now).toISOString());
}
