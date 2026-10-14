// SPDX-License-Identifier: MIT
/** Passive observations only; these helpers never start, reconnect or restart a kernel. */
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
export type KernelPhase =
  'loading' | 'observed' | 'paused' | 'unavailable' | 'unconfigured' | 'missing' | 'signed_out';
export interface KernelPollState {
  phase: KernelPhase;
  data: KernelObservation | null;
}
export class KernelReadError extends Error {
  constructor(readonly status: number) {
    super('Kernel observation unavailable');
  }
}
export const KERNEL_DEADLINE_MS = 8000;
export const KERNEL_INTERVAL_MS = 3000;
const identifier = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
function object(value: unknown, keys: string[]): value is Record<string, unknown> {
  return (
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}
function revision(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0);
}
export function validKernelScope(scope: KernelScope): boolean {
  return [scope.projectId, scope.sessionId, scope.environmentId].every(
    (id) => typeof id === 'string' && identifier.test(id),
  );
}
/** Only the exact loaded, open session may mount the poller. */
export function kernelPanelScope(
  project: string,
  session: string,
  detail: {
    project_id: string;
    session: { session_id: string; environment_id: string; closed_at: string | null };
  },
): KernelScope | null {
  const scope = {
    projectId: project,
    sessionId: session,
    environmentId: detail.session.environment_id,
  };
  return detail.project_id === project &&
    detail.session.session_id === session &&
    detail.session.closed_at === null &&
    validKernelScope(scope)
    ? scope
    : null;
}
function timestamp(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 64) return false;
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(
      value,
    );
  if (!match || !Number.isFinite(Date.parse(value))) return false;
  const [, year, month, day, hour, minute, second, zone] = match;
  const y = Number(year),
    m = Number(month),
    d = Number(day);
  const days = [
    31,
    y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0) ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  return (
    y > 0 &&
    m >= 1 &&
    m <= 12 &&
    d >= 1 &&
    d <= days[m - 1]! &&
    Number(hour) < 24 &&
    Number(minute) < 60 &&
    Number(second) < 60 &&
    (zone === 'Z' || (Number(zone?.slice(1, 3)) < 24 && Number(zone?.slice(4)) < 60))
  );
}
/** Reject incoherent or rounded pins instead of displaying guessed revision numbers. */
export function kernelObservation(value: unknown, scope: KernelScope): KernelObservation {
  const bad = () => new KernelReadError(0);
  if (
    !validKernelScope(scope) ||
    !object(value, [
      'project_id',
      'environment_id',
      'session_id',
      'current_revision',
      'kernels',
      'observed_at',
    ]) ||
    value.project_id !== scope.projectId ||
    value.environment_id !== scope.environmentId ||
    value.session_id !== scope.sessionId ||
    !revision(value.current_revision) ||
    !timestamp(value.observed_at) ||
    !Array.isArray(value.kernels) ||
    value.kernels.length > 2
  )
    throw bad();
  const languages = new Set<string>(),
    ids = new Set<string>();
  const kernels: KernelInfo[] = [];
  for (const kernel of value.kernels as unknown[]) {
    if (
      !object(kernel, [
        'kernel_id',
        'language',
        'generation',
        'start_revision',
        'current_revision',
        'revision_status',
      ]) ||
      typeof kernel.kernel_id !== 'string' ||
      !identifier.test(kernel.kernel_id) ||
      ids.has(kernel.kernel_id) ||
      (kernel.language !== 'python' && kernel.language !== 'sage') ||
      languages.has(kernel.language) ||
      typeof kernel.generation !== 'number' ||
      !Number.isSafeInteger(kernel.generation) ||
      kernel.generation < 1 ||
      !revision(kernel.start_revision) ||
      kernel.current_revision !== value.current_revision
    )
      throw bad();
    const start = kernel.start_revision,
      current = value.current_revision;
    if (start !== null && current !== null && start > current) throw bad();
    const status =
      start === null || current === null ? 'unknown' : start === current ? 'current' : 'predates';
    if (kernel.revision_status !== status) throw bad();
    languages.add(kernel.language);
    ids.add(kernel.kernel_id);
    kernels.push({
      kernel_id: kernel.kernel_id,
      language: kernel.language,
      generation: kernel.generation,
      start_revision: start,
      current_revision: current,
      revision_status: status,
    });
  }
  return {
    project_id: scope.projectId,
    environment_id: scope.environmentId,
    session_id: scope.sessionId,
    current_revision: value.current_revision,
    kernels,
    observed_at: value.observed_at,
  };
}

