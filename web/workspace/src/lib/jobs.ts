// SPDX-License-Identifier: MIT
/** Job output is plain untrusted text. It never enters Markdown or HTML rendering. */
import { api, type JobListing } from './api';
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
