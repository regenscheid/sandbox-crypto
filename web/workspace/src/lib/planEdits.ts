// SPDX-License-Identifier: MIT
/** Scoped USER text drafts and exact uncertain requests; no plan execution authority. */
import type { BodyChange, PlanReceipt } from './api-types';
import { ApiError, post } from './http';
import { DraftStore } from './mutation';
import {
  planObservation,
  validPlanBody,
  type PlanBody,
  type PlanObservation,
  type PlanRef,
  type PlanScope,
} from './plans';
import { signedOut } from './session';

export const EDIT_RESPONSE_BYTES = 8192;
export const EDIT_DEADLINE_MS = 8000;
const MAX_DRAFT_BYTES = 512 * 1024;
const ids = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const counter = (value: unknown, min = 0): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= min;
const ref = (value: unknown): value is PlanRef =>
  object(value) &&
  typeof value.plan_id === 'string' &&
  ids.test(value.plan_id) &&
  counter(value.version, 1);
export const sameRef = (a: PlanRef, b: PlanRef): boolean =>
  a.plan_id === b.plan_id && a.version === b.version;
const validText = (value: unknown, max: number): value is string =>
  typeof value === 'string' &&
  value.length <= 2 * max &&
  [...value].length <= max &&
  !!value.trim() &&
  !/[\u0000\uD800-\uDFFF]/u.test(value);

