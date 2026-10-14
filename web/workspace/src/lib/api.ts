// SPDX-License-Identifier: MIT
/**
 * The workspace API, version 0 (packages/workspace, api.py and documents.py): read-only
 * views of the harness's projects, sessions and runs and of the project's living
 * documents, live job status and cancellation, and repairs of LaTeX by the reviewer model
 * (mathrepair.py). Requests go through http.ts. The session cookie travels with every
 * request, and the browser adds the `Origin` header the server requires of a POST.
 */
import type {
  ProjectAnswer,
  ProjectListing,
  ProjectView,
  RunEntry,
  SessionDetail,
  SessionView,
} from './api-types';
import type { ServerBlock } from './attach';
import { ApiError, get, post } from './http';
import { signOut } from './session';

export { ApiError };

/** A project in the list; a project whose record is damaged has only its ID and error. */
export type ProjectSummary = ProjectListing['projects'][number];
export type Session = SessionView;
export type Project = ProjectView;
export type { RunEntry, SessionDetail };

export interface MacroContext {
  project: string;
  revision: number;
  sha256: string;
  macros: Record<string, string>;
  known?: boolean;
  key?: string;
  problems?: string[];
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
  /** Fixed harness phase; older records omit it. No exception or provider text. */
  failure?: 'model' | 'tool' | 'harness' | 'cleanup' | null;
  final_text: string | null;
  math_summary?: {
    pending?: boolean;
    renderer?: string;
    counts?: Record<string, number>;
    detail?: string;
  };
  review_summary?: {
    pending?: boolean;
    outcome?: string;
    counts?: Record<string, number>;
    reviews?: string[];
  };
  messages: Message[];
  tools: ToolRecord[];
}

