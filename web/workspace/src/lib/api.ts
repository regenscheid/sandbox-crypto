// SPDX-License-Identifier: MIT
/**
 * The workspace API, version 0 (packages/workspace, api.py and documents.py): read-only
 * views of the harness's projects, sessions and runs and of the project's living
 * documents, live job status and cancellation, and repairs of LaTeX by the reviewer model (mathrepair.py). The session
 * cookie travels with every request, and the browser adds the `Origin` header the server
 * requires of a POST; there is nothing else to send.
 */
import type { ServerBlock } from './attach';

export interface ProjectSummary {
  project_id: string;
  profile?: string;
  policy?: string;
  default_environment_id?: string;
  created_at?: string;
  updated_at?: string;
  sessions?: number;
  /** Set instead of the rest when the project's record is damaged. */
  error?: string;
}

export interface Session {
  session_id: string;
  environment_id: string;
  dedicated: boolean;
  opened_at: string;
  closed_at: string | null;
}

export interface Project {
  project_id: string;
  profile: string;
  policy: string;
  default_environment_id: string;
  created_at: string;
  updated_at: string;
  sessions: (Session & { runs: number })[];
  environments: { environment_id: string; default: boolean; sessions: string[] }[];
}

export interface RunEntry {
  name: string;
  run_id: string;
  started_at: string;
}

export interface SessionDetail {
  project_id: string;
  session: Session;
  runs: RunEntry[];
}

export interface BackgroundJob {
  job_id: string;
  state: string;
  purpose: string;
  expected_progress: string;
  elapsed_seconds: number;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  cpu_seconds: number | null;
  memory_bytes: number | null;
  logs: { stdout: string; stderr: string };
  worker_restart: string;
  error?: string | null;
  checkin?: JobCheckin | null;
}

export interface JobCheckin {
  job_id: string;
  alias: string;
  model: string | null;
  at: string;
  state: 'fine' | 'needs_attention';
  note: string;
}

export interface JobWait {
  run_id: string;
  state: 'waiting';
  job_ids: string[];
  mode: 'all' | 'first';
  started_at: string;
  watcher: string | null;
  checkins: JobCheckin[];
  connected: boolean;
}

export interface JobListing {
  enabled: boolean;
  jobs: BackgroundJob[];
  truncated: boolean;
  waits: JobWait[];
}

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

export interface ToolCallMessage {
  id: string;
  type: string;
  function: { name: string; arguments: string };
}

export interface Message {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content?: string | null;
  tool_calls?: ToolCallMessage[];
  tool_call_id?: string;
}

/** A tool call's record: its name and arguments, and whatever its outcome recorded. */
export interface ToolRecord {
  tool: string;
  arguments: string;
  replayed?: boolean;
  error?: string;
  [key: string]: Json | undefined;
}

export interface RunRecord {
  run_id: string;
  context: {
    project_id: string;
    session_id: string;
    environment_id: string;
    policy_revision: string | null;
    actor: string;
  };
  task: string;
  started_at: string;
  ended_at: string;
  stopped: string;
  final_text: string | null;
  messages: Message[];
  tools: ToolRecord[];
}

/** One expression KaTeX rejected, as the page asks for its repair. */
export interface MathRepairRequest {
  tex: string;
  display: boolean;
  /** KaTeX's error. */
  error: string;
}

/** The server's answer: `enabled` false when it has no reviewer model to ask. */
export interface MathRepairAnswer {
  enabled: boolean;
  tex?: string | null;
  /** The reviewer model's alias. */
  model?: string | null;
  /** Why there is no repair. */
  reason?: string | null;
  cached?: boolean;
}

/** Who wrote a revision (living documents, LD-10): a user, an agent run, an import or
 * the platform, with what identifies each. */
export interface Author {
  kind: 'user' | 'agent' | 'import' | 'platform';
  user?: string;
  session_id?: string;
  run_id?: string;
  role?: string;
  model?: string;
  alias?: string;
  tool_call?: string;
  import_id?: string;
  service?: string;
}

export interface DocumentInfo {
  document_id: string;
  project_id: string;
  scope: 'project' | 'session';
  session_id: string | null;
  name: string;
  title: string;
  created_at: string;
  created_by: Author;
  created_session: string | null;
  takes_suggestions: boolean;
  /** The current revision. */
  head: number;
  deleted: boolean;
}

