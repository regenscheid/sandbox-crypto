// SPDX-License-Identifier: MIT
/** The workspace job client, through the shared session transport. */
import type { JobListing, JobCancellation } from './api-types';
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
};
