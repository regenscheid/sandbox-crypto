// SPDX-License-Identifier: MIT
/** Read-only, scoped views of committed session plans. */
import { get, inScope } from './http';

export interface PlanScope {
  projectId: string;
  sessionId: string;
}
export interface PlanRef {
  plan_id: string;
  version: number;
}
export type StepStatus = 'pending' | 'in_progress' | 'completed' | 'blocked' | 'skipped';
export type PlanStatus = 'draft' | 'awaiting_approval' | 'active' | 'completed' | 'abandoned';
export interface PlanAuthor {
  scope: { project_id: string; session_id: string };
  role: 'user' | 'main' | 'delegated';
  user_id: string | null;
  run_id: string | null;
  alias: string | null;
  resolved_model: string | null;
  tool_call: string | null;
  grant: PlanRef | null;
  track_id: string | null;
}
export interface PlanStep {
  step_id: string;
  title: string;
  description: string;
  evidence_standard: string;
  track_id: string | null;
}
export interface PlanBody {
  goal: string;
  feasibility: string;
  outputs: { title: string; description: string; evidence_standard: string }[];
  tracks: { track_id: string; title: string }[];
  phases: { phase_id: string; title: string; steps: PlanStep[] }[];
}
export interface PlanOutcome {
  status: 'achieved' | 'partially_achieved' | 'not_achieved';
  summary: string;
  evidence: {
    project_id: string;
    kind: string;
    reference_id: string;
    revision: number | null;
    sha256: string | null;
    attempt_id: string | null;
  }[];
  remaining_work: string[];
}
export interface PlanProgressView {
  ref: PlanRef;
  sequence: number;
  step_sha256: string;
  status: StepStatus;
  author: PlanAuthor;
  recorded_at: string;
  evidence_count: number;
}
export interface PlanStepView {
  step_id: string;
  step_sha256: string;
  matches_current_step: boolean;
  latest: PlanProgressView | null;
}
export interface CompactPlan {
  event_cursor: number;
  head: {
    scope: { project_id: string; session_id: string };
    ref: PlanRef;
    status_revision: number;
    progress_cursor: number;
    status: PlanStatus;
    outcome: PlanOutcome | null;
  };
  version: {
    scope: { project_id: string; session_id: string };
    ref: PlanRef;
    base: number | null;
    body: PlanBody;
    author: PlanAuthor;
    reason: string;
    recorded_at: string;
  };
  steps: PlanStepView[];
}
export interface PlanObservation {
  project_id: string;
  session_id: string;
  observed_at: string;
  session_closed_at: string | null;
  plan: CompactPlan | null;
}
const identifier = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function text(value: unknown, max = 4096): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= max * 2 &&
    [...value].length <= max &&
    !/[\u0000\uD800-\uDFFF]/u.test(value)
  );
}
function id(value: unknown): value is string {
  return typeof value === 'string' && identifier.test(value);
}
export function planPanelScope(
  project: string,
  session: string,
  detail: { project_id: string; session: { session_id: string } },
): PlanScope | null {
  return id(project) &&
    id(session) &&
    detail.project_id === project &&
    detail.session.session_id === session
    ? { projectId: project, sessionId: session }
    : null;
}
/** A plan body a user may save: the schema's shape and limits, checked before sending. */
function body(value: unknown): value is PlanBody {
  if (
    !object(value) ||
    !text(value.goal) ||
    !text(value.feasibility) ||
    !Array.isArray(value.outputs) ||
    value.outputs.length < 1 ||
    value.outputs.length > 32 ||
    !Array.isArray(value.tracks) ||
    value.tracks.length > 16 ||
    !Array.isArray(value.phases) ||
    value.phases.length < 1 ||
    value.phases.length > 16
  )
    return false;
  if (
    value.outputs.some(
      (v: unknown) =>
        !object(v) || !text(v.title, 128) || !text(v.description) || !text(v.evidence_standard),
    )
  )
    return false;
  const tracks = new Set<string>(),
    phases = new Set<string>(),
    steps = new Set<string>();
  for (const track of value.tracks as unknown[]) {
    if (
      !object(track) ||
      !id(track.track_id) ||
      tracks.has(track.track_id) ||
      !text(track.title, 128)
    )
      return false;
    tracks.add(track.track_id);
  }
  for (const phase of value.phases as unknown[]) {
    if (
      !object(phase) ||
      !id(phase.phase_id) ||
      phases.has(phase.phase_id) ||
      !text(phase.title, 128) ||
      !Array.isArray(phase.steps) ||
      phase.steps.length < 1 ||
      phase.steps.length > 128
    )
      return false;
    phases.add(phase.phase_id);
    for (const step of phase.steps as unknown[]) {
      if (
        !object(step) ||
        !id(step.step_id) ||
        steps.has(step.step_id) ||
        !text(step.title, 128) ||
        !text(step.description) ||
        !text(step.evidence_standard) ||
        (step.track_id !== null &&
          (typeof step.track_id !== 'string' || !tracks.has(step.track_id)))
      )
        return false;
      steps.add(step.step_id);
    }
  }
  return steps.size <= 128 && new TextEncoder().encode(JSON.stringify(value)).length <= 65_536;
}
export { body as validPlanBody };
/** Refuse an answer for another session than the one the view shows. */
export function planObservation(value: PlanObservation, scope: PlanScope): PlanObservation {
  return inScope(value, { project_id: scope.projectId, session_id: scope.sessionId });
}
export function currentStepStatus(step: PlanStepView): StepStatus {
  return step.matches_current_step && step.latest ? step.latest.status : 'pending';
}
export function planCounts(plan: CompactPlan): Record<StepStatus, number> {
  const counts = { pending: 0, in_progress: 0, completed: 0, blocked: 0, skipped: 0 };
  for (const step of plan.steps) counts[currentStepStatus(step)]++;
  return counts;
}
export function authorLabel(value: PlanAuthor): string {
  return value.role === 'user'
    ? 'USER'
    : (value.alias ?? (value.role === 'main' ? 'MAIN' : 'Delegated agent'));
}

export async function readSessionPlan(
  scope: PlanScope,
  signal: AbortSignal,
): Promise<PlanObservation> {
  const path = `/projects/${encodeURIComponent(scope.projectId)}/sessions/${encodeURIComponent(scope.sessionId)}/plan`;
  return planObservation(await get<PlanObservation>(path, { signal }), scope);
}