/** A warning on a revision's math or front matter (LD-30). */
export interface DocumentWarning {
  kind: string;
  block?: string;
  math?: string;
  message: string;
}

export interface RevisionInfo {
  document_id: string;
  number: number;
  created_at: string;
  author: Author;
  accepted_by: Author | null;
  session_id: string | null;
  base: number | null;
  parent: number | null;
  reason: string;
  patch_id: string | null;
  tombstone: boolean;
  normalized: { converted?: Record<string, number>; kept?: { tex: string; why: string }[] };
  warnings: DocumentWarning[];
  parser: Record<string, string>;
}

/** A revision as the document view shows it. */
export interface DocumentRevision {
  document: DocumentInfo;
  revision: RevisionInfo;
  text: string;
  /** The front matter's title and macros, as the server read them. */
  title: string | null;
  macros: Record<string, string>;
  /** False when the server could not parse the revision as its block map was made. */
  matched: boolean;
  blocks: (ServerBlock & {
    parent: string | null;
    structural: string | null;
    attrs: Record<string, Json>;
  })[];
  removed: { id: string; kind: string }[];
  removed_math: { id: string; block: string }[];
}

export interface HistoryRevision {
  number: number;
  created_at: string;
  author: Author;
  accepted_by: Author | null;
  reason: string;
  base: number | null;
  parent: number | null;
  patch_id: string | null;
  tombstone: boolean;
  normalized: boolean;
  warnings: number;
  changed: string[];
  added: string[];
  moved: string[];
  removed: string[];
}

export interface PatchRecord {
  patch_id: string;
  created_at: string;
  author: Author;
  base: number;
  reason: string;
  state: string;
  revision?: number | null;
  decided_at?: string | null;
  decided_by?: Author | null;
}

/** Who changed what (LD-11). */
export interface DocumentHistory {
  document: DocumentInfo;
  revisions: HistoryRevision[];
  /** For each block, the revisions that changed it. */
  blocks: Record<string, { revision: number; status: string; author: Author }[]>;
  patches: PatchRecord[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly kind: string,
    message: string,
  ) {
    super(message);
  }

  get loggedOut(): boolean {
    return this.status === 401;
  }
}

async function failure(response: Response): Promise<ApiError> {
  let kind = 'unavailable';
  let message = `the server answered ${response.status}`;
  try {
    const body = (await response.json()) as { error?: { kind?: string; message?: string } };
    kind = body.error?.kind ?? kind;
    message = body.error?.message ?? message;
  } catch {
    // Not JSON: keep the status.
  }
  return new ApiError(response.status, kind, message);
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`/api/v0${path}`, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`/api/v0${path}`, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

const part = encodeURIComponent;

export const api = {
  projects: () => get<{ projects: ProjectSummary[] }>('/projects'),
  project: (project: string) =>
    get<{ project: Project }>(`/projects/${part(project)}`).then((found) => found.project),
  session: (project: string, session: string) =>
    get<SessionDetail>(`/projects/${part(project)}/sessions/${part(session)}`),
  jobs: (project: string, session: string) =>
    get<JobListing>(`/projects/${part(project)}/sessions/${part(session)}/jobs`),
  cancelJob: (project: string, session: string, job: string) =>
    post(`/projects/${part(project)}/sessions/${part(session)}/jobs/${part(job)}/cancel`, {}),
  run: (project: string, session: string, run: string) =>
    get<RunRecord>(`/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}`),
  documents: (project: string) =>
    get<{ documents: DocumentInfo[] }>(`/projects/${part(project)}/documents`).then(
      (found) => found.documents,
    ),
  document: (project: string, document: string, revision: number | null = null) =>
    get<DocumentRevision>(
      `/projects/${part(project)}/documents/${part(document)}` +
        (revision === null ? '' : `?revision=${revision}`),
    ),
  history: (project: string, document: string) =>
    get<DocumentHistory>(`/projects/${part(project)}/documents/${part(document)}/history`),
  mathRepair: (request: MathRepairRequest) => post<MathRepairAnswer>('/math-repair', request),
  async logout(): Promise<void> {
    const response = await fetch('/api/v0/logout', { method: 'POST' });
    if (!response.ok && response.status !== 401) {
      throw new ApiError(response.status, 'unavailable', 'logging out failed');
    }
  },
};
