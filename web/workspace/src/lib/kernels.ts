// SPDX-License-Identifier: MIT
/** Passive observations only; these helpers never start, reconnect or restart a kernel. */
import { get, inScope } from './http';

export interface KernelScope {
  projectId: string;
  sessionId: string;
  environmentId: string;
}
export interface KernelInfo {
  kernel_id: string;
  language: 'python' | 'sage';
  generation: number;
  start_revision: number | null;
  current_revision: number | null;
  revision_status: 'current' | 'predates' | 'unknown';
}
export interface KernelObservation {
  project_id: string;
  environment_id: string;
  session_id: string;
  current_revision: number | null;
  kernels: KernelInfo[];
  observed_at: string;
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

export async function readKernels(
  scope: KernelScope,
  signal: AbortSignal,
): Promise<KernelObservation> {
  const path = `/projects/${encodeURIComponent(scope.projectId)}/sessions/${encodeURIComponent(scope.sessionId)}/kernels`;
  return inScope(await get<KernelObservation>(path, { signal, maxBytes: 12 * 1024 }), {
    project_id: scope.projectId,
    session_id: scope.sessionId,
    environment_id: scope.environmentId,
  });
}