/** One expression KaTeX rejected, as the page asks for its repair. */
export interface MathRepairRequest {
  tex: string;
  display: boolean;
  /** KaTeX's error. */
  error: string;
  project?: string;
  macro_context?: string;
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

/** Who wrote a revision (living documents, LD-10): a user, an agent run, worker code,
 * an import or the platform, with what identifies each. */
export interface Author {
  kind: 'user' | 'agent' | 'worker' | 'import' | 'platform';
  user?: string;
  session_id?: string;
  run_id?: string;
  role?: string;
  model?: string;
  alias?: string;
  tool_call?: string;
  import_id?: string;
  service?: string;
  worker_binding?: string;
  environment_id?: string;
  incarnation?: string;
  execution_id?: string;
  attempt_id?: string;
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
  macro_context?: MacroContext;
  /** False when the server could not parse the revision as its block map was made. */
  matched: boolean;
  blocks: (ServerBlock & {
    parent: string | null;
    structural: string | null;
    attrs: Record<string, Json>;
    source?: string | null;
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
  operations?: Json[];
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

export interface DocumentChange {
  outcome: string;
  document_id: string;
  revision: number;
  patch_id: string | null;
  message: string;
  conflicts: {
    block?: string;
    math?: string;
    now?: string;
    at_base?: string;
    changed_by?: { revision: number; author: Author }[];
  }[];
  unnamed: string[];
}

export interface PatchPreview {
  patch: PatchRecord;
  base: number;
  current: string;
  proposed: string | null;
  allowed: boolean;
  message?: string | null;
  unnamed: string[];
}

/** Immutable anchor context, projected onto the selected document revision. */
export interface CommentAnchor {
  revision: number;
  block: string;
  math: string | null;
  start: number | null;
  end: number | null;
  kind: 'block' | 'math' | 'range';
  quote: string;
  quote_truncated: boolean;
  before: string;
  after: string;
}
export interface CommentBodySource {
  entry_id: string;
  revision?: number;
  sha256?: string;
  body?: string;
  author?: Author | null;
  original_author?: Author;
  held?: string;
}
export interface CommentEntry {
  entry_id: string;
  number: number;
  kind: 'create' | 'reply' | 'edit' | 'edit_body' | 'resolve' | 'reopen';
  author: Author;
  body: string;
  created_at: number;
  body_source?: CommentBodySource;
  payload: {
    revision?: number;
    changed_blocks?: string[];
    outside_anchor?: string[];
    changed_blocks_total?: number;
    changed_blocks_truncated?: boolean;
    outside_anchor_total?: number;
    outside_anchor_truncated?: boolean;
    addressed_to?: string;
    request_id?: string;
    anchor?: Record<string, unknown>;
    entry?: string;
    base?: number;
    sha256?: string;
    body_revision?: number;
  };
}
export interface CommentThread {
  thread_id: string;
  anchor: CommentAnchor;
  opener: Author;
  created_at: number;
  revision: number;
  status: 'open' | 'resolved';
  entries_before: number | null;
  entries: CommentEntry[];
  requests: {
    request_id: string;
    session_id: string;
    entry_id: string;
    state: 'queued' | 'delivered' | 'responded';
    delivered_run: string | null;
    responded_entry: string | null;
  }[];
  view_revision: number;
  attachment: {
    status: 'current' | 'moved' | 'stale' | 'orphaned';
    quote: string;
    current: string | null;
  };
}
export interface CommentListing {
  threads: CommentThread[];
  next_after: string | null;
}
export interface CommentChange {
  thread_id: string;
  entry_id: string;
  revision: number;
  request_id: string | null;
  outcome: string;
  replayed?: boolean;
}

const part = encodeURIComponent;

export const api = {
  projectMacros: (project: string) => get<MacroContext>(`/projects/${part(project)}/macros`),
  changeMacros: (project: string, value: Record<string, unknown>) =>
    post<MacroContext>(`/projects/${part(project)}/macros`, value),
  projects: () => get<ProjectListing>('/projects'),
  project: (project: string) =>
    get<ProjectAnswer>(`/projects/${part(project)}`).then((found) => found.project),
  session: (project: string, session: string, signal?: AbortSignal) =>
    get<SessionDetail>(`/projects/${part(project)}/sessions/${part(session)}`, { signal }),
  jobs: (project: string, session: string, signal?: AbortSignal) =>
    get<JobListing>(`/projects/${part(project)}/sessions/${part(session)}/jobs`, { signal }),
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
  changeDocument: (project: string, document: string, change: Record<string, unknown>) =>
    post<DocumentChange>(`/projects/${part(project)}/documents/${part(document)}/changes`, change),
  comments: (project: string, document: string, revision: number, after: string | null = null) =>
    get<CommentListing>(
      `/projects/${part(project)}/documents/${part(document)}/comments?revision=${revision}` +
        (after === null ? '' : `&after=${part(after)}`),
    ),
  commentThread: (
    project: string,
    document: string,
    thread: string,
    before: number | null = null,
  ) =>
    get<CommentThread>(
      `/projects/${part(project)}/documents/${part(document)}/comments/${part(thread)}` +
        (before === null ? '' : `?before=${before}`),
    ),
  changeComment: (project: string, document: string, change: Record<string, unknown>) =>
    post<CommentChange>(`/projects/${part(project)}/documents/${part(document)}/comments`, change),
  previewPatch: (project: string, document: string, patch: string) =>
    get<PatchPreview>(
      `/projects/${part(project)}/documents/${part(document)}/patches/${part(patch)}/preview`,
    ),
  history: (project: string, document: string) =>
    get<DocumentHistory>(`/projects/${part(project)}/documents/${part(document)}/history`),
  mathRepair: (request: MathRepairRequest) => post<MathRepairAnswer>('/math-repair', request),
  /** Ends the page's session at once, even when the server cannot be told. */
  async logout(): Promise<void> {
    signOut();
    const response = await fetch('/api/v0/logout', { method: 'POST' });
    if (!response.ok && response.status !== 401) {
      throw new ApiError(response.status, 'unavailable', 'logging out failed');
    }
  },
};
