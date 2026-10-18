// SPDX-License-Identifier: MIT
/**
 * The workspace API, version 0 (packages/workspace, api.py and documents.py): read-only
 * views of the harness's projects, sessions and runs and of the project's living
 * documents, live job status and cancellation, and repairs of LaTeX by the reviewer model
 * (mathrepair.py). Requests go through http.ts. The session cookie travels with every
 * request, and the browser adds the `Origin` header the server requires of a POST.
 */
import type {
  ChartLink,
  FigureLink,
  ArtifactListing,
  Category,
  FoldProposalList,
  FrontierAnswer,
  FrontierCategories,
  FrontierCategory,
  FrontierHistoryAnswer,
  FrontierPastAnswer,
  LedgerCitations,
  LedgerEntry,
  LedgerGate,
  LedgerHistory,
  LedgerListing,
  LedgerWork,
  MemoryAnswer,
  MemoryChange,
  MemoryHistory,
  MemoryProposals,
  McpInventory,
  ProjectAnswer,
  ProjectListing,
  ProjectSkills,
  ProjectView,
  ProposalView,
  RunEntry,
  RunTree,
  SessionDetail,
  SessionView,
  SkillSettingsChange,
  SkillSettingsView,
  ViewerLink,
} from './api-types';
import type { ArtifactFilter, ArtifactGroup, FileSettings } from './artifactFiles';
import { artifactQuery, NO_FILTER } from './artifactFiles';
import type { ServerBlock } from './attach';
import type { CategoryBody } from './frontierForm';
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
  /** Exact historical collections are paged separately when inline snapshots reach their bound. */
  stored_history?: {
    format: 'run-history-v1';
    project: string;
    session: string;
    run: string;
    collections?: Record<string, number>;
  } | null;
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
  /** The run's last answer: the text its last closing accepted. */
  final_text: string | null;
  /** The run's answer to the user: the text of the turn that answered, before any closing
   * rounds such as responses to review findings. Older records omit it. */
  answer?: string | null;
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
  /** Each answered model call's usage, in order; older records have none. */
  usage?: ModelUsage[];
  /** The run's hooks' summaries by hook name, such as `skills` (its discoveries and
   * start loads); older records have none. */
  summaries?: Record<string, Json>;
  /** The notice IDs a model call saw, by hook name. */
  delivered?: Record<string, Json>;
  messages: Message[];
  tools: ToolRecord[];
}

