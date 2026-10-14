// SPDX-License-Identifier: MIT
/**
 * The reviewer model's repairs, asked of the workspace server (mathrepair.py). Browser
 * only: the server-side math checker (mathcheck.ts) bundles the pure repairs
 * (texRepairs.ts, texDefinitions.ts) and must not carry this client or the API it calls.
 */
import type { MathRepairAnswer, MathRepairRequest } from './api';
import { Kept } from './kept';
import { describeError } from './mathErrors';
import type { ModelRepair } from './mathrepair';

/** The longest expression the server repairs (mathrepair.py, `MAX_TEX`). */
export const MAX_MODEL_TEX = 4_000;

export type Transport = (request: MathRepairRequest) => Promise<MathRepairAnswer>;

/** How a page asks for model repairs; the defaults suit a browser. */
export interface ModelRepairsOptions {
  /** Requests at once, once the server has said it has a model; one until then. */
  maxActive?: number;
  /** Runs `callback` before the next frame: listeners hear of answers once a frame. */
  nextFrame?: (callback: () => void) => void;
  /** Runs `callback` after `ms` milliseconds; returns a function that cancels it. */
  later?: (callback: () => void, ms: number) => () => void;
  /** The waits before each new try of a request the server was too busy for. */
  busyRetries?: readonly number[];
}

/**
 * Before the next frame; within a second where frames do not come, as in a hidden tab,
 * which runs none (the text is then rendered again at most once a second).
 */
function nextFrame(callback: () => void): void {
  if (typeof requestAnimationFrame !== 'function') {
    setTimeout(callback, 0);
    return;
  }
  let done = false;
  const run = (): void => {
    if (done) return;
    done = true;
    callback();
  };
  const timer = setTimeout(run, 1_000);
  requestAnimationFrame(() => {
    clearTimeout(timer);
    run();
  });
}

function later(callback: () => void, ms: number): () => void {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
}

interface Job {
  key: string;
  request: MathRepairRequest;
  check: (tex: string) => void;
  /** Tries the server was too busy for. */
  busy: number;
}

/** A job's next try, while it waits for it. */
interface Retry {
  cancel: () => void;
}

/** The repairs one page keeps: within a count, and a size in characters. */
const MAX_KEPT_REPAIRS = 2_000;
const MAX_KEPT_REPAIR_WEIGHT = 2_000_000;

function repairWeight(key: string, repair: ModelRepair): number {
  return key.length + (repair.tex === null ? repair.reason.length : repair.tex.length);
}

/**
 * The reviewer model's repairs for one page: asked for in the background, once per
 * expression, and remembered until the page reloads, the least recently used dropped
 * first beyond `MAX_KEPT_REPAIRS` (the server keeps its answers too, so asking again for
 * one dropped costs no model call).
 *
 * Until the server first says it has a model, one request is in flight at a time; then
 * two. When it says it has none, or the session ends or reaches its limit, the requests
 * still waiting are dropped at once, and so is any answered later as `busy`. A request
 * the server was too busy for is tried again later, a few times. Listeners hear of the
 * expressions whose answers arrived once a frame, so that the texts showing them render
 * again at most once a frame.
 */
export class ModelRepairs {
  readonly #cache = new Kept<ModelRepair>(MAX_KEPT_REPAIRS, MAX_KEPT_REPAIR_WEIGHT, repairWeight);
  /** Expressions asked for and not answered: waiting, in flight, or waiting to try again. */
  readonly #pending = new Set<string>();
  readonly #queue: Job[] = [];
  readonly #retries = new Map<string, Retry>();
  readonly #listeners = new Set<(keys: ReadonlySet<string>) => void>();
  readonly #arrived = new Set<string>();
  readonly #idle: (() => void)[] = [];
  readonly #maxActive: number;
  readonly #nextFrame: (callback: () => void) => void;
  readonly #later: (callback: () => void, ms: number) => () => void;
  readonly #busyRetries: readonly number[];
  #active = 0;
  #pumping = false;
  #telling = false;
  /** Whether the server has said it has a model. */
  #enabled = false;
  /** Why the page asks no more: the server has no model, the session ended, or its limit. */
  #off: string | null = null;

  constructor(
    private readonly transport: Transport | null,
    options: ModelRepairsOptions = {},
  ) {
    this.#maxActive = options.maxActive ?? 2;
    this.#nextFrame = options.nextFrame ?? nextFrame;
    this.#later = options.later ?? later;
    this.#busyRetries = options.busyRetries ?? [2_000, 4_000, 8_000];
  }

  /** Why no repairs are asked for, or null while they are. */
  get unavailable(): string | null {
    if (this.transport === null) return 'model repairs are not available here';
    return this.#off;
  }

  get(key: string): ModelRepair | undefined {
    return this.#cache.get(key);
  }

  /** Remember a repair, as if the model had answered it. */
  set(key: string, repair: ModelRepair): void {
    this.#cache.set(key, repair);
  }

  isPending(key: string): boolean {
    return this.#pending.has(key);
  }

