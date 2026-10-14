// SPDX-License-Identifier: MIT
/** Read-only CP-26 facts. No provisioning or permission action is exposed here. */
import type { Project } from './api';
import { kernelReadsAllowed, onKernelLogout } from './kernels';

export interface EnvironmentScope { projectId: string; environmentId: string }
export interface EnvironmentChoice { scope: EnvironmentScope; shared: boolean; sessions: string[] }
export type OperationState = 'queued' | 'checking' | 'resolving' | 'awaiting_permission' | 'installing' | 'probing' | 'finished';
export interface OperationSummary {
  provision_id: string; operation_id: string; session_id: string; seq: number; state: OperationState;
  outcome: string | null; reason: string | null; stage: string | null;
  planned_revision: number | null; revision_before: number | null; revision_after: number | null;
  permission_request: number | null; submitted_at: string; started_at: string | null; ended_at: string | null;
  files_total: number; changes_total: number; probes_total: number; executions_total: number; notes_total: number;
}
interface Observation { project_id: string; environment_id: string; observed_at: string }
export interface ProvisionObservation extends Observation {
  gate: {generation: number; mode: 'ready' | 'hold' | 'needs_recovery'; operation: string | null; revision: number | null; recorded: boolean};
  current_revision: number | null; queue_total: number; operations: OperationSummary[];
}
export interface OperationDetail extends Observation {
  summary: OperationSummary;
  request: {operation_id: string; context: {project_id: string; session_id: string; environment_id: string; policy_revision: string | null; actor: string};
    purpose: string; capability: string | null; conda: string[]; pip: string[]; pip_reason: string | null; system: string[]};
  policy: string | null; catalog: {version: number; sha256: string} | null; next_step: string | null;
  error_present: boolean; error_kind: string | null;
}
export interface ResolvedFile {
  ecosystem: 'conda' | 'pypi'; name: string; version: string; build: string | null;
  channel: string | null; subdir: string | null; file: string; url: string | null; sha256: string | null;
  license: string | null; agpl: boolean; action: 'allow' | 'alert' | 'escalate' | 'deny'; rule: string | null;
}
export interface FilePage extends Observation {
  provision_id: string; seq: number; total: number; offset: number; limit: number; next_offset: number | null; files: ResolvedFile[];
}
export interface RevisionSummary {
  revision: number; record_sha256: string; fingerprint: string; managed: boolean | null;
  changed_by: string | null; operation: string | null; recorded_at: string;
  lockfile: {sha256: string; size: number; conda_packages: number; pip_distributions: number} | null;
  commands_total: number; omitted_commands: number; probes_total: number;
}
export interface HistoryPage extends Observation {
  retained_total: number; first_retained: number | null; target: number | null; after: number; limit: number;
  next_after: number | null; revisions: RevisionSummary[];
}
export interface Probe {
  capability: string | null; probe: string; version: number; kind: 'functional' | 'import_check'; status: 'passed' | 'failed' | 'error';
  revision: number | null; execution_id: string | null; output: string; duration_seconds: number; operation: string | null; recorded_at: string;
}
export interface ProbePage extends Observation {
  revision: number; record_sha256: string; total: number; offset: number; limit: number; next_offset: number | null; probes: Probe[];
}
export type LockfileRef = NonNullable<RevisionSummary['lockfile']>;
export interface LockfilePin { revision: number; record_sha256: string; lockfile: LockfileRef | null }
export interface LockfileHeader extends LockfileRef { python: string | null; platform: string | null; skipped: number }
export interface CondaEntry { name: string; version: string; build: string; channel: string | null; subdir: string | null; file: string | null; sha256: string | null; md5: string | null; license: string | null }
export interface PipEntry { name: string; version: string; installer: string | null; location: 'prefix' | 'user'; url: string | null; sha256: string | null; license?: string | null }
export interface DeclaredLicense { declared: string | null; agpl: boolean | null }
export interface LockfileDifference {
  ecosystem: 'conda' | 'pip'; name: string; location: 'prefix' | 'user' | null; change: 'added' | 'removed' | 'changed';
  before: CondaEntry | PipEntry | null; after: CondaEntry | PipEntry | null; changed_fields: string[];
  before_license: DeclaredLicense | null; after_license: DeclaredLicense | null;
}
export interface LockfileDiffPage extends Observation {
  before: LockfilePin; after: LockfilePin; status: 'complete' | 'held';
  holds: {side: 'before' | 'after'; reason: 'lockfile_unrecorded' | 'blob_missing' | 'unsupported_format' | 'skipped_entries' | 'input_limit'}[];
  before_header: LockfileHeader | null; after_header: LockfileHeader | null; header_changes: ('python' | 'platform')[];
  total: number | null; added: number | null; removed: number | null; changed: number | null;
  offset: number; limit: number; next_offset: number | null; differences: LockfileDifference[];
}
export type DiffIdentity = Pick<LockfileDiffPage, 'before_header' | 'after_header' | 'header_changes' | 'total' | 'added' | 'removed' | 'changed'>;
export interface RevisionPackage {
  ecosystem: 'conda' | 'pip'; name: string; location: 'prefix' | 'user' | null;
  package: CondaEntry | PipEntry; license: DeclaredLicense;
}
export interface RevisionPackagePage extends Observation {
  selection: LockfilePin; status: 'complete' | 'held'; holds: LockfileDiffPage['holds'][number]['reason'][];
  header: LockfileHeader | null; total: number | null; offset: number; limit: number;
  next_offset: number | null; packages: RevisionPackage[];
}
export type PackageIdentity = Pick<RevisionPackagePage, 'header' | 'total'>;
export type Selection = {kind: 'summary'} | {kind: 'operation'; operation: OperationSummary} |
  {kind: 'retained_operation'; provisionId: string} | {kind: 'files'; operation: OperationSummary; offset: number; retained?: true} | {kind: 'history'; after: number; target: number | null} |
  {kind: 'probes'; revision: RevisionSummary; offset: number} |
  {kind: 'packages'; revision: RevisionSummary; offset: number; expected?: PackageIdentity; previousKey?: string} |
  {kind: 'diff'; before: RevisionSummary; after: RevisionSummary; offset: number; expected?: DiffIdentity; previousKey?: string};
