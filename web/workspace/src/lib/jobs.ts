// SPDX-License-Identifier: MIT
/** Job output is plain untrusted text. It never enters Markdown or HTML rendering. */
import { api, type BackgroundJob, type JobListing, type JobManifestView } from './api';
import { guardedWrite } from './mutation';
import { Poller, type PollState } from './poller';

export const finishedStates = new Set(['succeeded', 'failed', 'timed_out', 'cancelled', 'lost']);

export function jobElapsed(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${value % 60}s`;
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
}

/**
 * The session's job listing, polled. The jobs route passes the worker's refusals
 * through, such as a 404 for an environment the worker has not made yet or a cursor
 * that a new job moved, so only a 401 ends the poller; every other failure is retried.
 */
export function jobsPoller(
  project: string,
  session: string,
  publish: (state: PollState<JobListing>) => void,
): Poller<JobListing> {
  return new Poller((signal) => api.jobs(project, session, signal), publish, { endOn: {} });
}

export interface CancelState {
  /** The jobs asked to cancel and not yet answered, in the order asked. */
  queued: string[];
  /** The last cancellation's failure, cleared when the queue starts again. */
  error: unknown;
}

/**
 * The jobs the user asked to cancel, cancelled one at a time in the order asked. A job
 * asked for while another is being cancelled waits its turn instead of being dropped, and
 * one failure does not stop the others. The view makes the state with `$state`.
 */
export class JobCancels {
  #running = false;

  constructor(
    readonly state: CancelState,
    private readonly cancel: (job: string) => Promise<void>,
  ) {}

  async ask(job: string): Promise<void> {
    if (this.state.queued.includes(job)) return;
    this.state.queued = [...this.state.queued, job];
    if (this.#running) return;
    this.#running = true;
    this.state.error = null;
    try {
      while (this.state.queued.length > 0) {
        try {
          await this.cancel(this.state.queued[0]!);
        } catch (caught) {
          this.state.error = caught;
        }
        this.state.queued = this.state.queued.slice(1);
      }
    } finally {
      this.#running = false;
    }
  }
}

/** What a job sandbox's phase means to the user (JS-4); platform words, never agent text. */
const PHASE_TEXT: Record<string, string> = {
  creating: 'creating: not placed yet',
  preparing: 'preparing: packages and inputs',
  running: 'running',
  collecting: 'collecting: copying files back',
  released: 'released: deleted',
  retained: 'retained: stopped, disk kept',
};

export function phaseText(phase: string): string {
  return PHASE_TEXT[phase] ?? phase;
}

/** Reserved CPU, from the profile's millicores: "12 cores", "0.5 cores". */
export function cpuText(millicores: number): string {
  const cores = millicores / 1000;
  return `${Number.isInteger(cores) ? cores : cores.toFixed(1)} ${cores === 1 ? 'core' : 'cores'}`;
}

/** Reserved memory, from MiB: "32 GiB", "512 MiB". */
export function memoryText(mib: number): string {
  if (mib < 1024) return `${mib} MiB`;
  const gib = mib / 1024;
  return `${Number.isInteger(gib) ? gib : gib.toFixed(1)} GiB`;
}

/** A deadline in UTC, the same wherever the page is read: "2026-10-28 09:30 UTC". */
export function deadlineText(iso: string): string {
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return iso;
  return `${new Date(time).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

/** The profile and phase of a sandbox job for the list line, or null for any other job. */
export function sandboxTag(job: BackgroundJob): string | null {
  return job.sandbox ? `${job.sandbox.profile} · ${job.sandbox.phase}` : null;
}

/** The collection's state, with the files that came back once the manifest says. */
export function collectionText(job: BackgroundJob): string {
  const state = job.sandbox?.collection ?? 'pending';
  if (!job.manifest) return state;
  const n = job.manifest.collected;
  return `${state} · ${n} ${n === 1 ? 'file' : 'files'}`;
}

/** The immutable manifest in a line: why it ended and what came back (JS-16). */
export function manifestText(manifest: JobManifestView): string {
  const packages = manifest.replayed_packages;
  return [
    manifest.reason ? `ended ${manifest.reason}` : 'no failure reason',
    `${manifest.collected} collected`,
    `${manifest.not_collected_total} not collected`,
    `${packages} replayed ${packages === 1 ? 'package' : 'packages'}`,
    `isolation tier ${manifest.isolation_tier ?? 'unknown'}`,
  ].join(' · ');
}

export function canRelease(job: BackgroundJob): boolean {
  return job.sandbox?.phase === 'retained';
}

export const RELEASE_QUESTION =
  'Release this job sandbox now? Its disk is deleted for good, with everything the job wrote ' +
  'that was not collected. Files already collected into the session stay.';

/**
 * "Release now" for a retained job sandbox, once the user confirms. The operation ID is
 * kept until a result, so a retry after a lost answer is the same request. Resolves with
 * whether the release was sent; a failure is thrown for the view to show.
 */
export async function releaseNow(
  scope: string,
  job: BackgroundJob,
  ask: (question: string) => boolean,
  send: (body: { operation_id: string }) => Promise<unknown>,
): Promise<boolean> {
  if (!canRelease(job) || !ask(RELEASE_QUESTION)) return false;
  await guardedWrite('job-sandbox-release', `${scope}:${job.job_id}`, {}, send);
  return true;
}
