// SPDX-License-Identifier: MIT
/** Passive observations only; these helpers never start, reconnect or restart a kernel. */
import type { KernelInfo, WorkspaceObservation } from './api-types';
import { get, inScope } from './http';
import type { PollOptions } from './poller';

export type { KernelInfo };
/** The worker's observation of a session's kernels, with its project. */
export type KernelObservation = WorkspaceObservation;

export interface KernelScope {
  projectId: string;
  sessionId: string;
  environmentId: string;
}

/** Only the exact loaded, open session mounts the panel. */
export function kernelPanelScope(
  project: string,
  session: string,
  detail: {
    project_id: string;
    session: { session_id: string; environment_id: string; closed_at: string | null };
  },
): KernelScope | null {
  return detail.project_id === project &&
    detail.session.session_id === session &&
    detail.session.closed_at === null
    ? { projectId: project, sessionId: session, environmentId: detail.session.environment_id }
    : null;
}

/**
 * The most bytes of a kernel observation to read. It follows the server's bound: an
 * observation holds up to 128 kernels, the session's own and those of its delegated runs,
 * one a language each (`MAX_SESSION_KERNELS`), and the workspace accepts 66,560 bytes of it
 * from the worker (`MAX_KERNEL_OBSERVATION_BYTES`). The workspace adds the project ID, under
 * 150 bytes; the rest is headroom. A workspace test keeps this at or above that bound.
 */
export const KERNEL_OBSERVATION_MAX_BYTES = 68 * 1024;

/**
 * The status of a lost race for the worker's kernel lane (kernels.py): it reads one
 * environment's kernels for one caller at a time, so the panel's poll can meet a run's check
 * before a turn. That passes by itself, so the panel waits for its next poll as it is,
 * without a warning or a backoff, rather than showing the kernel state as unknown.
 */
export const KERNELS_BUSY = 429;
export const KERNEL_POLL: PollOptions<KernelObservation> = { waitOn: { [KERNELS_BUSY]: null } };

export async function readKernels(
  scope: KernelScope,
  signal: AbortSignal,
): Promise<KernelObservation> {
  const path = `/projects/${encodeURIComponent(scope.projectId)}/sessions/${encodeURIComponent(scope.sessionId)}/kernels`;
  return inScope(
    await get<KernelObservation>(path, { signal, maxBytes: KERNEL_OBSERVATION_MAX_BYTES }),
    {
      project_id: scope.projectId,
      session_id: scope.sessionId,
      environment_id: scope.environmentId,
    },
  );
}
