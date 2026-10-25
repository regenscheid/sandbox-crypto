// SPDX-License-Identifier: MIT
/**
 * The workspace API, version 0 (packages/workspace, api.py and documents.py): read-only
 * views of the harness's projects, sessions and runs and of the project's living
 * documents, live job status and cancellation, and repairs of LaTeX by the reviewer model
 * (mathrepair.py). Requests go through http.ts. The session cookie travels with every
 * request, and the browser adds the `Origin` header the server requires of a POST.
 */
import type {
  CapturedCalls,
  ContextParts,
  ContextText,
  IndexStatus,
  ChartLink,
  ClassificationAccepted,
  ClassificationListing,
  ClassificationView,
  ContextHistoryPage,
  ContextAttemptsPage,
  ContextTimelinePage,
  FigureLink,
  HistoryPage,
  HistoryChunk,
  ArtifactListing,
  RevisionListing,
  ConnectorSettings,
  DraftListing,
  DriftAccepted,
  AnswerResult,
  GrantListing,
  PendingApproval,
  PendingListing,
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
  ProjectMonitor,
  ModelSettings,
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
import type { CategoryBody } from './frontierForm';
import { ApiError, get, post } from './http';
import { documentApi } from './documentApi';
import { commentApi } from './commentApi';
import { jobApi } from './jobApi';
import { signOut } from './session';

export { ApiError };

/** A project in the list; a project whose record is damaged has only its ID and error. */
export type ProjectSummary = ProjectListing['projects'][number];
export type Session = SessionView;
export type Project = ProjectView;
/** An answer to a permission request: once, a grant for the session or the project, or a
 * refusal. The server refuses a scope the request's ceiling does not allow. A hold of the
 * session's calls (D103) is cleared, or its session stopped, instead. */
export type PermissionDecision = 'once' | 'session' | 'project' | 'deny' | 'clear' | 'stop';
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
  role?: 'main' | 'delegated';
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
  cache_read_input_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
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

export type {
  DocumentAuthor as Author,
  BackgroundJob,
  JobCheckin,
  JobWait,
  JobListing,
  DocumentInfo,
  DocumentWarning,
  RevisionInfo,
  DocumentRevision,
  HistoryRevision,
  PatchRecord,
  DocumentHistory,
  DocumentChange,
  PatchPreview,
  CommentAnchor,
  CommentBodySource,
  CommentEntry,
  CommentThread,
  CommentListing,
  CommentChange,
  DocumentChangeRequest,
  CommentChangeRequest,
  JobCancellation,
  JobRelease,
  JobSandboxView,
  JobManifestView,
} from './api-types';

/**
 * The most a page of the artifact list can be: ten entries, each path up to 4,096 bytes
 * of characters sent as escapes. A workspace test keeps this at or above that bound.
 */
export const ARTIFACT_LISTING_MAX_BYTES = 256 * 1024;

/** The most a page of one artifact's revisions can be: twenty such entries. */
export const ARTIFACT_REVISIONS_MAX_BYTES = 512 * 1024;

const part = encodeURIComponent;

/**
 * The most bytes a project memory answer can take: 100 entries, or 50 proposals each with
 * the entry beside it, or 50 versions, of 1,000 characters each and ten anchors, all escaped
 * (`test_routes_memory.py` checks the server's largest answers against it).
 */
export const MEMORY_MAX_BYTES = 4096 * 1024;

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

export interface MessageFieldChunk {
  message: string;
  field: 'content' | 'tool_calls';
  offset: number;
  total: number;
  next_offset: number | null;
  data_base64: string;
}

export const api = {
  ...documentApi,
  ...commentApi,
  ...jobApi,
  projectMacros: (project: string) => get<MacroContext>(`/projects/${part(project)}/macros`),
  changeMacros: (project: string, value: Record<string, unknown>) =>
    post<MacroContext>(`/projects/${part(project)}/macros`, value),
  projects: () => get<ProjectListing>('/projects'),
  project: (project: string, signal?: AbortSignal) =>
    get<ProjectAnswer>(`/projects/${part(project)}`, { signal }).then((found) => found.project),
  session: (project: string, session: string, signal?: AbortSignal) =>
    get<SessionDetail>(`/projects/${part(project)}/sessions/${part(session)}`, { signal }),
  /** Open a new session in the project's shared environment; a name it has is a conflict. */
  createSession: (project: string, session: string) =>
    post<SessionView>(`/projects/${part(project)}/sessions`, { session_id: session }),
  /** The session's runs, running ones included, and its delegations (PD-42). */
  runTree: (project: string, session: string, signal?: AbortSignal) =>
    get<RunTree>(`/projects/${part(project)}/sessions/${part(session)}/tree`, { signal }),
  message: (project: string, message: string) =>
    get<ArchivedMessage>(`/projects/${part(project)}/messages/${part(message)}`),
  messageChunk: (project: string, message: string, field: 'content' | 'tool_calls', offset = 0) =>
    get<MessageFieldChunk>(
      `/projects/${part(project)}/messages/${part(message)}/chunks?` +
        new URLSearchParams({ field, offset: String(offset) }),
    ),
  run: (project: string, session: string, run: string) =>
    get<RunRecord>(`/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}`),
  contextAttempts: (project: string, session: string, run: string, after = '') =>
    get<ContextAttemptsPage>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/context/attempts?` +
        new URLSearchParams({ after }),
    ),
  contextTimeline: (
    project: string,
    session: string,
    run: string,
    answersAfter = 0,
    eventsAfter = 0,
    signal?: AbortSignal,
    tail = false,
  ) =>
    get<ContextTimelinePage>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/context/timeline?` +
        new URLSearchParams({
          answers_after: String(answersAfter),
          events_after: String(eventsAfter),
          limit: '50',
          tail: tail ? '1' : '0',
        }),
      { signal, maxBytes: 2 * 1024 * 1024 },
    ),
  contextCaptures: (
    project: string,
    session: string,
    run: string,
    scope: 'attempt' | 'agent',
    before = 0,
    signal?: AbortSignal,
  ) =>
    get<CapturedCalls>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/context/captures?${new URLSearchParams({ scope, before: String(before) })}`,
      { signal },
    ),
  contextParts: (
    project: string,
    session: string,
    run: string,
    identity: string,
    after = 0,
    signal?: AbortSignal,
  ) =>
    get<ContextParts>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/context/captures/${part(identity)}/parts?after=${after}`,
      { signal },
    ),
  contextPartText: (
    project: string,
    session: string,
    run: string,
    identity: string,
    ordinal: number,
    offset = 0,
    signal?: AbortSignal,
  ) =>
    get<ContextText>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/context/captures/${part(identity)}/parts/${ordinal}?offset=${offset}`,
      { signal },
    ),
  indexContextHistory: (
    project: string,
    session: string,
    run: string,
    scope: 'attempt' | 'agent',
  ) =>
    post<IndexStatus>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/context/history/index?scope=${scope}`,
      undefined,
    ),
  contextHistory: (
    project: string,
    session: string,
    run: string,
    options: {
      after?: number;
      contains?: string;
      role?: 'user' | 'assistant' | 'tool';
      kind?: string;
      includeDerived?: boolean;
      scope?: 'attempt' | 'agent';
      signal?: AbortSignal;
    } = {},
  ) => {
    const query = new URLSearchParams();
    if (options.after) query.set('after', String(options.after));
    if (options.contains) query.set('contains', options.contains);
    query.set('indexed', '1');
    if (options.role) query.set('role', options.role);
    if (options.kind) query.set('kind', options.kind);
    if (options.scope) query.set('scope', options.scope);
    if (options.includeDerived) query.set('include_derived', '1');
    return get<ContextHistoryPage>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/context/history` +
        (query.size ? `?${query}` : ''),
      { signal: options.signal },
    );
  },
  runHistory: (project: string, session: string, run: string, collection: string, offset = 0) =>
    get<HistoryPage>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/history?` +
        new URLSearchParams({ collection, offset: String(offset), limit: '100' }),
    ),
  runHistoryChunk: (
    project: string,
    session: string,
    run: string,
    collection: string,
    position: number,
    offset = 0,
  ) =>
    get<HistoryChunk>(
      `/projects/${part(project)}/sessions/${part(session)}/runs/${part(run)}/history/chunks?` +
        new URLSearchParams({ collection, position: String(position), offset: String(offset) }),
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
  /** One artifact's revisions, the newest first, twenty at a time (RA-37). */
  artifactRevisions: (project: string, artifact: string, before: number | null = null) =>
    get<RevisionListing>(
      `/projects/${part(project)}/artifacts/${part(artifact)}/revisions` +
        (before === null ? '' : `?before=${before}`),
      { maxBytes: ARTIFACT_REVISIONS_MAX_BYTES },
    ),
  /** The project's settings for the files its executions write, with the defaults. */
  fileSettings: (project: string) => get<FileSettings>(`/projects/${part(project)}/files/settings`),
  changeFileSettings: (project: string, value: Record<string, unknown>) =>
    post<FileSettings>(`/projects/${part(project)}/files/settings`, value),
  /** Model aliases and reasoning effort for newly admitted agent trees. */
  modelSettings: (project: string) =>
    get<ModelSettings>(`/projects/${part(project)}/models/settings`),
  changeModelSettings: (project: string, value: Record<string, unknown>) =>
    post<ModelSettings>(`/projects/${part(project)}/models/settings`, value),
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
  /** The project's monitor figures for a window (D103): counted by the platform, no agent text. */
  monitor: (project: string, window: '24h' | '7d', signal?: AbortSignal) =>
    get<ProjectMonitor>(`/projects/${part(project)}/monitor?` + new URLSearchParams({ window }), {
      signal,
    }),
  /** The requests that wait for the user's answer in the session (D76, D97): connector
   * tool calls the MCP proxy or the policy middleware holds, and research code's network
   * and package requests. */
  permissionRequests: (project: string, session: string, signal?: AbortSignal) =>
    get<PendingListing>(
      `/projects/${part(project)}/sessions/${part(session)}/permission-requests`,
      { signal },
    ),
  answerPermission: (
    project: string,
    request: { seq: number; kind: PendingApproval['kind']; source?: PendingApproval['source'] },
    decision: PermissionDecision,
    session: string | null = null,
  ) =>
    post<AnswerResult>(`/projects/${part(project)}/permission-requests/${request.seq}/answer`, {
      decision,
      kind: request.kind,
      // Who holds it: the MCP proxy, or the worker for the policy middleware (D98).
      source: request.source ?? null,
      // The session whose run a stopped hold stops, when the hold names none (D103).
      session,
    }),
  /** The project's grants, with their last use (D97). */
  grants: (project: string) => get<GrantListing>(`/projects/${part(project)}/grants`),
  revokeGrant: (project: string, grant: string) =>
    post<GrantListing>(`/projects/${part(project)}/grants/${part(grant)}/revoke`, {}),
  /** Which configured connectors the project's user turned off. */
  connectorSettings: (project: string) =>
    get<ConnectorSettings>(`/projects/${part(project)}/mcp-services/settings`),
  changeConnectorSettings: (
    project: string,
    change: { expected: number; disabled: string[]; operation_id: string },
  ) => post<ConnectorSettings>(`/projects/${part(project)}/mcp-services/settings`, change),
  /** Every configured service's classification in force (D96, CL-1). */
  classifications: (project: string) =>
    get<ClassificationListing>(`/projects/${part(project)}/classifications`),
  /** One service's classification in force, per tool with its pin state; calls no tool. */
  classification: (project: string, server: string) =>
    get<ClassificationView>(
      `/projects/${part(project)}/mcp-services/${part(server)}/classification`,
    ),
  /** The drafts `classify-draft` wrote for the service: the model's text (CL-2). */
  classificationDrafts: (project: string, server: string) =>
    get<DraftListing>(
      `/projects/${part(project)}/mcp-services/${part(server)}/classification/drafts`,
    ),
  /** Accept a draft, edited or not, with the user's reason (CL-3). */
  acceptClassificationDraft: (
    project: string,
    server: string,
    draft: string,
    change: { yaml: string; reason: string; operation_id: string },
  ) =>
    post<ClassificationAccepted>(
      `/projects/${part(project)}/mcp-services/${part(server)}/classification/drafts/${part(draft)}/accept`,
      change,
    ),
  /** Re-accept the service's tool list after drift, with the user's reason (CL-4). */
  acceptDrift: (
    project: string,
    server: string,
    change: { reason: string; operation_id: string },
  ) =>
    post<DriftAccepted>(
      `/projects/${part(project)}/mcp-services/${part(server)}/drift/accept`,
      change,
    ),
  /**
   * Where a project revision of a service's classification exports as its YAML, exactly
   * as stored (CL-3): a link for the browser to follow, not a call.
   */
  classificationExportUrl: (project: string, server: string, revision: string) =>
    `/api/v0/projects/${part(project)}/mcp-services/${part(server)}/classification/revisions/${part(revision)}/export`,
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
