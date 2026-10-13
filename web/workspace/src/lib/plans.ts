// SPDX-License-Identifier: MIT
/** Read-only, scoped views of committed session plans. */
import { kernelReadsAllowed, onKernelLogout } from './kernels';

export interface PlanScope { projectId: string; sessionId: string }
export interface PlanRef { plan_id: string; version: number }
export type StepStatus = 'pending' | 'in_progress' | 'completed' | 'blocked' | 'skipped';
export type PlanStatus = 'draft' | 'awaiting_approval' | 'active' | 'completed' | 'abandoned';
export interface PlanAuthor {
  scope: {project_id: string; session_id: string};
  role: 'user' | 'main' | 'delegated';
  user_id: string | null; run_id: string | null; alias: string | null;
  resolved_model: string | null; tool_call: string | null; grant: PlanRef | null; track_id: string | null;
}
export interface PlanStep { step_id: string; title: string; description: string; evidence_standard: string; track_id: string | null }
export interface PlanBody {
  goal: string; feasibility: string;
  outputs: {title: string; description: string; evidence_standard: string}[];
  tracks: {track_id: string; title: string}[];
  phases: {phase_id: string; title: string; steps: PlanStep[]}[];
}
export interface PlanOutcome {
  status: 'achieved' | 'partially_achieved' | 'not_achieved'; summary: string;
  evidence: {project_id: string; kind: string; reference_id: string; revision: number | null; sha256: string | null; attempt_id: string | null}[];
  remaining_work: string[];
}
export interface PlanProgressView {
  ref: PlanRef; sequence: number; step_sha256: string; status: StepStatus;
  author: PlanAuthor; recorded_at: string; evidence_count: number;
}
export interface PlanStepView { step_id: string; step_sha256: string; matches_current_step: boolean; latest: PlanProgressView | null }
export interface CompactPlan {
  event_cursor: number;
  head: {scope: {project_id: string; session_id: string}; ref: PlanRef; status_revision: number; progress_cursor: number; status: PlanStatus; outcome: PlanOutcome | null};
  version: {scope: {project_id: string; session_id: string}; ref: PlanRef; base: number | null; body: PlanBody; author: PlanAuthor; reason: string; recorded_at: string};
  steps: PlanStepView[];
}
export interface PlanObservation {
  project_id: string; session_id: string; observed_at: string; session_closed_at: string | null; plan: CompactPlan | null;
}
export type PlanPhase = 'loading' | 'observed' | 'paused' | 'unavailable' | 'missing' | 'signed_out';
export interface PlanPollState { phase: PlanPhase; data: PlanObservation | null }
export class PlanReadError extends Error { constructor(readonly status: number) { super('Session plan unavailable'); } }
export const PLAN_MAX_BYTES = 256 * 1024;
export const PLAN_DEADLINE_MS = 8000;
export const PLAN_INTERVAL_MS = 3000;
const identifier = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const digest = /^[0-9a-f]{64}$/;
const statuses = new Set<StepStatus>(['pending', 'in_progress', 'completed', 'blocked', 'skipped']);
function object(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value); }
function text(value: unknown, max = 4096): value is string { return typeof value === 'string' && value.length > 0 && value.length <= max * 2 && [...value].length <= max && !/[\u0000\uD800-\uDFFF]/u.test(value); }
function id(value: unknown): value is string { return typeof value === 'string' && identifier.test(value); }
function counter(value: unknown, minimum = 0): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum; }
function timestamp(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 64) return false;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match || !Number.isFinite(Date.parse(value))) return false;
  const [, year, month, day, hour, minute, second, zone] = match;
  const y = Number(year), m = Number(month), d = Number(day);
  const days = [31, y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return y > 0 && m >= 1 && m <= 12 && d >= 1 && d <= days[m - 1]! && Number(hour) < 24 && Number(minute) < 60 && Number(second) < 60 &&
    (zone === 'Z' || (Number(zone?.slice(1, 3)) < 24 && Number(zone?.slice(4)) < 60));
}
function sameScope(value: unknown, scope: PlanScope): boolean { return object(value) && value.project_id === scope.projectId && value.session_id === scope.sessionId; }
export function planPanelScope(project: string, session: string, detail: {project_id: string; session: {session_id: string}}): PlanScope | null {
  return id(project) && id(session) && detail.project_id === project && detail.session.session_id === session ? {projectId: project, sessionId: session} : null;
}
function ref(value: unknown): value is PlanRef { return object(value) && id(value.plan_id) && counter(value.version, 1); }
function sameRef(a: PlanRef, b: PlanRef): boolean { return a.plan_id === b.plan_id && a.version === b.version; }
function author(value: unknown, scope: PlanScope): value is PlanAuthor {
  if (!object(value) || !sameScope(value.scope, scope)) return false;
  for (const name of ['user_id', 'run_id', 'tool_call', 'track_id']) if (value[name] !== null && !id(value[name])) return false;
  for (const name of ['alias', 'resolved_model']) if (value[name] !== null && !text(value[name], 256)) return false;
  if (value.role === 'user') return id(value.user_id) && ['run_id', 'alias', 'resolved_model', 'tool_call', 'grant', 'track_id'].every(key => value[key] === null);
  if (value.user_id !== null || !id(value.run_id) || value.run_id.toLowerCase() === 'user') return false;
  if (value.role === 'main') return value.grant === null && value.track_id === null;
  return value.role === 'delegated' && ref(value.grant) && id(value.track_id);
}
function body(value: unknown): value is PlanBody {
  if (!object(value) || !text(value.goal) || !text(value.feasibility) || !Array.isArray(value.outputs) || value.outputs.length < 1 || value.outputs.length > 32 ||
    !Array.isArray(value.tracks) || value.tracks.length > 16 || !Array.isArray(value.phases) || value.phases.length < 1 || value.phases.length > 16) return false;
  if (value.outputs.some((v: unknown) => !object(v) || !text(v.title, 128) || !text(v.description) || !text(v.evidence_standard))) return false;
  const tracks = new Set<string>(), phases = new Set<string>(), steps = new Set<string>();
  for (const track of value.tracks as unknown[]) {
    if (!object(track) || !id(track.track_id) || tracks.has(track.track_id) || !text(track.title, 128)) return false;
    tracks.add(track.track_id);
  }
  for (const phase of value.phases as unknown[]) {
    if (!object(phase) || !id(phase.phase_id) || phases.has(phase.phase_id) || !text(phase.title, 128) || !Array.isArray(phase.steps) || phase.steps.length < 1 || phase.steps.length > 128) return false;
    phases.add(phase.phase_id);
    for (const step of phase.steps as unknown[]) {
      if (!object(step) || !id(step.step_id) || steps.has(step.step_id) || !text(step.title, 128) || !text(step.description) || !text(step.evidence_standard) ||
        (step.track_id !== null && (typeof step.track_id !== 'string' || !tracks.has(step.track_id)))) return false;
      steps.add(step.step_id);
    }
  }
  return steps.size <= 128 && new TextEncoder().encode(JSON.stringify(value)).length <= 65_536;
}
export { body as validPlanBody };
function outcome(value: unknown, scope: PlanScope): value is PlanOutcome {
  if (!object(value) || typeof value.status !== 'string' || !['achieved', 'partially_achieved', 'not_achieved'].includes(value.status) || !text(value.summary) ||
    !Array.isArray(value.remaining_work) || value.remaining_work.length > 16 || value.remaining_work.some((v: unknown) => !text(v)) || !Array.isArray(value.evidence) || value.evidence.length > 8 ||
    (value.status === 'achieved' ? value.remaining_work.length !== 0 : value.remaining_work.length === 0)) return false;
  return value.evidence.every((v: unknown) => {
    if (!object(v) || v.project_id !== scope.projectId || !id(v.reference_id)) return false;
    if (v.kind === 'artifact') return counter(v.revision, 1) && typeof v.sha256 === 'string' && digest.test(v.sha256) && v.attempt_id === null;
    if (v.kind === 'execution') return id(v.attempt_id) && v.revision === null && v.sha256 === null;
    return v.kind === 'ledger' && counter(v.revision, 1) && v.sha256 === null && v.attempt_id === null;
  });
}
/** Check exact scope and current-step pins before rendering any progress. */
export function planObservation(value: unknown, scope: PlanScope): PlanObservation {
  const bad = () => new PlanReadError(0);
  if (!id(scope.projectId) || !id(scope.sessionId) || !object(value) || !sameScope(value, scope) || !timestamp(value.observed_at) ||
    (value.session_closed_at !== null && !timestamp(value.session_closed_at))) throw bad();
  if (value.plan === null) return value as unknown as PlanObservation;
  const plan = value.plan;
  if (!object(plan) || !counter(plan.event_cursor, 1) || !object(plan.head) || !object(plan.version) || !Array.isArray(plan.steps)) throw bad();
  const head = plan.head, version = plan.version;
  if (!sameScope(head.scope, scope) || !sameScope(version.scope, scope) || !ref(head.ref) || !ref(version.ref) || !sameRef(head.ref, version.ref) ||
    !counter(head.status_revision, 1) || !counter(head.progress_cursor) || typeof head.status !== 'string' || !['draft', 'awaiting_approval', 'active', 'completed', 'abandoned'].includes(head.status) ||
    !body(version.body) || !author(version.author, scope) || !text(version.reason) || !timestamp(version.recorded_at) ||
    (version.base !== null && (!counter(version.base, 1) || version.base >= version.ref.version))) throw bad();
  if (head.status === 'completed' ? !outcome(head.outcome, scope) : head.outcome !== null) throw bad();
  const ids = version.body.phases.flatMap(phase => phase.steps.map(step => step.step_id));
  if (plan.steps.length !== ids.length) throw bad();
  for (let index = 0; index < ids.length; index++) {
    const step: unknown = plan.steps[index];
    if (!object(step) || step.step_id !== ids[index] || typeof step.step_sha256 !== 'string' || !digest.test(step.step_sha256) || typeof step.matches_current_step !== 'boolean') throw bad();
    if (step.latest === null) { if (step.matches_current_step) throw bad(); continue; }
    const latest = step.latest;
    if (!object(latest) || !ref(latest.ref) || latest.ref.plan_id !== head.ref.plan_id || latest.ref.version > head.ref.version || !counter(latest.sequence, 1) || latest.sequence > head.progress_cursor ||
      typeof latest.step_sha256 !== 'string' || !digest.test(latest.step_sha256) || !statuses.has(latest.status as StepStatus) || !author(latest.author, scope) || !timestamp(latest.recorded_at) ||
      !counter(latest.evidence_count) || latest.evidence_count > 8 || step.matches_current_step !== (step.step_sha256 === latest.step_sha256)) throw bad();
  }
  const result = value as unknown as PlanObservation;
  if (head.status === 'completed') {
    const states = result.plan!.steps.map(currentStepStatus);
    if (states.some(status => status === 'pending' || status === 'in_progress') ||
      (result.plan!.head.outcome!.status === 'achieved' && states.some(status => status !== 'completed'))) throw bad();
  }
  return result;
}
export function currentStepStatus(step: PlanStepView): StepStatus { return step.matches_current_step && step.latest ? step.latest.status : 'pending'; }
export function planCounts(plan: CompactPlan): Record<StepStatus, number> {
  const counts = {pending: 0, in_progress: 0, completed: 0, blocked: 0, skipped: 0};
  for (const step of plan.steps) counts[currentStepStatus(step)]++;
  return counts;
}
export function authorLabel(value: PlanAuthor): string { return value.role === 'user' ? 'USER' : value.alias ?? (value.role === 'main' ? 'MAIN' : 'Delegated agent'); }

