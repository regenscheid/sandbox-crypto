// SPDX-License-Identifier: MIT
/** The workspace job client, through the shared session transport. */
import type { JobListing, JobCancellation, JobRelease } from './api-types';
import { get, post } from './http';

const part = encodeURIComponent;

export const jobApi = {
  jobs: (project: string, session: string, signal?: AbortSignal) =>
    get<JobListing>(`/projects/${part(project)}/sessions/${part(session)}/jobs`, { signal }),
  cancelJob: (project: string, session: string, job: string) =>
    post<JobCancellation>(
      `/projects/${part(project)}/sessions/${part(session)}/jobs/${part(job)}/cancel`,
      {},
    ),
  /** The user's "release now" for a retained job sandbox; `operation_id` is its receipt. */
  releaseJobSandbox: (
    project: string,
    session: string,
    job: string,
    body: { operation_id: string },
  ) =>
    post<JobRelease>(
      `/projects/${part(project)}/sessions/${part(session)}/jobs/${part(job)}/release`,
      body,
    ),
};
