// SPDX-License-Identifier: MIT
/** Read-only CP-26 facts. No provisioning or permission action is exposed here. */
import type { Project } from './api';
import type {
  CondaPackage,
  LicenseObservation,
  LockfileDiffPage,
  LockfileDifference,
  LockfileHeader,
  LockfileRef,
  LockfileSelection,
  PipDistribution,
  ProbeResult,
  ProvisionDetail,
  ProvisionFilePage,
  ProvisionObservation,
  ProvisionState,
  ProvisionSummary,
  ResolvedFile,
  RevisionPackage,
  RevisionPackagePage,
  RevisionProbePage,
  RevisionSummary,
  RevisionSummaryPage,
} from './api-types';
import { get, inScope } from './http';
import type { PollOptions } from './poller';

export interface EnvironmentScope {
  projectId: string;
  environmentId: string;
}
export interface EnvironmentChoice {
  scope: EnvironmentScope;
  shared: boolean;
  sessions: string[];
  /**
   * Whether a session using it has a run. The worker makes an environment when a session
   * first runs, so until then the project record names an environment that does not exist
   * yet, and the worker's "not found" does not mean it is gone.
   */
  ran: boolean;
}
export type {
  LockfileDiffPage,
  LockfileDifference,
  LockfileHeader,
  LockfileRef,
  ProvisionObservation,
  ResolvedFile,
  RevisionPackage,
  RevisionPackagePage,
  RevisionSummary,
};
export type CondaEntry = CondaPackage;
export type DeclaredLicense = LicenseObservation;
export type LockfilePin = LockfileSelection;
export type PipEntry = PipDistribution;
export type Probe = ProbeResult;
export type OperationDetail = ProvisionDetail;
export type FilePage = ProvisionFilePage;
export type OperationState = ProvisionState;
export type OperationSummary = ProvisionSummary;
export type ProbePage = RevisionProbePage;
export type HistoryPage = RevisionSummaryPage;
export type Selection =
  | { kind: 'summary' }
  | { kind: 'operation'; operation: OperationSummary }
  | { kind: 'retained_operation'; provisionId: string }
  | { kind: 'files'; operation: OperationSummary; offset: number; retained?: true }
  | { kind: 'history'; after: number; target: number | null }
  | { kind: 'probes'; revision: RevisionSummary; offset: number }
  | { kind: 'packages'; revision: RevisionSummary; offset: number }
  | { kind: 'diff'; before: RevisionSummary; after: RevisionSummary; offset: number };
export type ProvisionData =
  | ProvisionObservation
  | OperationDetail
  | FilePage
  | HistoryPage
  | ProbePage
  | LockfileDiffPage
  | RevisionPackagePage;
export const PROVISION_MAX_BYTES = 64 * 1024;
export const PROVISION_DEADLINE_MS = 8000;
export const DIFF_DEADLINE_MS = 12000;
export const PACKAGES_DEADLINE_MS = 12000;

/** The project's environments, as its record lists them: the shared one first. */
export function environmentChoices(projectId: string, project: Project): EnvironmentChoice[] {
  if (project.project_id !== projectId) return [];
  return project.environments.map((e) => ({
    scope: { projectId, environmentId: e.environment_id },
    shared: e.default,
    sessions: [...e.sessions],
    ran: project.sessions.some((s) => s.runs > 0 && e.sessions.includes(s.session_id)),
  }));
}

/**
 * How the overview of an environment is polled. One no session has run in is not created
 * yet, and a "not found" for it only means wait: the panel says so and keeps asking, and
 * shows the environment once a first run makes it. For any other, "not found" ends the
 * poll, as it does for a record that is gone.
 */
export function overviewPoll(choice: EnvironmentChoice): PollOptions<ProvisionData> {
  return choice.ran ? {} : { waitOn: { 404: 'missing' } };
}

export function differenceKey(v: LockfileDifference): string {
  return [v.ecosystem, v.location ?? '', v.name].join('\0');
}
export function packageKey(v: RevisionPackage): string {
  return [v.ecosystem, v.location ?? '', v.name].join('\0');
}
export function provisionPath(scope: EnvironmentScope, selected: Selection): string {
  const base = `/projects/${encodeURIComponent(scope.projectId)}/environments/${encodeURIComponent(scope.environmentId)}/provisioning/observations`;
  const query = new URLSearchParams();
  if (selected.kind === 'summary') return base;
  if (selected.kind === 'diff') {
    query.set('before', String(selected.before.revision));
    query.set('before_pin', selected.before.record_sha256);
    query.set('after', String(selected.after.revision));
    query.set('after_pin', selected.after.record_sha256);
    query.set('offset', String(selected.offset));
    query.set('limit', '8');
    return base + '/lockfile-diff?' + query;
  }
  if (selected.kind === 'history') {
    query.set('after', String(selected.after));
    query.set('limit', '8');
    if (selected.target !== null) query.set('target', String(selected.target));
    return base + '/revisions?' + query;
  }
  if (selected.kind === 'retained_operation') {
    return base + `/operations/${encodeURIComponent(selected.provisionId)}`;
  }
  if (selected.kind === 'probes') {
    query.set('pin', selected.revision.record_sha256);
    query.set('offset', String(selected.offset));
    query.set('limit', '8');
    return base + `/revisions/${selected.revision.revision}/probes?` + query;
  }
  if (selected.kind === 'packages') {
    query.set('pin', selected.revision.record_sha256);
    query.set('offset', String(selected.offset));
    query.set('limit', '8');
    return base + `/revisions/${selected.revision.revision}/packages?` + query;
  }
  query.set('seq', String(selected.operation.seq));
  if (selected.kind === 'files') {
    query.set('offset', String(selected.offset));
    query.set('limit', '8');
  }
  return (
    base +
    `/operations/${encodeURIComponent(selected.operation.provision_id)}` +
    (selected.kind === 'files' ? '/files' : '') +
    '?' +
    query
  );
}

export async function readProvision(
  scope: EnvironmentScope,
  selected: Selection,
  signal: AbortSignal,
): Promise<ProvisionData> {
  const found = await get<ProvisionData>(provisionPath(scope, selected), {
    signal,
    maxBytes: PROVISION_MAX_BYTES,
  });
  return inScope(found, { project_id: scope.projectId, environment_id: scope.environmentId });
}