let readOwned = false;
export async function readSessionPlan(scope: PlanScope, signal: AbortSignal): Promise<PlanObservation> {
  if (!kernelReadsAllowed()) throw new PlanReadError(401);
  if (readOwned || signal.aborted || !id(scope.projectId) || !id(scope.sessionId)) throw new PlanReadError(0);
  readOwned = true;
  const controller = new AbortController();
  let response: Response | undefined, reader: ReadableStreamDefaultReader<Uint8Array> | undefined, cancellation: Promise<unknown> | undefined;
  const cancel = () => cancellation ??= (reader ? reader.cancel() : response?.body?.cancel() ?? Promise.resolve()).catch(() => undefined);
  const abort = () => { controller.abort(); if (response) void cancel(); };
  signal.addEventListener('abort', abort, {once: true});
  const deadline = setTimeout(abort, PLAN_DEADLINE_MS);
  try {
    response = await fetch(`/api/v0/projects/${encodeURIComponent(scope.projectId)}/sessions/${encodeURIComponent(scope.sessionId)}/plan`, {
      headers: {Accept: 'application/json'}, signal: controller.signal, redirect: 'error', cache: 'no-store',
    });
    if (controller.signal.aborted || response.redirected || response.type === 'opaqueredirect') throw new PlanReadError(0);
    if (!response.ok) { await cancel(); throw new PlanReadError(response.status); }
    if (!response.body) throw new PlanReadError(0);
    const declared = response.headers.get('content-length');
    if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > PLAN_MAX_BYTES)) throw new PlanReadError(0);
    reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0, reads = 0;
    while (true) {
      const chunk = await reader.read();
      if (controller.signal.aborted) throw new PlanReadError(0);
      if (chunk.done) break;
      if (++reads > 1024 || chunk.value.length > PLAN_MAX_BYTES - size) throw new PlanReadError(0);
      chunks.push(chunk.value); size += chunk.value.length;
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return planObservation(JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes)) as unknown, scope);
  } catch (error) { throw error instanceof PlanReadError ? error : new PlanReadError(0); }
  finally { await cancel(); reader?.releaseLock(); clearTimeout(deadline); signal.removeEventListener('abort', abort); readOwned = false; }
}