export type ProvisionData = ProvisionObservation | OperationDetail | FilePage | HistoryPage | ProbePage | LockfileDiffPage | RevisionPackagePage;
export class ProvisionReadError extends Error { constructor(readonly status: number) { super('Environment observation unavailable'); } }
export const PROVISION_MAX_BYTES = 64 * 1024;
export const PROVISION_DEADLINE_MS = 8000;
export const DIFF_DEADLINE_MS = 12000;
export const PACKAGES_DEADLINE_MS = 12000;
export const PROVISION_INTERVAL_MS = 3000;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const hash = /^[0-9a-f]{64}$/;
const outcomes = ['already_satisfied', 'completed', 'awaiting_permission', 'denied', 'failed', 'rolled_back', 'needs_recovery'];
const states = ['queued', 'checking', 'resolving', 'awaiting_permission', 'installing', 'probing', 'finished'];
const reasons = ['base_image_change_required', 'agpl_license', 'policy_denied', 'permission_denied', 'permission_timed_out', 'permission_unavailable', 'resolve_failed', 'install_failed', 'environment_changed', 'policy_changed', 'probe_failed', 'cancelled', 'worker_restarted'];
const stages = ['queue', 'check', 'resolve', 'permission', 'download', 'link', 'post-link', 'probe'];
function object(v: unknown, keys: string[]): v is Record<string, unknown> { return typeof v === 'object' && v !== null && !Array.isArray(v) && Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k)); }
function id(v: unknown): v is string { return typeof v === 'string' && idPattern.test(v); }
function count(v: unknown, max = Number.MAX_SAFE_INTEGER, min = 0): v is number { return typeof v === 'number' && Number.isSafeInteger(v) && v >= min && v <= max; }
function text(v: unknown, max: number, empty = false): v is string { return typeof v === 'string' && v.length <= max * 2 && (empty || v.length > 0) && [...v].length <= max && !/[\uD800-\uDFFF]/u.test(v); }
function nullable(v: unknown, check: (v: unknown) => boolean): boolean { return v === null || check(v); }
function digest(v: unknown): v is string { return typeof v === 'string' && hash.test(v); }
function choice(v: unknown, values: string[]): boolean { return typeof v === 'string' && values.includes(v); }
function time(v: unknown): v is string {
  if (typeof v !== 'string' || v.length > 64) return false;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(v);
  if (!m || !Number.isFinite(Date.parse(v))) return false;
  const y = Number(m[1]), month = Number(m[2]), d = Number(m[3]);
  const days = [31, y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return y > 0 && month >= 1 && month <= 12 && d >= 1 && d <= days[month - 1]! && Number(m[4]) < 24 && Number(m[5]) < 60 && Number(m[6]) < 60 && (m[7] === 'Z' || (Number(m[7]!.slice(1, 3)) < 24 && Number(m[7]!.slice(4)) < 60));
}
const common = ['project_id', 'environment_id', 'observed_at'];
function scoped(v: Record<string, unknown>, scope: EnvironmentScope): boolean { return id(scope.projectId) && id(scope.environmentId) && v.project_id === scope.projectId && v.environment_id === scope.environmentId && time(v.observed_at); }
const summaryKeys = ['provision_id', 'operation_id', 'session_id', 'seq', 'state', 'outcome', 'reason', 'stage', 'planned_revision', 'revision_before', 'revision_after', 'permission_request', 'submitted_at', 'started_at', 'ended_at', 'files_total', 'changes_total', 'probes_total', 'executions_total', 'notes_total'];
function summary(v: unknown): v is OperationSummary {
  return object(v, summaryKeys) && id(v.provision_id) && id(v.operation_id) && id(v.session_id) && count(v.seq) && choice(v.state, states) && nullable(v.outcome, v => choice(v, outcomes)) && nullable(v.reason, v => choice(v, reasons)) && nullable(v.stage, v => choice(v, stages)) &&
    ['planned_revision', 'revision_before', 'revision_after'].every(k => nullable(v[k], count)) && nullable(v.permission_request, v => count(v, Number.MAX_SAFE_INTEGER, 1)) && time(v.submitted_at) && nullable(v.started_at, time) && nullable(v.ended_at, time) &&
    count(v.files_total, 2000) && count(v.changes_total, 4000) && count(v.probes_total, 64) && count(v.executions_total, 128) && count(v.notes_total, 32);
}
function selectedSummary(v: unknown, selected: OperationSummary): v is OperationSummary { return summary(v) && summaryKeys.every(k => v[k as keyof OperationSummary] === selected[k as keyof OperationSummary]); }
function detailSummary(v: unknown, selected: Selection): v is OperationSummary { return selected.kind === 'operation' ? selectedSummary(v, selected.operation) : selected.kind === 'retained_operation' && summary(v) && v.provision_id === selected.provisionId; }
function specs(v: unknown): v is string[] { return Array.isArray(v) && v.length <= 32 && v.every((s: unknown) => text(s, 256)); }
function humanText(v: unknown): boolean { return text(v, 2000) && !/[\x00-\x08\x0b-\x1f\x7f]/.test(v); }
function requestKind(v: OperationDetail['request']): boolean {
  const packages = v.conda.length + v.pip.length + v.system.length > 0;
  return (v.capability !== null) !== packages && (v.pip.length > 0) === (v.pip_reason !== null) && !(v.conda.length && v.pip.length) &&
    (v.capability === null || /^[a-z0-9][a-z0-9_-]{0,63}$/.test(v.capability)) && v.system.every(s => /^[a-z0-9][a-z0-9.+-]{0,127}$/.test(s));
}
export function environmentChoices(projectId: string, project: Project): EnvironmentChoice[] {
  if (project.project_id !== projectId || !id(projectId) || !Array.isArray(project.environments) || project.environments.length > 256 || !id(project.default_environment_id)) return [];
  if (!Array.isArray(project.sessions) || project.sessions.length > 512 || project.sessions.some(s => !id(s.session_id) || !id(s.environment_id) || typeof s.dedicated !== 'boolean') || new Set(project.sessions.map(s => s.session_id)).size !== project.sessions.length) return [];
  const seen = new Set<string>();
  const values: EnvironmentChoice[] = [];
  for (const e of project.environments) {
    if (!id(e.environment_id) || seen.has(e.environment_id) || typeof e.default !== 'boolean' || e.default !== (e.environment_id === project.default_environment_id) || !Array.isArray(e.sessions) || e.sessions.length > 512 || e.sessions.some(s => !id(s)) || new Set(e.sessions).size !== e.sessions.length) return [];
    const recorded = project.sessions.filter(s => s.environment_id === e.environment_id);
    if (recorded.length !== e.sessions.length || recorded.some(s => !e.sessions.includes(s.session_id) || s.dedicated === e.default) || (!e.default && recorded.length === 0)) return [];
    seen.add(e.environment_id); values.push({scope: {projectId, environmentId: e.environment_id}, shared: e.default, sessions: [...e.sessions]});
  }
  return seen.has(project.default_environment_id) && project.sessions.every(s => seen.has(s.environment_id)) ? values : [];
}
function file(v: unknown): v is ResolvedFile {
  return object(v, ['ecosystem', 'name', 'version', 'build', 'channel', 'subdir', 'file', 'url', 'sha256', 'license', 'agpl', 'action', 'rule']) && choice(v.ecosystem, ['conda', 'pypi']) && text(v.name, 256) && /^[A-Za-z0-9_][A-Za-z0-9._+-]*$/.test(v.name) && text(v.version, 256) && nullable(v.build, v => text(v, 256)) && nullable(v.channel, v => text(v, 2048)) && nullable(v.subdir, v => text(v, 128)) && text(v.file, 255) && /^[A-Za-z0-9_][A-Za-z0-9._+!-]*$/.test(v.file) && nullable(v.url, v => text(v, 2048)) && nullable(v.sha256, digest) && nullable(v.license, v => text(v, 256) && /^[ -~]+$/.test(v)) && typeof v.agpl === 'boolean' && choice(v.action, ['allow', 'alert', 'escalate', 'deny']) && nullable(v.rule, v => text(v, 256));
}
function revision(v: unknown): v is RevisionSummary {
  if (!object(v, ['revision', 'record_sha256', 'fingerprint', 'managed', 'changed_by', 'operation', 'recorded_at', 'lockfile', 'commands_total', 'omitted_commands', 'probes_total']) || !count(v.revision) || !digest(v.record_sha256) || typeof v.fingerprint !== 'string' || !/^[0-9a-f]{32}$/.test(v.fingerprint) || !nullable(v.managed, v => typeof v === 'boolean') || !nullable(v.changed_by, id) || !nullable(v.operation, id) || !time(v.recorded_at) || !count(v.commands_total, 8) || !count(v.omitted_commands) || !count(v.probes_total, 64)) return false;
  return nullable(v.lockfile, lockfileRef);
}
const refKeys = ['sha256', 'size', 'conda_packages', 'pip_distributions'];
const headerKeys = [...refKeys, 'python', 'platform', 'skipped'];
function lockfileRef(v: unknown): v is LockfileRef {
  return object(v, refKeys) && digest(v.sha256) && count(v.size, 8 * 1024 * 1024) && count(v.conda_packages, 20_000) && count(v.pip_distributions, 20_000);
}
function sameRef(a: LockfileRef | null, b: LockfileRef | null): boolean {return a === null || b === null ? a === b : refKeys.every(k => a[k as keyof LockfileRef] === b[k as keyof LockfileRef]);}
function pin(v: unknown, selected: RevisionSummary): v is LockfilePin {
  return object(v, ['revision', 'record_sha256', 'lockfile']) && v.revision === selected.revision && v.record_sha256 === selected.record_sha256 && nullable(v.lockfile, lockfileRef) && sameRef(v.lockfile as LockfileRef | null, selected.lockfile);
}
const versionPattern = /^[A-Za-z0-9_.+!-]{1,256}$/;
const packagePattern = /^[A-Za-z0-9_][A-Za-z0-9._+-]{0,255}$/;
function header(v: unknown, ref: LockfileRef | null): v is LockfileHeader {
  return ref !== null && object(v, headerKeys) && refKeys.every(k => v[k] === ref[k as keyof LockfileRef]) && count(v.size, 8 * 1024 * 1024, 1) && nullable(v.python, v => typeof v === 'string' && versionPattern.test(v)) && nullable(v.platform, v => text(v, 128) && /^[a-z0-9]+(?:-[a-z0-9_]+)?$/.test(v)) && count(v.skipped);
}
function condaEntry(v: unknown): v is CondaEntry {
  return object(v, ['name', 'version', 'build', 'channel', 'subdir', 'file', 'sha256', 'md5', 'license']) && typeof v.name === 'string' && packagePattern.test(v.name) && typeof v.version === 'string' && versionPattern.test(v.version) && typeof v.build === 'string' && /^[A-Za-z0-9_.+]{1,256}$/.test(v.build) && nullable(v.channel, v => text(v, 2048) && /^(?:[A-Za-z0-9_][A-Za-z0-9._-]{0,255}(?:\/label\/[A-Za-z0-9._-]{1,255})?|(?:https?|file):\/\/[!-~]+)$/.test(v)) && nullable(v.subdir, v => text(v, 128) && /^[a-z0-9]+(?:-[a-z0-9_]+)?$/.test(v)) && nullable(v.file, v => typeof v === 'string' && /^[A-Za-z0-9_][A-Za-z0-9._+!-]{0,250}\.(?:conda|tar\.bz2)$/.test(v)) && nullable(v.sha256, digest) && nullable(v.md5, v => typeof v === 'string' && /^[0-9a-f]{32}$/.test(v)) && nullable(v.license, v => typeof v === 'string' && /^[ -~]{1,256}$/.test(v));
}
function pipEntry(v: unknown): v is PipEntry {
  return (object(v, ['name', 'version', 'installer', 'location', 'url', 'sha256']) || object(v, ['name', 'version', 'installer', 'location', 'url', 'sha256', 'license'])) && (!Object.hasOwn(v, 'license') || nullable(v.license, v => typeof v === 'string' && /^[ -~]{1,256}$/.test(v))) && typeof v.name === 'string' && packagePattern.test(v.name) && typeof v.version === 'string' && versionPattern.test(v.version) && nullable(v.installer, v => typeof v === 'string' && /^[A-Za-z0-9_.+-]{1,64}$/.test(v)) && choice(v.location, ['prefix', 'user']) && nullable(v.url, v => text(v, 2048) && /^(?:https?|file|(?:git|hg|svn|bzr)\+(?:https?|ssh|file)):\/\/[!-~]+$/.test(v)) && nullable(v.sha256, digest);
}
function declared(v: unknown, expected: string | null): v is DeclaredLicense {
  return object(v, ['declared', 'agpl']) && v.declared === expected && (expected === null ? v.agpl === null : v.agpl === /agpl|affero/i.test(expected));
}
export function differenceKey(v: LockfileDifference): string {return [v.ecosystem, v.location ?? '', v.name].join('\0');}
function difference(v: unknown): v is LockfileDifference {
  if (!object(v, ['ecosystem', 'name', 'location', 'change', 'before', 'after', 'changed_fields', 'before_license', 'after_license']) || !choice(v.ecosystem, ['conda', 'pip']) || typeof v.name !== 'string' || !packagePattern.test(v.name) || !choice(v.change, ['added', 'removed', 'changed']) || (v.ecosystem === 'conda' ? v.location !== null : !choice(v.location, ['prefix', 'user'])) || !Array.isArray(v.changed_fields) || !v.changed_fields.length || v.changed_fields.length > 10) return false;
  for (const side of ['before', 'after'] as const) {
    const entry = v[side], license = v[`${side}_license`];
    if (entry === null) {if (license !== null) return false; continue;}
    if (v.ecosystem === 'conda') {if (!condaEntry(entry) || entry.name !== v.name || !declared(license, entry.license)) return false;}
    else if (!pipEntry(entry) || entry.name.replace(/[-_.]+/g, '-').toLowerCase() !== v.name || entry.location !== v.location || !declared(license, entry.license ?? null)) return false;
  }
  if (v.change === 'added' ? v.before !== null || v.after === null : v.change === 'removed' ? v.before === null || v.after !== null : v.before === null || v.after === null) return false;
  const a = (v.before ?? {}) as Record<string, unknown>, b = (v.after ?? {}) as Record<string, unknown>;
  // The retained worker schema treats a missing side's optional field as null.
  const changed = [...new Set([...Object.keys(a), ...Object.keys(b)])].filter(k => (a[k] ?? null) !== (b[k] ?? null)).sort();
  const fields = v.changed_fields;
  return changed.length === fields.length && changed.every((k, i) => k === fields[i]);
}
export function diffIdentity(v: LockfileDiffPage): DiffIdentity {return {before_header: v.before_header, after_header: v.after_header, header_changes: [...v.header_changes], total: v.total, added: v.added, removed: v.removed, changed: v.changed};}
function sameIdentity(v: LockfileDiffPage, expected: DiffIdentity): boolean {
  return ['total', 'added', 'removed', 'changed'].every(k => v[k as 'total'] === expected[k as 'total']) &&
    (['before_header', 'after_header'] as const).every(side => {const a = v[side], b = expected[side]; return a === null || b === null ? a === b : headerKeys.every(k => a[k as keyof LockfileHeader] === b[k as keyof LockfileHeader]);}) &&
    v.header_changes.length === expected.header_changes.length && v.header_changes.every((k, i) => k === expected.header_changes[i]);
}
function diffPage(v: Record<string, unknown>, selection: Extract<Selection, {kind: 'diff'}>): boolean {
  if (!revision(selection.before) || !revision(selection.after) || !count(selection.offset, 80_000)) return false;
  if (!object(v, [...common, 'before', 'after', 'status', 'holds', 'before_header', 'after_header', 'header_changes', 'total', 'added', 'removed', 'changed', 'offset', 'limit', 'next_offset', 'differences']) || !pin(v.before, selection.before) || !pin(v.after, selection.after) || !choice(v.status, ['complete', 'held']) || !Array.isArray(v.holds) || v.holds.length > 2 || !v.holds.every(h => object(h, ['side', 'reason']) && choice(h.side, ['before', 'after']) && choice(h.reason, ['lockfile_unrecorded', 'blob_missing', 'unsupported_format', 'skipped_entries', 'input_limit'])) || new Set(v.holds.map(h => h.side)).size !== v.holds.length || !nullable(v.before_header, h => header(h, selection.before.lockfile)) || !nullable(v.after_header, h => header(h, selection.after.lockfile)) || !Array.isArray(v.header_changes) || !Array.isArray(v.differences) || v.offset !== selection.offset || v.limit !== 8) return false;
  if (v.status === 'held') return v.holds.length > 0 && [v.total, v.added, v.removed, v.changed, v.next_offset].every(x => x === null) && !v.header_changes.length && !v.differences.length;
  if (v.holds.length || v.before_header === null || v.after_header === null || (v.before_header as LockfileHeader).skipped !== 0 || (v.after_header as LockfileHeader).skipped !== 0 || !count(v.total, 80_000) || !count(v.added, 80_000) || !count(v.removed, 80_000) || !count(v.changed, 80_000) || v.total !== v.added + v.removed + v.changed || !v.differences.every(difference) || !page(v, v.differences, {offset: selection.offset, total: v.total})) return false;
  const rows = v.differences as LockfileDifference[], a = v.before_header as LockfileHeader, b = v.after_header as LockfileHeader;
  const oldCount = a.conda_packages + a.pip_distributions, newCount = b.conda_packages + b.pip_distributions;
  if (newCount - oldCount !== v.added - v.removed || v.added > newCount || v.removed > oldCount || v.changed > Math.min(oldCount, newCount)) return false;
  const changed = (['python', 'platform'] as const).filter(k => a[k] !== b[k]);
  const headerChanges = v.header_changes;
  if (changed.length !== headerChanges.length || changed.some((k, i) => k !== headerChanges[i]) || rows.some((r, i) => (i > 0 && differenceKey(rows[i - 1]!) >= differenceKey(r)) || (selection.previousKey !== undefined && differenceKey(r) <= selection.previousKey))) return false;
  if (['added', 'removed', 'changed'].some(k => rows.filter(r => r.change === k).length > (v[k] as number))) return false;
  return selection.expected === undefined || sameIdentity(v as unknown as LockfileDiffPage, selection.expected);
}
export function packageKey(v: RevisionPackage): string {return [v.ecosystem, v.location ?? '', v.name].join('\0');}
export function packageIdentity(v: RevisionPackagePage): PackageIdentity {return {header: v.header, total: v.total};}
function revisionPackage(v: unknown): v is RevisionPackage {
  if (!object(v, ['ecosystem', 'name', 'location', 'package', 'license']) || typeof v.name !== 'string' || !packagePattern.test(v.name)) return false;
  if (v.ecosystem === 'conda') return v.location === null && condaEntry(v.package) && v.package.name === v.name && declared(v.license, v.package.license) && (v.package.license === null || v.license.agpl === /agpl|affero/i.test(v.package.license));
  return v.ecosystem === 'pip' && pipEntry(v.package) && v.location === v.package.location && v.package.name.replace(/[-_.]+/g, '-').toLowerCase() === v.name && declared(v.license, v.package.license ?? null) && (v.package.license == null || v.license.agpl === /agpl|affero/i.test(v.package.license));
}
function packagePage(v: Record<string, unknown>, selected: Extract<Selection, {kind: 'packages'}>): boolean {
  if (!revision(selected.revision) || !count(selected.offset, 40_000) || !object(v, [...common, 'selection', 'status', 'holds', 'header', 'total', 'offset', 'limit', 'next_offset', 'packages']) || !pin(v.selection, selected.revision) || !choice(v.status, ['complete', 'held']) || !Array.isArray(v.holds) || v.holds.length > 1 || !v.holds.every(h => choice(h, ['lockfile_unrecorded', 'blob_missing', 'unsupported_format', 'skipped_entries', 'input_limit'])) || !nullable(v.header, h => header(h, selected.revision.lockfile)) || v.offset !== selected.offset || v.limit !== 8 || !Array.isArray(v.packages)) return false;
  if (v.status === 'held') return v.holds.length === 1 && v.total === null && v.next_offset === null && v.packages.length === 0;
  if (v.holds.length || v.header === null || (v.header as LockfileHeader).skipped !== 0 || !count(v.total, 40_000) || !v.packages.every(revisionPackage) || !page(v, v.packages, {offset: selected.offset, total: v.total})) return false;
  const h = v.header as LockfileHeader, rows = v.packages as RevisionPackage[];
  if (v.total !== h.conda_packages + h.pip_distributions || rows.filter(r => r.ecosystem === 'conda').length > h.conda_packages || rows.filter(r => r.ecosystem === 'pip').length > h.pip_distributions || rows.some((r, i) => (i > 0 && packageKey(rows[i - 1]!) >= packageKey(r)) || (selected.previousKey !== undefined && packageKey(r) <= selected.previousKey))) return false;
  const expected = selected.expected;
  return expected === undefined || (expected.total === v.total && expected.header !== null && headerKeys.every(k => h[k as keyof LockfileHeader] === expected.header![k as keyof LockfileHeader]));
}
function probe(v: unknown, n: number): v is Probe {
  return object(v, ['capability', 'probe', 'version', 'kind', 'status', 'revision', 'execution_id', 'output', 'duration_seconds', 'operation', 'recorded_at']) && nullable(v.capability, v => text(v, 64) && /^[a-z0-9][a-z0-9_-]*$/.test(v)) && text(v.probe, 128) && /^[a-z0-9][a-z0-9:._-]*$/.test(v.probe) && count(v.version, Number.MAX_SAFE_INTEGER, 1) && choice(v.kind, ['functional', 'import_check']) && choice(v.status, ['passed', 'failed', 'error']) && (v.revision === null || v.revision === n) && nullable(v.execution_id, id) && text(v.output, 2000, true) && typeof v.duration_seconds === 'number' && Number.isFinite(v.duration_seconds) && v.duration_seconds >= 0 && nullable(v.operation, id) && time(v.recorded_at);
}
function page(v: Record<string, unknown>, rows: unknown[], expected: {offset: number; total: number}): boolean {
  const end = expected.offset + rows.length;
  return v.offset === expected.offset && v.total === expected.total && v.limit === 8 && rows.length <= 8 && end <= expected.total && v.next_offset === (end < expected.total ? end : null) && (rows.length > 0 || end === expected.total);
}
export function provisionData(value: unknown, scope: EnvironmentScope, selection: Selection): ProvisionData {
  const bad = () => new ProvisionReadError(0);
  if (typeof value !== 'object' || value === null || Array.isArray(value) || !scoped(value as Record<string, unknown>, scope)) throw bad();
  const v = value as Record<string, unknown>;
  switch (selection.kind) {
    case 'packages': {
      if (!packagePage(v, selection)) throw bad();
      return v as unknown as RevisionPackagePage;
    }
    case 'diff': {
      if (!diffPage(v, selection)) throw bad();
      return v as unknown as LockfileDiffPage;
    }
    case 'summary': {
      if (!object(v, [...common, 'gate', 'current_revision', 'queue_total', 'operations']) || !object(v.gate, ['generation', 'mode', 'operation', 'revision', 'recorded']) || !count(v.gate.generation) || !choice(v.gate.mode, ['ready', 'hold', 'needs_recovery']) || !nullable(v.gate.operation, id) || !nullable(v.gate.revision, count) || typeof v.gate.recorded !== 'boolean' || !nullable(v.current_revision, count) || !count(v.queue_total, 32) || !Array.isArray(v.operations) || v.operations.length !== v.queue_total || !v.operations.every(summary) || new Set(v.operations.map(o => o.provision_id)).size !== v.queue_total || v.operations.some(o => o.state === 'finished')) throw bad();
      if ((v.gate.mode === 'ready' && v.gate.operation !== null) || (v.gate.mode === 'needs_recovery' && v.gate.operation === null)) throw bad();
      if (!v.gate.recorded && (v.gate.generation !== 0 || v.gate.mode !== 'ready' || v.gate.operation !== null || v.gate.revision !== null)) throw bad();
      return v as unknown as ProvisionObservation;
    }
    case 'retained_operation':
    case 'operation': {
      if (!object(v, [...common, 'summary', 'request', 'policy', 'catalog', 'next_step', 'error_present', 'error_kind']) || !detailSummary(v.summary, selection) || !object(v.request, ['operation_id', 'context', 'purpose', 'capability', 'conda', 'pip', 'pip_reason', 'system']) || v.request.operation_id !== v.summary.operation_id || !object(v.request.context, ['project_id', 'session_id', 'environment_id', 'policy_revision', 'actor']) || v.request.context.project_id !== scope.projectId || v.request.context.environment_id !== scope.environmentId || v.request.context.session_id !== v.summary.session_id || !id(v.request.context.actor) || !nullable(v.request.context.policy_revision, v => text(v, 256)) || !humanText(v.request.purpose) || !nullable(v.request.capability, v => text(v, 64)) || (!specs(v.request.conda) || !specs(v.request.pip) || !specs(v.request.system)) || !nullable(v.request.pip_reason, humanText) || !nullable(v.policy, v => typeof v === 'string' && /^sha256:[0-9a-f]{64}$/.test(v)) || !(v.catalog === null || (object(v.catalog, ['version', 'sha256']) && count(v.catalog.version, Number.MAX_SAFE_INTEGER, 1) && digest(v.catalog.sha256))) || !nullable(v.next_step, humanText) || typeof v.error_present !== 'boolean' || !nullable(v.error_kind, v => choice(v, ['invalid_request', 'unsupported', 'unavailable', 'policy_denied', 'not_found', 'conflict', 'timed_out', 'cancelled', 'kernel_died', 'exception', 'internal'])) || (v.error_present !== (v.error_kind !== null))) throw bad();
      if (!requestKind(v.request as unknown as OperationDetail['request'])) throw bad();
      return v as unknown as OperationDetail;
    }
    case 'files': {
      if (!object(v, [...common, 'provision_id', 'seq', 'total', 'offset', 'limit', 'next_offset', 'files']) || v.provision_id !== selection.operation.provision_id || v.seq !== selection.operation.seq || !Array.isArray(v.files) || !v.files.every(file) || !page(v, v.files, {offset: selection.offset, total: selection.operation.files_total})) throw bad();
      return v as unknown as FilePage;
    }
    case 'history': {
      if (!object(v, [...common, 'retained_total', 'first_retained', 'target', 'after', 'limit', 'next_after', 'revisions']) || !count(v.retained_total, 512) || !nullable(v.first_retained, count) || !nullable(v.target, count) || (selection.target !== null && v.target !== selection.target) || v.after !== selection.after || v.limit !== 8 || !Array.isArray(v.revisions) || v.revisions.length > 8 || !v.revisions.every(revision)) throw bad();
      const rows = v.revisions as RevisionSummary[];
      if ((v.retained_total === 0 && (v.first_retained !== null || v.target !== null || v.next_after !== null || rows.length !== 0)) || (v.retained_total > 0 && (v.first_retained === null || v.target === null || (v.first_retained as number) > (v.target as number) || rows.length > (v.retained_total as number))) || rows.some((r, i) => r.revision <= selection.after || v.target === null || r.revision > (v.target as number) || v.first_retained === null || r.revision < (v.first_retained as number) || (i > 0 && rows[i - 1]!.revision >= r.revision)) || (v.next_after !== null && (rows.length === 0 || v.next_after !== rows.at(-1)!.revision || v.next_after === v.target))) throw bad();
      return v as unknown as HistoryPage;
    }
    case 'probes': {
      if (!object(v, [...common, 'revision', 'record_sha256', 'total', 'offset', 'limit', 'next_offset', 'probes']) || v.revision !== selection.revision.revision || v.record_sha256 !== selection.revision.record_sha256 || !Array.isArray(v.probes) || !v.probes.every(p => probe(p, selection.revision.revision)) || !page(v, v.probes, {offset: selection.offset, total: selection.revision.probes_total})) throw bad();
      return v as unknown as ProbePage;
    }
  }
}

/** Only allowlisted selectors can produce a same-origin observation URL. */
export function provisionPath(scope: EnvironmentScope, selected: Selection): string {
  if (!id(scope.projectId) || !id(scope.environmentId)) throw new ProvisionReadError(0);
  const base = `/api/v0/projects/${encodeURIComponent(scope.projectId)}/environments/${encodeURIComponent(scope.environmentId)}/provisioning/observations`;
  const query = new URLSearchParams();
  if (selected.kind === 'summary') return base;
  if (selected.kind === 'diff') {
    if (!revision(selected.before) || !revision(selected.after) || !count(selected.offset, 80_000)) throw new ProvisionReadError(0);
    query.set('before', String(selected.before.revision)); query.set('before_pin', selected.before.record_sha256);
    query.set('after', String(selected.after.revision)); query.set('after_pin', selected.after.record_sha256);
    query.set('offset', String(selected.offset)); query.set('limit', '8');
    return base + '/lockfile-diff?' + query;
  }
  if (selected.kind === 'history') {
    if (!count(selected.after, Number.MAX_SAFE_INTEGER, -1) || !nullable(selected.target, count)) throw new ProvisionReadError(0);
    query.set('after', String(selected.after)); query.set('limit', '8');
    if (selected.target !== null) query.set('target', String(selected.target));
    return base + '/revisions?' + query;
  }
  if (selected.kind === 'retained_operation') {
    if (!id(selected.provisionId)) throw new ProvisionReadError(0);
    return base + `/operations/${encodeURIComponent(selected.provisionId)}`;
  }
  if (selected.kind === 'probes') {
    if (!revision(selected.revision) || !count(selected.offset) || selected.offset > selected.revision.probes_total) throw new ProvisionReadError(0);
    query.set('pin', selected.revision.record_sha256); query.set('offset', String(selected.offset)); query.set('limit', '8');
    return base + `/revisions/${selected.revision.revision}/probes?` + query;
  }
  if (selected.kind === 'packages') {
    if (!revision(selected.revision) || !count(selected.offset, 40_000)) throw new ProvisionReadError(0);
    query.set('pin', selected.revision.record_sha256); query.set('offset', String(selected.offset)); query.set('limit', '8');
    return base + `/revisions/${selected.revision.revision}/packages?` + query;
  }
  if (!summary(selected.operation)) throw new ProvisionReadError(0);
  query.set('seq', String(selected.operation.seq));
  if (selected.kind === 'files') {
    if (!count(selected.offset) || selected.offset > selected.operation.files_total) throw new ProvisionReadError(0);
    query.set('offset', String(selected.offset)); query.set('limit', '8');
  }
  return base + `/operations/${encodeURIComponent(selected.operation.provision_id)}` + (selected.kind === 'files' ? '/files' : '') + '?' + query;
}

let actualReadOwned = false;
export async function readProvision(scope: EnvironmentScope, selected: Selection, signal: AbortSignal): Promise<ProvisionData> {
  if (!kernelReadsAllowed()) throw new ProvisionReadError(401);
  const path = provisionPath(scope, selected);
  if (actualReadOwned || signal.aborted) throw new ProvisionReadError(0);
  actualReadOwned = true;
  const controller = new AbortController();
  let response: Response | undefined, reader: ReadableStreamDefaultReader<Uint8Array> | undefined, cancellation: Promise<unknown> | undefined;
  const cancel = () => cancellation ??= (reader ? reader.cancel() : response?.body?.cancel() ?? Promise.resolve()).catch(() => undefined);
  const abort = () => {controller.abort(); if (response) void cancel();};
  signal.addEventListener('abort', abort, {once: true});
  const logout = onKernelLogout(abort);
  const timeout = setTimeout(abort, selected.kind === 'diff' ? DIFF_DEADLINE_MS : selected.kind === 'packages' ? PACKAGES_DEADLINE_MS : PROVISION_DEADLINE_MS);
  try {
    response = await fetch(path, {headers: {Accept: 'application/json'}, signal: controller.signal, redirect: 'error', cache: 'no-store'});
    if (controller.signal.aborted || !kernelReadsAllowed() || response.redirected || response.type === 'opaqueredirect') throw new ProvisionReadError(0);
    if (!response.ok) {await cancel(); throw new ProvisionReadError(response.status);}
    if (!response.body) throw new ProvisionReadError(0);
    const length = response.headers.get('content-length');
    if (length !== null && (!/^\d+$/.test(length) || Number(length) > PROVISION_MAX_BYTES)) throw new ProvisionReadError(0);
    reader = response.body.getReader();
    const pieces: Uint8Array[] = []; let bytes = 0, chunks = 0;
    while (true) {
      const item = await reader.read();
      if (controller.signal.aborted || !kernelReadsAllowed()) throw new ProvisionReadError(0);
      if (item.done) break;
      if (++chunks > 256 || item.value.length > PROVISION_MAX_BYTES - bytes) throw new ProvisionReadError(0);
      pieces.push(item.value); bytes += item.value.length;
    }
    const joined = new Uint8Array(bytes); let start = 0;
    for (const piece of pieces) {joined.set(piece, start); start += piece.length;}
    return provisionData(JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(joined)) as unknown, scope, selected);
  } catch (error) {throw error instanceof ProvisionReadError ? error : new ProvisionReadError(0);}
  finally {await cancel(); reader?.releaseLock(); clearTimeout(timeout); signal.removeEventListener('abort', abort); logout(); actualReadOwned = false;}
}