  /** Ask for a repair of the expression under `key`, unless it is known or asked for. */
  request(key: string, request: MathRepairRequest, check: (tex: string) => void): void {
    if (this.transport === null || this.#off !== null) return;
    if (this.#cache.has(key) || this.#pending.has(key)) return;
    if (request.tex.length > MAX_MODEL_TEX) {
      this.#cache.set(key, { tex: null, reason: 'too long to ask the model for a repair' });
      return;
    }
    this.#pending.add(key);
    this.#queue.push({ key, request, check, busy: 0 });
    // Requests start after the rendering that asks for them.
    if (!this.#pumping) {
      this.#pumping = true;
      queueMicrotask(() => {
        this.#pumping = false;
        this.#pump();
      });
    }
  }

  #pump(): void {
    const transport = this.transport;
    if (transport === null) return;
    // One at a time until the server says it has a model: with none, its first answer
    // turns repairs off, and the rest are never sent.
    const limit = this.#enabled ? this.#maxActive : 1;
    while (this.#off === null && this.#active < limit) {
      const job = this.#queue.shift();
      if (job === undefined) break;
      this.#active += 1;
      void this.#ask(transport, job);
    }
  }

  async #ask(transport: Transport, job: Job): Promise<void> {
    let outcome: ModelRepair | 'busy' | 'off';
    try {
      outcome = this.#accept(await transport(job.request), job.check);
    } catch (error) {
      outcome = this.#failed(error);
    }
    this.#active -= 1;
    // A `busy` that arrives after another answer turned repairs off is not tried again.
    if (outcome === 'busy' && this.#off === null) this.#tryLater(job);
    else this.#settle(job.key, outcome === 'off' || outcome === 'busy' ? null : outcome);
    this.#pump();
  }

  #accept(answer: MathRepairAnswer, check: (tex: string) => void): ModelRepair | 'busy' | 'off' {
    if (!answer.enabled) {
      this.#turnOff(answer.reason || 'model repairs are off');
      return 'off';
    }
    this.#enabled = true;
    const candidate = typeof answer.tex === 'string' ? answer.tex.trim() : '';
    if (!candidate && answer.reason === 'busy') return 'busy';
    if (!candidate) return { tex: null, reason: answer.reason || 'no repair' };
    try {
      check(candidate);
    } catch (error) {
      return { tex: null, reason: `the repair does not render either: ${describeError(error)}` };
    }
    return { tex: candidate, model: answer.model ?? null };
  }

  #failed(error: unknown): ModelRepair | 'off' {
    const status = (error as { status?: unknown } | null)?.status;
    // Logged out, or this session's limit reached: asking again would fail the same way.
    if (status === 401 || status === 429) {
      this.#turnOff(describeError(error));
      return 'off';
    }
    return { tex: null, reason: describeError(error) };
  }

  /** Ask again after a wait, while the server is busy; after the last, give up. */
  #tryLater(job: Job): void {
    const wait = this.#busyRetries[job.busy];
    if (wait === undefined) {
      this.#settle(job.key, { tex: null, reason: 'the server stayed busy' });
      return;
    }
    job.busy += 1;
    const retry: Retry = { cancel: () => undefined };
    this.#retries.set(job.key, retry);
    retry.cancel = this.#later(() => {
      // Cancelled, or settled since: as when repairs went off.
      if (this.#retries.get(job.key) !== retry) return;
      this.#retries.delete(job.key);
      if (this.#off !== null) {
        this.#settle(job.key, null);
        return;
      }
      this.#queue.push(job);
      this.#pump();
    }, wait);
  }

  /** Repairs are off for this page: drop what waits, at once, and tell the listeners once. */
  #turnOff(reason: string): void {
    if (this.#off !== null) return;
    this.#off = reason;
    for (const job of this.#queue.splice(0)) this.#settle(job.key, null);
    const retries = [...this.#retries];
    this.#retries.clear();
    for (const [key, retry] of retries) {
      retry.cancel();
      this.#settle(key, null);
    }
  }

  /** `key` answered (`repair`, or nothing to keep); the listeners hear of it next frame. */
  #settle(key: string, repair: ModelRepair | null): void {
    if (repair !== null) this.#cache.set(key, repair);
    this.#pending.delete(key);
    this.#arrived.add(key);
    if (!this.#telling) {
      this.#telling = true;
      this.#nextFrame(() => this.#tell());
    }
  }

  #tell(): void {
    this.#telling = false;
    const keys: ReadonlySet<string> = new Set(this.#arrived);
    this.#arrived.clear();
    for (const listener of [...this.#listeners]) listener(keys);
    if (this.#pending.size === 0 && !this.#telling) {
      for (const resolve of this.#idle.splice(0)) resolve();
    }
  }

  /** Call `listener`, at most once a frame, with the expressions whose answers arrived. */
  subscribe(listener: (keys: ReadonlySet<string>) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  /** Every request asked for so far answered, and the listeners told. */
  settled(): Promise<void> {
    if (this.#pending.size === 0 && !this.#telling) return Promise.resolve();
    return new Promise((resolve) => this.#idle.push(resolve));
  }
}