/** Keep ownership until settlement; abort never authorizes an overlapping read. */
export class PlanPoller {
  private visible = false; private visibilityKnown = false; private stopped = false; private epoch = 0; private busy = false; private failures = 0;
  private timer: ReturnType<typeof setTimeout> | undefined; private deadline: ReturnType<typeof setTimeout> | undefined; private controller: AbortController | undefined;
  private state: PlanPollState = {phase: 'loading', data: null};
  constructor(private readonly read: (signal: AbortSignal) => Promise<PlanObservation>, private readonly publish: (state: PlanPollState) => void, private readonly once = false) {}
  private emit(phase: PlanPhase, data = this.state.data): void { this.state = {phase, data}; this.publish(this.state); }
  setVisible(visible: boolean): void {
    if (this.stopped || (this.visibilityKnown && this.visible === visible)) return;
    this.visibilityKnown = true; this.visible = visible; this.epoch++; clearTimeout(this.timer); clearTimeout(this.deadline); this.controller?.abort();
    if (visible) { this.emit('loading'); void this.refresh(); } else this.emit('paused');
  }
  stop(): void { this.stopped = true; this.epoch++; clearTimeout(this.timer); clearTimeout(this.deadline); this.controller?.abort(); }
  logout(): void { this.stop(); this.emit('signed_out', null); }
  private async refresh(): Promise<void> {
    if (this.stopped || !this.visible || this.busy) return;
    this.busy = true;
    const epoch = this.epoch, controller = new AbortController(); this.controller = controller;
    let timedOut = false;
    this.deadline = setTimeout(() => { timedOut = true; controller.abort(); if (!this.stopped && this.visible && epoch === this.epoch) this.emit('unavailable'); }, PLAN_DEADLINE_MS);
    try {
      const data = await this.read(controller.signal);
      if (!this.stopped && this.visible && epoch === this.epoch && !timedOut) {
        this.failures = 0; this.emit('observed', data);
        if (this.once || data.session_closed_at !== null) this.stop();
      }
    } catch (error) {
      if (!this.stopped && this.visible && epoch === this.epoch) {
        const status = error instanceof PlanReadError ? error.status : 0;
        if (status === 401) { this.logout(); return; }
        if (status === 404) { this.stop(); this.emit('missing', null); return; }
        this.emit('unavailable'); if (!timedOut) this.failures++;
      }
    } finally {
      clearTimeout(this.deadline); this.busy = false; if (timedOut) this.failures++;
      if (this.once && !this.stopped) {
        this.stop();
        if (this.state.phase !== 'observed') this.emit('unavailable');
      }
      if (!this.stopped && this.visible) this.timer = setTimeout(() => void this.refresh(), epoch !== this.epoch ? 0 : Math.min(30_000, PLAN_INTERVAL_MS * 2 ** Math.min(this.failures, 4)));
    }
  }
}

/** Reuse the existing workspace sign-out fence, including a failed logout POST. */
export const onPlanLogout = onKernelLogout;