export interface TextField {
  key: string;
  label: string;
  max: number;
  path: (string | number)[];
  value: string;
}
function at(body: unknown, path: readonly (string | number)[]): unknown {
  let value = body;
  for (const part of path) {
    if (Array.isArray(value) && typeof part === 'number') value = value[part];
    else if (object(value) && typeof part === 'string') value = value[part];
    else return undefined;
  }
  return value;
}
function set(body: PlanBody, path: readonly (string | number)[], value: string): void {
  const parent = at(body, path.slice(0, -1));
  if (!object(parent)) throw new Error('Plan field unavailable');
  parent[String(path.at(-1))] = value;
}
/** Paths are generated from the existing body; no identifier/reference field is editable. */
export function textFields(body: PlanBody): TextField[] {
  const fields: TextField[] = [];
  const add = (key: string, label: string, max: number, path: (string | number)[]) =>
    fields.push({ key, label, max, path, value: at(body, path) as string });
  add('goal', 'Goal', 4096, ['goal']);
  add('feasibility', 'Feasibility', 4096, ['feasibility']);
  body.outputs.forEach((_output, i) => {
    for (const [name, label, max] of [
      ['title', 'title', 128],
      ['description', 'description', 4096],
      ['evidence_standard', 'evidence standard', 4096],
    ] as const)
      add(`output-${i}-${name}`, `Output ${i + 1} ${label}`, max, ['outputs', i, name]);
  });
  body.tracks.forEach((track, i) =>
    add(`track-${track.track_id}`, `Track ${i + 1} title`, 128, ['tracks', i, 'title']),
  );
  body.phases.forEach((phase, i) => {
    add(`phase-${phase.phase_id}`, `Phase ${i + 1} title`, 128, ['phases', i, 'title']);
    phase.steps.forEach((step, j) => {
      for (const [name, label, max] of [
        ['title', 'title', 128],
        ['description', 'description', 4096],
        ['evidence_standard', 'evidence standard', 4096],
      ] as const)
        add(`step-${step.step_id}-${name}`, `Phase ${i + 1}, step ${j + 1} ${label}`, max, [
          'phases',
          i,
          'steps',
          j,
          name,
        ]);
    });
  });
  return fields;
}
/** Preserve every nontext field, including array order, IDs and track links. */
export function sameStructure(a: PlanBody, b: PlanBody): boolean {
  try {
    const shape = (body: PlanBody) => {
      const result = copy(body);
      for (const field of textFields(result)) set(result, field.path, '');
      return JSON.stringify(result);
    };
    return shape(a) === shape(b);
  } catch {
    return false;
  }
}
function asciiBytes(value: unknown): number {
  return JSON.stringify(value).replace(
    /[\u007f-\uffff]/g,
    (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`,
  ).length;
}
export function editProblem(body: PlanBody, reason: string): string | null {
  if (!validText(reason, 4096)) return 'Add a nonempty reason of at most 4,096 characters.';
  if (!validPlanBody(body) || textFields(body).some((field) => !validText(field.value, field.max)))
    return 'Complete every text field within its character limit.';
  if (asciiBytes(body) > 65_536) return 'The plan text exceeds the saved-plan size limit.';
  return null;
}

/** A body edit against an exact base version. */
export type PlanChange = BodyChange;
export type { PlanReceipt };
function receipt(value: unknown, scope: PlanScope, request: PlanChange): PlanReceipt {
  if (
    !object(value) ||
    value.project_id !== scope.projectId ||
    value.session_id !== scope.sessionId ||
    !object(value.receipt)
  )
    throw new PlanWriteError(0);
  const r = value.receipt;
  if (
    !object(r.scope) ||
    r.scope.project_id !== scope.projectId ||
    r.scope.session_id !== scope.sessionId ||
    r.operation_id !== request.operation_id ||
    r.action !== 'revise' ||
    !ref(r.ref) ||
    r.ref.plan_id !== request.base.plan_id ||
    r.ref.version !== request.base.version + 1 ||
    !counter(r.event_sequence, 1) ||
    !counter(r.status_revision, 1) ||
    !counter(r.progress_cursor)
  )
    throw new PlanWriteError(0);
  return {
    scope: { project_id: scope.projectId, session_id: scope.sessionId },
    operation_id: request.operation_id,
    action: 'revise',
    ref: copy(r.ref),
    event_sequence: r.event_sequence,
    status_revision: r.status_revision,
    progress_cursor: r.progress_cursor,
  };
}
export class PlanWriteError extends Error {
  constructor(
    readonly status: number,
    readonly current: PlanRef | null = null,
  ) {
    super('Plan text save unavailable');
  }
  get definitive(): boolean {
    return [400, 403, 404, 409, 413].includes(this.status);
  }
}
const owned = new Set<string>();
const scopeKey = (scope: PlanScope, plan: string) =>
  `${scope.projectId}:${scope.sessionId}:${plan}`;
const writeKey = (scope: PlanScope) => `${scope.projectId}:${scope.sessionId}`;
/** One write per session at a time; the scope's key is held until the request settles. */
export async function writePlanChange(
  scope: PlanScope,
  request: PlanChange,
  signal: AbortSignal,
): Promise<PlanReceipt> {
  scope = { ...scope };
  const key = writeKey(scope);
  if (signedOut()) throw new PlanWriteError(401);
  if (owned.has(key) || signal.aborted || editProblem(request.body, request.reason))
    throw new PlanWriteError(0);
  request = copy(request);
  owned.add(key);
  try {
    const value = await post<unknown>(
      `/projects/${encodeURIComponent(scope.projectId)}/sessions/${encodeURIComponent(scope.sessionId)}/plan/changes`,
      request,
      { signal, deadlineMs: EDIT_DEADLINE_MS, maxBytes: EDIT_RESPONSE_BYTES },
    );
    return receipt(value, scope, request);
  } catch (error) {
    if (error instanceof PlanWriteError) throw error;
    if (!(error instanceof ApiError)) throw new PlanWriteError(0);
    const current = error.status === 409 && ref(error.error.current) ? error.error.current : null;
    throw new PlanWriteError(error.status, current ? copy(current) : null);
  } finally {
    owned.delete(key);
  }
}

export interface RetainedText {
  base: PlanRef;
  baseline: PlanBody;
  body: PlanBody;
  reason: string;
}
export interface PendingEdit {
  request: PlanChange;
  state: 'unknown' | 'acknowledged';
  receipt?: PlanReceipt;
}
export interface PlanDraft extends RetainedText {
  pending: PendingEdit | null;
  retained: RetainedText | null;
  unresolved: PlanChange | null;
}
function validDraft(value: unknown, scope: PlanScope, plan: string): value is PlanDraft {
  try {
    if (
      !object(value) ||
      !ref(value.base) ||
      value.base.plan_id !== plan ||
      !validPlanBody(value.baseline) ||
      !sameStructure(value.baseline, value.body as PlanBody) ||
      typeof value.reason !== 'string' ||
      value.reason.length > 8192
    )
      return false;
    if (
      textFields(value.body as PlanBody).some(
        (field) => typeof field.value !== 'string' || field.value.length > 2 * field.max,
      )
    )
      return false;
    if (value.retained !== null) {
      const r = value.retained;
      if (
        !object(r) ||
        !ref(r.base) ||
        r.base.plan_id !== plan ||
        !validPlanBody(r.baseline) ||
        !sameStructure(r.baseline, r.body as PlanBody) ||
        typeof r.reason !== 'string' ||
        r.reason.length > 8192 ||
        textFields(r.body as PlanBody).some(
          (f) => typeof f.value !== 'string' || f.value.length > 2 * f.max,
        )
      )
        return false;
    }
    if (value.unresolved !== null) {
      const u = value.unresolved;
      if (
        !object(u) ||
        !ref(u.base) ||
        u.base.plan_id !== plan ||
        typeof u.operation_id !== 'string' ||
        !ids.test(u.operation_id) ||
        typeof u.reason !== 'string' ||
        editProblem(u.body as PlanBody, u.reason)
      )
        return false;
    }
    if (value.pending !== null) {
      const p = value.pending;
      if (
        !object(p) ||
        !object(p.request) ||
        typeof p.request.operation_id !== 'string' ||
        !ids.test(p.request.operation_id) ||
        !ref(p.request.base) ||
        !sameRef(p.request.base, value.base) ||
        !sameStructure(value.baseline, p.request.body as PlanBody) ||
        typeof p.request.reason !== 'string' ||
        editProblem(p.request.body as PlanBody, p.request.reason) ||
        !['unknown', 'acknowledged'].includes(p.state as string)
      )
        return false;
      if (
        p.state === 'unknown' &&
        (JSON.stringify(p.request.body) !== JSON.stringify(value.body) ||
          p.request.reason !== value.reason)
      )
        return false;
      if (p.state === 'acknowledged')
        receipt(
          { project_id: scope.projectId, session_id: scope.sessionId, receipt: p.receipt },
          scope,
          p.request as unknown as PlanChange,
        );
    }
    return new TextEncoder().encode(JSON.stringify(value)).length <= MAX_DRAFT_BYTES;
  } catch {
    return false;
  }
}
const planDrafts = (scope: PlanScope, plan: string) =>
  new DraftStore<PlanDraft>(
    'plan',
    (value): value is PlanDraft => validDraft(value, scope, plan),
    MAX_DRAFT_BYTES,
  );
export function storePlanDraft(scope: PlanScope, plan: string, draft: PlanDraft): boolean {
  return planDrafts(scope, plan).save(scopeKey(scope, plan), draft);
}
export function loadPlanDraft(
  scope: PlanScope,
  plan: string,
): { draft: PlanDraft | null; warning: boolean; blocked: boolean } {
  const loaded = planDrafts(scope, plan).load(scopeKey(scope, plan));
  return { draft: loaded.draft, warning: !loaded.stored, blocked: loaded.damaged };
}
function clearDraft(scope: PlanScope, plan: string): boolean {
  return planDrafts(scope, plan).clear(scopeKey(scope, plan));
}
export interface EditorView {
  draft: PlanDraft;
  current: PlanObservation;
  eligible: boolean;
  busy: boolean;
  warning: boolean;
  blocked: boolean;
  signedOut: boolean;
  message: string;
  conflict: PlanRef | null;
  canContinue: boolean;
}
export class PlanEditSession {
  private current: PlanObservation;
  private draft: PlanDraft;
  private observed: boolean;
  private active = true;
  private epoch = 0;
  private busy = false;
  private warning = false;
  private blocked = false;
  private signedOut = false;
  private message = '';
  private conflict: PlanRef | null = null;
  private controller: AbortController | undefined;
  private readonly plan: string;
  private readonly scope: PlanScope;
  private unknownReadAt: string | null = null;
  constructor(
    scope: PlanScope,
    current: PlanObservation,
    observed: boolean,
    private readonly publish: (view: EditorView) => void,
    private readonly transport = writePlanChange,
    private readonly refresh: () => void = () => undefined,
  ) {
    this.scope = { ...scope };
    this.current = copy(planObservation(current, scope));
    if (!current.plan) throw new Error('No plan to edit');
    this.plan = current.plan.head.ref.plan_id;
    this.observed = observed;
    const loaded = loadPlanDraft(scope, this.plan);
    this.warning = loaded.warning;
    this.blocked = loaded.blocked;
    this.draft = loaded.draft ?? this.fresh();
    this.emit();
  }
  private fresh(): PlanDraft {
    const p = this.current.plan!;
    return {
      base: copy(p.head.ref),
      baseline: copy(p.version.body),
      body: copy(p.version.body),
      reason: 'User plan edit',
      pending: null,
      retained: null,
      unresolved: null,
    };
  }
  private get unresolved(): boolean {
    return this.draft.pending?.state === 'unknown';
  }
  private get waiting(): boolean {
    return this.busy || owned.has(writeKey(this.scope));
  }
  private get eligible(): boolean {
    return (
      this.active &&
      !this.signedOut &&
      !signedOut() &&
      !this.blocked &&
      this.observed &&
      this.current.session_closed_at === null &&
      !!this.current.plan &&
      !['completed', 'abandoned'].includes(this.current.plan.head.status)
    );
  }
  private persist(): boolean {
    const ok = storePlanDraft(this.scope, this.plan, this.draft);
    this.warning = !ok;
    return ok;
  }
  private emit(): void {
    this.publish({
      draft: copy(this.draft),
      current: copy(this.current),
      eligible: this.eligible,
      busy: this.busy || owned.has(writeKey(this.scope)),
      warning: this.warning,
      blocked: this.blocked,
      signedOut: this.signedOut,
      message: this.message,
      conflict: this.conflict,
      canContinue:
        this.unresolved &&
        !this.draft.unresolved &&
        this.unknownReadAt !== this.current.observed_at,
    });
  }
  observe(current: PlanObservation, observed: boolean): void {
    if (!this.active) return;
    const value = planObservation(current, this.scope);
    if (
      !value.plan ||
      value.plan.head.ref.plan_id !== this.plan ||
      value.plan.head.ref.version < this.current.plan!.head.ref.version
    )
      return;
    this.current = copy(value);
    this.observed = observed;
    const pending = this.draft.pending;
    if (
      pending?.state === 'acknowledged' &&
      sameRef(value.plan.head.ref, pending.receipt!.ref) &&
      JSON.stringify(value.plan.version.body) === JSON.stringify(pending.request.body) &&
      JSON.stringify(this.draft.body) === JSON.stringify(pending.request.body) &&
      this.draft.reason === pending.request.reason
    ) {
      const unresolved = this.draft.unresolved,
        retained = this.draft.retained;
      this.draft = this.fresh();
      this.draft.unresolved = unresolved;
      this.draft.retained = retained;
      if (unresolved || retained) this.persist();
      else this.warning = !clearDraft(this.scope, this.plan);
    } else if (
      !pending &&
      !this.draft.retained &&
      !this.draft.unresolved &&
      JSON.stringify(this.draft.body) === JSON.stringify(this.draft.baseline) &&
      this.draft.reason === 'User plan edit'
    )
      this.draft = this.fresh();
    this.emit();
  }
  edit(key: string, text: string): void {
    if (!this.eligible || this.waiting || this.unresolved) return;
    const field = textFields(this.draft.body).find((f) => f.key === key);
    if (!field || text.length > 2 * field.max) return;
    set(this.draft.body, field.path, text);
    this.persist();
    this.emit();
  }
  reason(text: string): void {
    if (!this.eligible || this.waiting || this.unresolved || text.length > 8192) return;
    this.draft.reason = text;
    this.persist();
    this.emit();
  }
  adopt(): void {
    if (!this.eligible || this.waiting || this.unresolved) return;
    if (!sameStructure(this.draft.baseline, this.current.plan!.version.body)) {
      this.message =
        'The plan structure changed. Load current text and copy the changes you want from the retained draft.';
      this.emit();
      return;
    }
    this.draft.base = copy(this.current.plan!.head.ref);
    this.draft.baseline = copy(this.current.plan!.version.body);
    this.draft.pending = null;
    this.conflict = null;
    this.persist();
    this.emit();
  }
  loadCurrent(): void {
    if (!this.eligible || this.waiting || this.unresolved) return;
    if (this.draft.retained) {
      this.message =
        'An earlier draft is already retained. Copy the text you need before leaving this draft; it will not be replaced silently.';
      this.emit();
      return;
    }
    const old = {
      base: copy(this.draft.base),
      baseline: copy(this.draft.baseline),
      body: copy(this.draft.body),
      reason: this.draft.reason,
    };
    const unresolved = this.draft.unresolved;
    this.draft = this.fresh();
    this.draft.retained = old;
    this.draft.unresolved = unresolved;
    this.conflict = null;
    this.persist();
    this.emit();
  }
  continueCurrent(): void {
    if (
      !this.eligible ||
      this.waiting ||
      !this.unresolved ||
      this.draft.unresolved ||
      this.unknownReadAt === this.current.observed_at
    )
      return;
    const unresolved = copy(this.draft.pending!.request);
    const old = {
      base: copy(this.draft.base),
      baseline: copy(this.draft.baseline),
      body: copy(this.draft.body),
      reason: this.draft.reason,
    };
    const retained = this.draft.retained ?? old;
    this.draft = this.fresh();
    this.draft.retained = retained;
    this.draft.unresolved = unresolved;
    this.conflict = null;
    this.message =
      'Started from current text. The earlier save is still unresolved and retained; no earlier failure or replay is assumed.';
    this.persist();
    this.emit();
  }
  async save(): Promise<void> {
    if (!this.eligible || this.waiting) return;
    const retry = this.unresolved;
    if (
      !retry &&
      (!sameRef(this.draft.base, this.current.plan!.head.ref) ||
        !sameStructure(this.draft.baseline, this.current.plan!.version.body))
    ) {
      this.message = 'The current plan changed. Review it before choosing a new base.';
      this.emit();
      return;
    }
    const problem = editProblem(this.draft.body, this.draft.reason);
    if (problem) {
      this.message = problem;
      this.emit();
      return;
    }
    const request = retry
      ? copy(this.draft.pending!.request)
      : {
          operation_id: `plan-edit-${crypto.randomUUID()}`,
          base: copy(this.draft.base),
          body: copy(this.draft.body),
          reason: this.draft.reason,
        };
    this.draft.pending = { request, state: 'unknown' };
    if (!this.persist()) {
      if (!retry) this.draft.pending = null;
      this.message =
        'Save was not sent because the retry could not be retained. Keep this tab open or copy your draft.';
      this.persist();
      this.emit();
      return;
    }
    this.unknownReadAt = this.current.observed_at;
    const epoch = this.epoch,
      controller = new AbortController();
    this.controller = controller;
    this.busy = true;
    this.message = '';
    this.emit();
    try {
      const ack = await this.transport(this.scope, request, controller.signal);
      if (!this.active || epoch !== this.epoch || signedOut() || controller.signal.aborted) return;
      this.draft.pending = {
        request,
        state: 'acknowledged',
        receipt: receipt(
          { project_id: this.scope.projectId, session_id: this.scope.sessionId, receipt: ack },
          this.scope,
          request,
        ),
      };
      this.message = `Saved version ${ack.ref.version}. Refreshing the current plan; this does not start work or approve it.`;
      this.persist();
      this.refresh();
    } catch (error) {
      if (!this.active || epoch !== this.epoch) return;
      const failure = error instanceof PlanWriteError ? error : new PlanWriteError(0);
      if (!retry && failure.definitive) {
        this.draft.pending = null;
        this.conflict = failure.current;
        this.message =
          failure.status === 409
            ? 'The plan changed before saving. Your draft is retained.'
            : 'The plan text was refused. Your draft is retained; check required text and distinct step titles.';
        this.persist();
        this.refresh();
      } else {
        this.message =
          'Save outcome unknown. Retry the same request or review a fresh current plan before explicitly continuing.';
        this.persist();
        this.refresh();
      }
      if (failure.status === 401) {
        this.signedOut = true;
        this.observed = false;
      }
    } finally {
      if (this.active && epoch === this.epoch) {
        this.busy = false;
        this.emit();
      }
    }
  }
  logout(): void {
    this.signedOut = true;
    this.observed = false;
    this.epoch++;
    this.controller?.abort();
    this.busy = false;
    this.emit();
  }
  stop(): void {
    this.active = false;
    this.epoch++;
    this.controller?.abort();
  }
}
