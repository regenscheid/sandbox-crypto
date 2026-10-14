// SPDX-License-Identifier: MIT
/**
 * One polling loop for the panels that refresh what a page shows: kernels, plans, jobs,
 * provisioning and a session's runs. A poller reads only while its page is visible, never
 * overlaps its own reads, gives up on a read at its deadline, backs off after failures,
 * and stops for good on sign-out (401) or a missing scope (404).
 */
import { ApiError } from './http';
import { onSignOut } from './session';

export type PollPhase =
  | 'loading'
  | 'observed'
  | 'paused'
  | 'unavailable'
  | 'unsupported'
  | 'missing'
  | 'changed'
  | 'signed_out';

export interface PollState<T> {
  phase: PollPhase;
  data: T | null;
  /** A read is in flight. */
  busy: boolean;
}

export interface PollOptions<T> {
  /** Between the end of one read and the start of the next; failures double it up to 30 s. */
  intervalMs?: number;
  deadlineMs?: number;
  /** Read again after a successful read (default true). */
  repeat?: boolean;
  /** Stop after the first read, whatever its outcome. */
  once?: boolean;
  /** Stop after observing data for which this is true, a closed session say. */
  done?: (data: T) => boolean;
}

export const POLL_INTERVAL_MS = 3000;
export const POLL_DEADLINE_MS = 8000;
const MAX_BACKOFF_MS = 30_000;

/** The phase an ending status puts a poller in; other failures are 'unavailable'. */
const ENDING: Record<number, PollPhase> = { 401: 'signed_out', 404: 'missing', 409: 'changed' };

export function initialPoll<T>(): PollState<T> {
  return { phase: 'loading', data: null, busy: false };
}

export class Poller<T> {
  private visible = false;
  private visibilityKnown = false;
  private stopped = false;
  private epoch = 0;
  private busy = false;
  private failures = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private deadline: ReturnType<typeof setTimeout> | undefined;
  private controller: AbortController | undefined;
  private state: PollState<T> = initialPoll();

  constructor(
    private readonly read: (signal: AbortSignal) => Promise<T>,
    private readonly publish: (state: PollState<T>) => void,
    private readonly options: PollOptions<T> = {},
  ) {}

  private emit(phase: PollPhase, data = this.state.data): void {
    this.state = { phase, data, busy: this.busy && !this.stopped };
    this.publish(this.state);
  }

  private current(epoch: number): boolean {
    return !this.stopped && this.visible && epoch === this.epoch;
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

  stop(): void {
    this.stopped = true;
    this.epoch++;
    clearTimeout(this.timer);
    clearTimeout(this.deadline);
    this.controller?.abort();
  }

  logout(): void {
    this.stop();
    this.emit('signed_out', null);
  }

  /** Follow the document's visibility and the page's sign-out; returns the cleanup. */
  watch(): () => void {
    const visibility = () => this.setVisible(document.visibilityState === 'visible');
    const offSignOut = onSignOut(() => this.logout());
    document.addEventListener('visibilitychange', visibility);
    visibility();
    return () => {
      this.stop();
      offSignOut();
      document.removeEventListener('visibilitychange', visibility);
    };
  }

  private async refresh(): Promise<void> {
    if (this.stopped || !this.visible || this.busy) return;
    this.busy = true;
    this.emit(this.state.phase);
    const epoch = this.epoch,
      controller = new AbortController();
    this.controller = controller;
    let timedOut = false;
    this.deadline = setTimeout(() => {
      timedOut = true;
      controller.abort();
      if (this.current(epoch)) this.emit('unavailable');
    }, this.options.deadlineMs ?? POLL_DEADLINE_MS);
    try {
      const data = await this.read(controller.signal);
      if (this.current(epoch) && !timedOut) {
        this.failures = 0;
        this.emit('observed', data);
        if (this.options.done?.(data)) this.stop();
      }
    } catch (error) {
      if (this.current(epoch)) {
        const status = error instanceof ApiError ? error.status : 0;
        const ending = ENDING[status];
        if (ending !== undefined) {
          this.busy = false;
          if (ending === 'signed_out') this.logout();
          else {
            this.stop();
            this.emit(ending, ending === 'changed' ? this.state.data : null);
          }
          return;
        }
        this.emit(status === 422 ? 'unsupported' : 'unavailable');
        if (!timedOut) this.failures++;
      }
    } finally {
      clearTimeout(this.deadline);
      this.busy = false;
      if (timedOut) this.failures++;
      if (this.current(epoch)) this.emit(this.state.phase);
      if (this.options.once && !this.stopped) {
        this.stop();
        if (this.state.phase !== 'observed') this.emit('unavailable');
      }
      if (!this.stopped && this.visible && (epoch !== this.epoch || this.options.repeat !== false))
        this.timer = setTimeout(
          () => void this.refresh(),
          epoch !== this.epoch
            ? 0
            : Math.min(
                MAX_BACKOFF_MS,
                (this.options.intervalMs ?? POLL_INTERVAL_MS) * 2 ** Math.min(this.failures, 4),
              ),
        );
    }
  }
}