/** Own one actual request through settlement, even when an abort is ignored. */
export class KernelPoller {
  private visible = false;
  private visibilityKnown = false;
  private stopped = false;
  private epoch = 0;
  private busy = false;
  private failures = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private deadline: ReturnType<typeof setTimeout> | undefined;
  private controller: AbortController | undefined;
  private state: KernelPollState = { phase: 'loading', data: null };
  constructor(
    private readonly read: (signal: AbortSignal) => Promise<KernelObservation>,
    private readonly publish: (state: KernelPollState) => void,
  ) {}
  private emit(phase: KernelPhase, data = this.state.data): void {
    this.state = { phase, data };
    this.publish(this.state);
  }
  setVisible(visible: boolean): void {
    if (this.stopped || (this.visibilityKnown && this.visible === visible)) return;
    this.visibilityKnown = true;
    this.visible = visible;
    this.epoch++;
    clearTimeout(this.timer);
    clearTimeout(this.deadline);
    this.controller?.abort();
    if (visible) {
      this.emit('loading');
      void this.refresh();
    } else this.emit('paused');
  }
  logout(): void {
    this.stop();
    this.emit('signed_out', null);
  }
  stop(): void {
    this.stopped = true;
    this.epoch++;
    clearTimeout(this.timer);
    clearTimeout(this.deadline);
    this.controller?.abort();
  }
  private async refresh(): Promise<void> {
    if (this.stopped || !this.visible || this.busy) return;
    this.busy = true;
    const epoch = this.epoch,
      controller = new AbortController();
    this.controller = controller;
    let timedOut = false;
    this.deadline = setTimeout(() => {
      timedOut = true;
      controller.abort();
      if (!this.stopped && this.visible && epoch === this.epoch) this.emit('unavailable');
    }, KERNEL_DEADLINE_MS);
    try {
      const data = await this.read(controller.signal);
      if (!this.stopped && this.visible && epoch === this.epoch && !timedOut) {
        this.failures = 0;
        this.emit('observed', data);
      }
    } catch (error) {
      if (!this.stopped && this.visible && epoch === this.epoch) {
        const status = error instanceof KernelReadError ? error.status : 0;
        if (status === 401) {
          this.logout();
          return;
        }
        if (status === 404) {
          this.stop();
          this.emit('missing', null);
          return;
        }
        this.emit(status === 422 ? 'unconfigured' : 'unavailable');
        if (!timedOut) this.failures++;
      }
    } finally {
      clearTimeout(this.deadline);
      this.busy = false;
      if (timedOut) this.failures++;
      if (!this.stopped && this.visible) {
        const delay =
          epoch !== this.epoch
            ? 0
            : Math.min(30_000, KERNEL_INTERVAL_MS * 2 ** Math.min(this.failures, 4));
        this.timer = setTimeout(() => void this.refresh(), delay);
      }
    }
  }
}

const logoutListeners = new Set<() => void>();
let logoutStarted = false;
export function kernelReadsAllowed(): boolean {
  return !logoutStarted;
}
/** Narrow logout lifecycle for passive kernel reads; ordinary API defaults stay unchanged. */
export function onKernelLogout(listener: () => void): () => void {
  if (logoutStarted) {
    listener();
    return () => undefined;
  }
  logoutListeners.add(listener);
  return () => logoutListeners.delete(listener);
}
export function stopKernelReadsForLogout(): void {
  logoutStarted = true;
  for (const listener of logoutListeners) listener();
}