/** One model call's usage, as the gateway reported it; a count it left out is null. */
export interface ModelUsage {
  /** The model that answered, as the gateway names it. */
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  /** What the call cost: as the gateway reported it, or priced from its model info
   * (`cost_source`); null or absent when neither gives it. */
  cost?: number | null;
  /** Where the cost came from: `gateway` (reported) or `model_info` (priced from the
   * gateway's model info, at the call's tokens); older records omit it. */
  cost_source?: 'gateway' | 'model_info' | null;
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
  /** Its references to recorded files, each pinned, with a newer revision as `latest`, or
   * with why it is not (`unresolved`; figures, FG-8, FG-9). Older servers send none. */
  pins?: Record<string, unknown>[];
  /** The checks of its figures, when this revision was checked as the current one: each
   * figure's place and result (figures, FG-23). Older servers send none. */
  figure_checks?: Record<string, unknown>[];
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

/**
 * The most a page of the artifact list can be: ten entries, each path up to 4,096 bytes
 * of characters sent as escapes. A workspace test keeps this at or above that bound.
 */
export const ARTIFACT_LISTING_MAX_BYTES = 256 * 1024;

const part = encodeURIComponent;

/**
 * The most bytes a project memory answer can take: 100 entries, or 50 proposals each with
 * the entry beside it, or 50 versions, of 1,000 characters each, all escaped
 * (`test_routes_memory.py` checks the server's largest answers against it).
 */
export const MEMORY_MAX_BYTES = 2048 * 1024;

/** The ledger's answer to the user's change (orchestrator `MutationResult`), as the views read it. */
export interface LedgerChangeAnswer {
  operation_id: string;
  replayed: boolean;
  entries: { entry_id: string; handle: string; revision: number; status: string }[];
  cleared_total: number;
  flags_cleared: string[];
  flags_reopened: string[];
}

/** An archived message, read whole (CM-3, CM-34). */
export interface ArchivedMessage {
  message: string;
  session: string;
  position: number;
  run: string;
  parent: string | null;
  created: string;
  role: string;
  kind: string;
  source: string | null;
  call_id: string | null;
  /** Its content as the run added it, then an answer's tool calls, one JSON object a line. */
  text: string;
}

export const api = {
  projectMacros: (project: string) => get<MacroContext>(`/projects/${part(project)}/macros`),
  changeMacros: (project: string, value: Record<string, unknown>) =>
    post<MacroContext>(`/projects/${part(project)}/macros`, value),
  projects: () => get<ProjectListing>('/projects'),
  project: (project: string, signal?: AbortSignal) =>
    get<ProjectAnswer>(`/projects/${part(project)}`, { signal }).then((found) => found.project),
  session: (project: string, session: string, signal?: AbortSignal) =>
    get<SessionDetail>(`/projects/${part(project)}/sessions/${part(session)}`, { signal }),
  jobs: (project: string, session: string, signal?: AbortSignal) =>
    get<JobListing>(`/projects/${part(project)}/sessions/${part(session)}/jobs`, { signal }),
  cancelJob: (project: string, session: string, job: string) =>
    post(`/projects/${part(project)}/sessions/${part(session)}/jobs/${part(job)}/cancel`, {}),
  /** The session's runs, running ones included, and its delegations (PD-42). */
  runTree: (project: string, session: string, signal?: AbortSignal) =>
    get<RunTree>(`/projects/${part(project)}/sessions/${part(session)}/tree`, { signal }),
  message: (project: string, message: string) =>
    get<ArchivedMessage>(`/projects/${part(project)}/messages/${part(message)}`),
  run: (project: string, session: string, run: string) =>
    get<RunRecord>(`/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}`),
  documents: (project: string, signal?: AbortSignal) =>
    get<{ documents: DocumentInfo[] }>(`/projects/${part(project)}/documents`, { signal }).then(
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
  /** The project's artifacts of one group, the most recently changed first, a page at a
   * time, and whether the viewer is on. */
  artifacts: (
    project: string,
    before: number | null,
    group: ArtifactGroup,
    filter: ArtifactFilter = NO_FILTER,
  ) =>
    get<ArtifactListing>(
      `/projects/${part(project)}/artifacts?${artifactQuery(group, filter, before)}`,
      { maxBytes: ARTIFACT_LISTING_MAX_BYTES },
    ),
  /** The project's settings for the files its executions write, with the defaults. */
  fileSettings: (project: string) => get<FileSettings>(`/projects/${part(project)}/files/settings`),
  changeFileSettings: (project: string, value: Record<string, unknown>) =>
    post<FileSettings>(`/projects/${part(project)}/files/settings`, value),
  /** The figure frame's URL for a chart, and a capability for each data file it pins, in
   * order, for 10 minutes (FG-15). */
  chartLink: (project: string, data: { artifact: string; revision: number; sha256: string }[]) =>
    get<ChartLink>(
      `/projects/${part(project)}/figures/chart` +
        (data.length
          ? '?' +
            new URLSearchParams(
              data.map((file) => ['data', `${file.artifact}:${file.revision}:${file.sha256}`]),
            )
          : ''),
    ),
  /** One PNG or JPEG revision's URL on the preview origin, for 10 minutes (FG-13). */
  figureLink: (project: string, artifact: string, revision: number, sha256: string) =>
    get<FigureLink>(
      `/projects/${part(project)}/artifacts/${part(artifact)}/revisions/${revision}/figure` +
        `?sha256=${part(sha256)}`,
    ),
  /** A link that opens one PDF revision in the viewer, for 10 minutes. */
  viewerLink: (project: string, artifact: string, revision: number, sha256: string) =>
    get<ViewerLink>(
      `/projects/${part(project)}/artifacts/${part(artifact)}/revisions/${revision}/viewer` +
        `?sha256=${part(sha256)}`,
    ),
  /** A page of the project's ledger entries of one type and current status, oldest first. */
  ledger: (project: string, type: string, status: string, after: string | null = null) =>
    get<LedgerListing>(
      `/projects/${part(project)}/ledger?` +
        new URLSearchParams({ type, status, ...(after === null ? {} : { after }) }).toString(),
    ),
  /**
   * An entry by its handle or ID, at its current revision or at `revision`; at its current
   * revision, the links to it from the `incomingAfter`-th on, a page of them.
   */
  ledgerEntry: (
    project: string,
    entry: string,
    revision: number | null = null,
    incomingAfter = 0,
  ) =>
    get<LedgerEntry>(
      `/projects/${part(project)}/ledger/${part(entry)}` +
        (revision === null ? '' : `?revision=${revision}`) +
        (incomingAfter === 0
          ? ''
          : `${revision === null ? '?' : '&'}incoming_after=${incomingAfter}`),
    ),
  /** The gate for an entry as it reads now, each clause with why, and the decisions that affect it. */
  ledgerGate: (project: string, entry: string) =>
    get<LedgerGate>(`/projects/${part(project)}/ledger/${part(entry)}/gate`),
  /** A page of the work records on an entry, newest first, before position `before`. */
  ledgerWork: (project: string, entry: string, before: number | null = null, limit = 20) =>
    get<LedgerWork>(
      `/projects/${part(project)}/ledger/${part(entry)}/work?` +
        new URLSearchParams({
          limit: String(limit),
          ...(before === null ? {} : { before: String(before) }),
        }).toString(),
    ),
  /** The consistency reviewer's checks of each quote an entry's revision holds, newest first. */
  ledgerCitations: (project: string, entry: string, revision: number) =>
    get<LedgerCitations>(
      `/projects/${part(project)}/ledger/${part(entry)}/citations?revision=${revision}`,
    ),
  /** The user's record that they checked an entry at the revision they read; a newer one is a conflict. */
  checkLedgerEntry: (
    project: string,
    entry: string,
    check: { revision: number; note?: string; operation_id: string },
  ) => post<LedgerChangeAnswer>(`/projects/${part(project)}/ledger/${part(entry)}/checks`, check),
  /** The user's assessment of an obstruction claimed on an issue, at the revision they read. */
  assessLedgerEntry: (
    project: string,
    entry: string,
    assessment: {
      claim: string;
      verdict: 'accepted' | 'rejected';
      revision: number;
      note: string;
      operation_id: string;
    },
  ) =>
    post<LedgerChangeAnswer>(
      `/projects/${part(project)}/ledger/${part(entry)}/assessments`,
      assessment,
    ),
  /** The user's clearing of open flags on an entry: those named, or else all, with the reason. */
  clearLedgerFlags: (
    project: string,
    entry: string,
    change: { flags: string[]; reason: string; operation_id: string },
  ) => post<LedgerChangeAnswer>(`/projects/${part(project)}/ledger/${part(entry)}/flags`, change),
  /** The user's reopening of cleared flags on an entry, each as a new flag, with the reason. */
  reopenLedgerFlags: (
    project: string,
    entry: string,
    change: { flags: string[]; reason: string; operation_id: string },
  ) =>
    post<LedgerChangeAnswer>(
      `/projects/${part(project)}/ledger/${part(entry)}/flags/reopened`,
      change,
    ),
  /** The user's dropping of an entry's `dominates` link to `target`, at the revision read, with the reason. */
  dropDominatesLink: (
    project: string,
    entry: string,
    drop: { target: string; revision: number; reason: string; operation_id: string },
  ) =>
    post<LedgerChangeAnswer>(
      `/projects/${part(project)}/ledger/${part(entry)}/dominates/dropped`,
      drop,
    ),
  /** A session's fold proposals waiting (CM-15), oldest first. */
  foldProposals: (project: string, session: string) =>
    get<FoldProposalList>(`/projects/${part(project)}/sessions/${part(session)}/proposals`).then(
      (found) => found.proposals,
    ),
  /** The user's acceptance of a fold's proposal, which the ledger records as the user's. */
  acceptFoldProposal: (
    project: string,
    session: string,
    proposal: string,
    body: { operation_id: string },
  ) =>
    post<Record<string, Json>>(
      `/projects/${part(project)}/sessions/${part(session)}/proposals/${part(proposal)}/accepted`,
      body,
    ),
  /** The user's dismissal of a fold's proposal, with the reason. */
  dismissFoldProposal: (project: string, session: string, proposal: string, reason: string) =>
    post<ProposalView>(
      `/projects/${part(project)}/sessions/${part(session)}/proposals/${part(proposal)}/dismissed`,
      { reason },
    ),
  /** A page of an entry's revisions, newest first, before `before`. */
  ledgerHistory: (project: string, entry: string, before: number | null = null) =>
    get<LedgerHistory>(
      `/projects/${part(project)}/ledger/${part(entry)}/history` +
        (before === null ? '' : `?before=${before}`),
    ),
  /** A page of the project's frontier categories, in the order they were defined. */
  frontierCategories: (project: string, after: string | null = null) =>
    get<FrontierCategories>(
      `/projects/${part(project)}/frontier` + (after === null ? '' : `?after=${part(after)}`),
    ),
  /**
   * A category's frontier: a page of its groups, in the order of their keys, after the
   * group keyed `groupsAfterKey`, with the proposed candidates made before result number
   * `candidatesBefore`, or the newest.
   */
  frontier: (
    project: string,
    category: string,
    groupsAfterKey: string | null = null,
    candidatesBefore: number | null = null,
  ) => {
    const query = new URLSearchParams({
      ...(groupsAfterKey === null ? {} : { groups_after_key: groupsAfterKey }),
      ...(candidatesBefore === null ? {} : { candidates_before: String(candidatesBefore) }),
    }).toString();
    return get<FrontierAnswer>(
      `/projects/${part(project)}/frontier/${part(category)}` + (query ? `?${query}` : ''),
    );
  },
  /** A category at its current revision. */
  frontierDefinition: (project: string, category: string) =>
    get<FrontierCategory>(`/projects/${part(project)}/frontier/${part(category)}/definition`),
  /** The user's definition of a category, or its revision at `expected`, the revision read. */
  defineCategory: (project: string, body: CategoryBody & { operation_id: string }) =>
    post<Category>(`/projects/${part(project)}/frontier`, body),
  /** A category's changes to its best known results, newest first, before change `before`. */
  frontierHistory: (project: string, category: string, before: number | null = null) =>
    get<FrontierHistoryAnswer>(
      `/projects/${part(project)}/frontier/${part(category)}/history?` +
        new URLSearchParams({
          limit: '20',
          ...(before === null ? {} : { before: String(before) }),
        }),
    ),
  /** A category's best known results as they were at `at`, a time with its zone. */
  frontierPast: (project: string, category: string, at: string) =>
    get<FrontierPastAnswer>(
      `/projects/${part(project)}/frontier/${part(category)}/past?` + new URLSearchParams({ at }),
    ),
  /** The project memory's accepted entries in order, and the bound agents receive. */
  memory: (project: string) =>
    get<MemoryAnswer>(`/projects/${part(project)}/memory`, { maxBytes: MEMORY_MAX_BYTES }),
  /** The project's skills: its settings, its pins with their imports' reports, and the
   * skills a run is given now. */
  skills: (project: string) => get<ProjectSkills>(`/projects/${part(project)}/skills`),
  /** Safe configured MCP service identity and bounded historical run observations. */
  mcpServices: (project: string) => get<McpInventory>(`/projects/${part(project)}/mcp-services`),
  /** The user's change of the always-loaded and excluded skills, at `expected`. */
  changeSkillSettings: (project: string, change: SkillSettingsChange) =>
    post<SkillSettingsView>(`/projects/${part(project)}/skills/settings`, change),
  /** The proposals that wait for the user, oldest first. */
  memoryProposals: (project: string) =>
    get<MemoryProposals>(`/projects/${part(project)}/memory/proposals`, {
      maxBytes: MEMORY_MAX_BYTES,
    }).then((found) => found.proposals),
  /** A page of an entry's versions, newest first, before `before`. */
  memoryHistory: (project: string, entry: string, before: number | null = null) =>
    get<MemoryHistory>(
      `/projects/${part(project)}/memory/${part(entry)}/history` +
        (before === null ? '' : `?before=${before}`),
      { maxBytes: MEMORY_MAX_BYTES },
    ).then((found) => found.history),
  /** The user's change of the memory, with its operation ID. */
  changeMemory: (project: string, change: Record<string, unknown>) =>
    post<MemoryChange>(`/projects/${part(project)}/memory`, change),
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
