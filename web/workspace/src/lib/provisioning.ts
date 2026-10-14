// SPDX-License-Identifier: MIT
/** Read-only CP-26 facts. No provisioning or permission action is exposed here. */
import type { Project } from './api';
import { get, inScope } from './http';

export interface EnvironmentScope {
  projectId: string;
  environmentId: string;
}
export interface EnvironmentChoice {
  scope: EnvironmentScope;
  shared: boolean;
  sessions: string[];
}
export type OperationState =
  | 'queued'
  | 'checking'
  | 'resolving'
  | 'awaiting_permission'
  | 'installing'
  | 'probing'
  | 'finished';
export interface OperationSummary {
  provision_id: string;
  operation_id: string;
  session_id: string;
  seq: number;
  state: OperationState;
  outcome: string | null;
  reason: string | null;
  stage: string | null;
  planned_revision: number | null;
  revision_before: number | null;
  revision_after: number | null;
  permission_request: number | null;
  submitted_at: string;
  started_at: string | null;
  ended_at: string | null;
  files_total: number;
  changes_total: number;
  probes_total: number;
  executions_total: number;
  notes_total: number;
}
interface Observation {
  project_id: string;
  environment_id: string;
  observed_at: string;
}
export interface ProvisionObservation extends Observation {
  gate: {
    generation: number;
    mode: 'ready' | 'hold' | 'needs_recovery';
    operation: string | null;
    revision: number | null;
    recorded: boolean;
  };
  current_revision: number | null;
  queue_total: number;
  operations: OperationSummary[];
}
export interface OperationDetail extends Observation {
  summary: OperationSummary;
  request: {
    operation_id: string;
    context: {
      project_id: string;
      session_id: string;
      environment_id: string;
      policy_revision: string | null;
      actor: string;
    };
    purpose: string;
    capability: string | null;
    conda: string[];
    pip: string[];
    pip_reason: string | null;
    system: string[];
    action?: 'install' | 'upgrade';
  };
  policy: string | null;
  catalog: { version: number; sha256: string } | null;
  next_step: string | null;
  error_present: boolean;
  error_kind: string | null;
}
export interface ResolvedFile {
  ecosystem: 'conda' | 'pypi';
  name: string;
  version: string;
  build: string | null;
  channel: string | null;
  subdir: string | null;
  file: string;
  url: string | null;
  sha256: string | null;
  license: string | null;
  agpl: boolean;
  action: 'allow' | 'alert' | 'escalate' | 'deny';
  rule: string | null;
}
export interface FilePage extends Observation {
  provision_id: string;
  seq: number;
  total: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  files: ResolvedFile[];
}
export interface RevisionSummary {
  revision: number;
  record_sha256: string;
  fingerprint: string;
  managed: boolean | null;
  changed_by: string | null;
  operation: string | null;
  recorded_at: string;
  lockfile: {
    sha256: string;
    size: number;
    conda_packages: number;
    pip_distributions: number;
  } | null;
  commands_total: number;
  omitted_commands: number;
  probes_total: number;
}
export interface HistoryPage extends Observation {
  retained_total: number;
  first_retained: number | null;
  target: number | null;
  after: number;
  limit: number;
  next_after: number | null;
  revisions: RevisionSummary[];
}
export interface Probe {
  capability: string | null;
  probe: string;
  version: number;
  kind: 'functional' | 'import_check';
  status: 'passed' | 'failed' | 'error';
  revision: number | null;
  execution_id: string | null;
  output: string;
  duration_seconds: number;
  operation: string | null;
  recorded_at: string;
}
export interface ProbePage extends Observation {
  revision: number;
  record_sha256: string;
  total: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  probes: Probe[];
}
export type LockfileRef = NonNullable<RevisionSummary['lockfile']>;
export interface LockfilePin {
  revision: number;
  record_sha256: string;
  lockfile: LockfileRef | null;
}
export interface LockfileHeader extends LockfileRef {
  python: string | null;
  platform: string | null;
  skipped: number;
}
export interface CondaEntry {
  name: string;
  version: string;
  build: string;
  channel: string | null;
  subdir: string | null;
  file: string | null;
  sha256: string | null;
  md5: string | null;
  license: string | null;
}
export interface PipEntry {
  name: string;
  version: string;
  installer: string | null;
  location: 'prefix' | 'user';
  url: string | null;
  sha256: string | null;
  license?: string | null;
}
export interface DeclaredLicense {
  declared: string | null;
  agpl: boolean | null;
}
export interface LockfileDifference {
  ecosystem: 'conda' | 'pip';
  name: string;
  location: 'prefix' | 'user' | null;
  change: 'added' | 'removed' | 'changed';
  before: CondaEntry | PipEntry | null;
  after: CondaEntry | PipEntry | null;
  changed_fields: string[];
  before_license: DeclaredLicense | null;
  after_license: DeclaredLicense | null;
}
export interface LockfileDiffPage extends Observation {
  before: LockfilePin;
  after: LockfilePin;
  status: 'complete' | 'held';
  holds: {
    side: 'before' | 'after';
    reason:
      | 'lockfile_unrecorded'
      | 'blob_missing'
      | 'unsupported_format'
      | 'skipped_entries'
      | 'input_limit';
  }[];
  before_header: LockfileHeader | null;
  after_header: LockfileHeader | null;
  header_changes: ('python' | 'platform')[];
  total: number | null;
  added: number | null;
  removed: number | null;
  changed: number | null;
  offset: number;
  limit: number;
  next_offset: number | null;
  differences: LockfileDifference[];
}
export interface RevisionPackage {
  ecosystem: 'conda' | 'pip';
  name: string;
  location: 'prefix' | 'user' | null;
  package: CondaEntry | PipEntry;
  license: DeclaredLicense;
}
export interface RevisionPackagePage extends Observation {
  selection: LockfilePin;
  status: 'complete' | 'held';
  holds: LockfileDiffPage['holds'][number]['reason'][];
  header: LockfileHeader | null;
  total: number | null;
  offset: number;
  limit: number;
  next_offset: number | null;
  packages: RevisionPackage[];
}
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
  }));
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