export type ReadPhase = 'loading' | 'observed' | 'paused' | 'unavailable' | 'unsupported' | 'missing' | 'changed' | 'signed_out';
export interface ReadState { phase: ReadPhase; data: ProvisionData | null; busy: boolean }
/** One sequential selected read stream. Even an ignored abort retains its actual owner. */
export class ProvisionReader {
  private visible = false; private known = false; private stopped = false; private epoch = 0; private busy = false; private failures = 0;
  private timer: ReturnType<typeof setTimeout> | undefined; private deadline: ReturnType<typeof setTimeout> | undefined;
  private controller: AbortController | undefined; private state: ReadState = {phase: 'loading', data: null, busy: false};
  constructor(private readonly read: (signal: AbortSignal) => Promise<ProvisionData>, private readonly publish: (value: ReadState) => void, private readonly poll = true, private readonly deadlineMs = PROVISION_DEADLINE_MS, private readonly settleOnce = false) {}
  private emit(phase: ReadPhase, data = this.state.data): void {this.state = {phase, data, busy: this.busy}; this.publish(this.state);}
  setVisible(visible: boolean): void {
    if (this.stopped || (this.known && this.visible === visible)) return;
    this.known = true; this.visible = visible; this.epoch++; clearTimeout(this.timer); clearTimeout(this.deadline); this.controller?.abort();
    if (visible) {this.emit('loading'); void this.refresh();} else this.emit('paused');
  }
  stop(): void {this.stopped = true; this.epoch++; clearTimeout(this.timer); clearTimeout(this.deadline); this.controller?.abort();}
  logout(): void {this.stop(); this.emit('signed_out', null);}
  private async refresh(): Promise<void> {
    if (this.stopped || !this.visible || this.busy) return;
    this.busy = true; this.emit(this.state.phase);
    const epoch = this.epoch, controller = new AbortController(); this.controller = controller;
    let expired = false;
    this.deadline = setTimeout(() => {expired = true; controller.abort(); if (!this.stopped && this.visible && epoch === this.epoch) this.emit('unavailable');}, this.deadlineMs);
    try {
      const data = await this.read(controller.signal);
      if (!expired && !this.stopped && this.visible && epoch === this.epoch) {this.failures = 0; this.emit('observed', data);}
    } catch (error) {
      if (!this.stopped && this.visible && epoch === this.epoch) {
        const status = error instanceof ProvisionReadError ? error.status : 0;
        if (status === 401) {this.busy = false; this.logout(); return;}
        if (status === 404) {this.busy = false; this.emit('missing', null); this.stop(); return;}
        if (status === 409) {this.busy = false; this.emit('changed'); this.stop(); return;}
        this.emit(status === 422 ? 'unsupported' : 'unavailable'); this.failures++;
      }
    } finally {
      clearTimeout(this.deadline); this.busy = false;
      if (!this.stopped && this.visible && epoch === this.epoch) this.emit(this.state.phase);
      if (this.settleOnce && !this.stopped) {this.emit(this.state.phase === 'loading' ? 'unavailable' : this.state.phase); this.stop();}
      if (!this.stopped && this.visible) {
        if (this.poll || epoch !== this.epoch) this.timer = setTimeout(() => void this.refresh(), epoch !== this.epoch ? 0 : Math.min(30_000, PROVISION_INTERVAL_MS * 2 ** Math.min(this.failures, 4)));
      }
    }
  }
}
