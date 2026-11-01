# Workspace browsers

Status: implemented in the authenticated workspace, 2026-10-18. The ledger and
Skills browsers retain their existing list/detail and explicit save operations.
Overview, Memory and session Plan/Agents/Activity now use searchable, selected
records with tab-scoped view state. Memory edits and skill-loading drafts retain
the original revision across navigation and conflicts. MCP metadata and explicit
availability checks use the controlled host-to-proxy seam described below.
Memory and skill editors warn when drafts are held only in tab memory because
browser storage is blocked or full, and report saved data that could not be restored.
Deployment and real browser acceptance are recorded separately; local checks do
not establish deployed service health.

## Shared interaction

Keep project/session navigation and the [research layout](research-ui.md).
Search, filters and sorting sit above a compact list; selecting an item opens
its details alongside it. Details become a drawer on smaller screens. Preserve
search, selection and unsaved drafts across navigation. Use readable names
first, identifiers and provenance in details. Loading, empty, failed and stale
observations remain distinct. Nothing starts research simply by being viewed.

Desktop detail panes have a draggable divider, with continuous sizing up to
half the browser window. The chosen width survives screen changes. The focused
divider also supports arrow keys and Home/End; smaller screens retain drawers.
Resizing must preserve selection, scroll and drafts without rebuilding content.

The ledger keeps the same project and session sidebar as the rest of the
workspace, with a direct return link to the last session. Ledger kinds belong
in the list's filters, not in a replacement navigation sidebar. Moving between
the ledger and a session preserves local view state and unsent drafts.

## Skills

Separate Catalog, Pinned sources and Loading rules. The catalog shows each
skill's name, description, source, topic and loading choice. Search and filters
operate on those fields. Selection reveals pin/revision, metadata authorship,
entry status and relevant helper bindings. Source details hold import warnings,
overridden built-ins, applied bindings and held bindings with their reasons.

Available, always loaded and excluded are configuration choices for future
runs; they do not describe what a historical run loaded. Excluded identifiers
without effective metadata still appear, without an invented description.
Loading rules use individual or bulk choices, an explicit pending-change review
and save/discard actions. A revision conflict retains the draft for comparison
with the latest settings rather than silently overwriting them.

The existing project skills GET provides effective metadata, pins and import
reports, settings and overrides, and source bindings. The settings POST accepts
`expected`, `always` and `excluded`. The redesign must retain that revision check
and the existing run-specific skills view. Workspace routes for skill bodies,
import, update, unpin and override editing do not exist; the prototype cannot
establish those capabilities.

## MCP services

Add a service list with readable identity/purpose, configuration revision and
separate policy and observation summaries. Service details offer Overview,
Tools, Policy and Observations. Tool descriptions and parameter schemas are
read-only, with search; viewing them never calls a tool or grants access.

Configuration, permission and availability answer different questions:

- **Configured** means the service is part of the project's configuration.
- **Policy** describes the applicable access rules, including required approval;
  it is not a promise that a future call will succeed.
- **Last observation** identifies its time, run and result. A successful listing
  does not establish present availability or successful execution. No observation
  is unknown, not a failure. Old or failed observations remain visibly dated.

The authenticated project inventory retains the bounded historical run sample
(at most 24 main runs across eight sessions, scanning at most 64 recent entries
and eight main runs per session). It also reads `/metadata/{server}` through a
private host-to-proxy client. This authenticated endpoint accepts orchestrator
callers only, scopes by their bound project and rejects browser Origin headers.
It reads the current bound policy and process-lifetime cached observations;
viewing never connects to an upstream service. A standalone workspace without
launcher metadata wiring reports unknown metadata rather than inferring it.

Actual `tools/list` traffic populates a bounded proxy snapshot: at most 128 tools
per service, 512 KiB of projected schema/prose, 32 cached service snapshots. Tool
schemas show structural types, properties and bounds; defaults, examples, enum
values, schema descriptions and external references are omitted explicitly.
Descriptions are untrusted plain text; addresses and credential placeholders are
withheld. Current per-tool policy decisions include the hard floor and pool
ceiling. Metadata shows authentication mode, tool patterns, worker access and
payload limits, without URLs, credential references, raw configuration or remote
error bodies. A policy revision change leaves the old listing visibly dated.
Tool-list-change notifications invalidate the cached listing.

**Check availability** is a separate same-origin authenticated POST to
`/api/v0/projects/{project}/mcp-services/{server}/check` with `{}`. It performs only
the normal policy-checked initialize/metadata-list handshake through the host
client, bounded to 15 seconds and 32 pages, and never invokes `tools/call`.
The result records a checked timestamp and listed/denied/unavailable/timeout
category. Since the MCP proxy retired (D98, 2026-10-26), the check is one listing by the agent runtime's client, asked of the control service, and the tools are the runtime's latest reported listing, kept by the worker. Observations
become stale after five minutes; missing checks or a process restart are unknown.
A listing success does not prove that a particular tool call will succeed.
Previous safe snapshots survive a failed refresh in this workspace process and
are labeled previous.
No browser gets credentials or direct MCP access.

## Classification review

*Decided 2026-10-25 by the owner (A3, D96's "accept-or-edit view"): the three questions
below as recommended; not built.* D96 says each server's classification is checked in and admin-reviewed: a
trusted-side model drafts a proposal that the admin accepts or edits in the workspace,
and a drift test fails when the server's tool list changes. The library, the
checked-in files, `classify-draft` and the middleware's drift records exist
([connectors](../MCP-CONNECTORS.md#classification-files)); the view does not. Items are
numbered CL-n.

- CL-1. **Where.** The MCP services browser gains a Classification tab per service and a
  project-level Classifications list. The tab shows the classification in force for the
  project: its source (the checked-in file's name and `pinned` date, or an accepted
  project revision, CL-3), and for each tool its class, reason, argument rules,
  significant argument and pin state against the latest listing (`current`, `changed`,
  `added`, `removed`, from the middleware's `mcp-tools-drift` records and the listing
  snapshot); for a tool the classification does not mention, the class its hints give
  and that it escalates. Read-only; viewing calls no tool and grants nothing, as the
  Tools tab's rules say.
  *As built 2026-10-25:* the MCP services screen has a Classifications list (each
  configured service's source, pinned or accepted date, tool count and drift state) and a
  fifth tab, Classification (`ClassificationTab.svelte`), with the in-force table, the
  drafts with their editor and acceptance, and the drift section with Re-accept. The
  routes are `GET …/classifications`, `GET …/mcp-services/{server}/classification`, `GET
  …/classification/drafts`, `POST …/classification/drafts/{draft}/accept` and `POST
  …/mcp-services/{server}/drift/accept` (`packages/workspace/…/classifications.py`),
  passing the worker's operator routes through, with each write's operation ID for the
  worker's receipts.
- CL-2. **Drafts.** A draft `classify-draft` wrote for the project (the YAML marked DRAFT,
  pinned by digest) is a record the view lists beside the classification in force, tool
  by tool: the proposed class, reason and rules against the current ones, with widenings
  (a class that would let a call pass without approval where it escalates today) marked.
  The admin edits a draft in the view: class, reason, argument rules and the significant
  argument per tool, within the library's schema; an edit is checked as the loader
  checks a file. A draft is the model's text, framed as such.
- CL-3. **Accepting.** Accepting a draft, edited or not, makes a project classification
  revision in the control store's policy store, as `policy add` makes a policy revision:
  the YAML the library would load, its pins, who accepted it, when, the reason and the
  diff from the classification it replaces. The middleware compiles it into the
  project's policy at the next revision, and it supersedes the checked-in file for that
  project; the checked-in files stay the repository's defaults and the drift fixtures.
  Only a user with the admin role may accept (D97's ceilings); an agent never reads a
  draft as an instruction nor edits a classification. Accepting shows a confirmation
  that lists every widening, and the acceptance is an event with its diff. An accepted
  revision can be exported as the YAML to check in.
  - *As built 2026-10-25 (backend):* the policy store is the file store the worker and the
    policy middleware share (`classifications/<project>/<server>/<hex>.yaml`, its
    acceptance record and a `current.json` binding), not the control store. The workspace
    has one user per installation and no roles, so "admin-only" is that user holding the
    worker's operator token; a multi-user workspace would need a role. The middleware reads
    the binding at every call, so a revision applies at the next call, not only at the next
    policy revision; the widening rule is in [connectors](../MCP-CONNECTORS.md#project-revisions).
    An accepted revision exports as its stored YAML through `GET /v1/classifications/{server}/
    revisions/{revision}/export?project=P` ([worker](../WORKER.md)).
  - *As built 2026-10-28 (admin group, D104):* behind Authentik with an admin group
    configured, "admin" is a member of that Authentik group, from the provider's signed
    token, or the operator's login link: the workspace's accept route answers anyone else
    403 before it asks the worker, and the page disables Accept with the reason. Without an
    admin group every logged-in user is an admin, as before. The workspace logs who
    accepted; the worker's acceptance record does not yet name the user (it is the
    workspace's operator client), which is open work
    ([workspace](../WORKSPACE.md#login-through-authentik)).
  - *As built 2026-10-27 (fences):* the agent runtime's native fence admits tools by the
    revision in force when the launcher compiles it, on `up` and at every runtime
    replacement, so a revision accepted while a runtime runs reaches the fence at the next
    replacement; the middleware decides by it from the next call, so a stale fence only
    refuses a widening and never admits more than the middleware allows. An unreadable
    binding leaves the fence on the checked-in file while the middleware refuses the
    server's calls. The Kubernetes runtime manager does the same at each creation of the
    runtime's sandbox, reading the revisions through the worker
    ([connectors](../MCP-CONNECTORS.md#project-revisions)).
- CL-4. **Drift.** When the middleware records `tool_list_drift` for a server, the tab
  shows the added, removed and changed tools with their digests, dated, and the
  middleware's hold (its non-read calls ask until re-accepted, D103). **Re-accept** from
  the view does what `accept-tools` does today, through the worker, with the user's
  reason recorded: a changed tool keeps its class only after re-acceptance, an added tool
  is `unclassified` until a classification names it, a removed one is listed as gone.
  Re-acceptance is admin-only, as acceptance is.
  - *As built 2026-10-28 (admin group, D104):* as for CL-3: with an Authentik admin group,
    the workspace's re-accept route refuses a user outside it (403) and the page disables
    Re-accept with the reason; the workspace logs who re-accepted.
- CL-5. **Proxy-side drift.** Moot: the MCP proxy retired after the A2 decision
  (2026-10-26). The tab reads the middleware's records and the runtime's reported
  listings' (source `runtime`).
- CL-6. **Records and routes.** `GET …/mcp-services/{server}/classification` (in force,
  per tool with pin state), `GET …/classification/drafts` and `POST …/classification/
  drafts/{draft}/accept` (admin, with the edited YAML and a reason), `POST
  …/mcp-services/{server}/drift/accept` (admin, a reason). The policy store gains the
  classification revision kind; the drift read is the middleware's existing record
  through the worker, as the request cards read it.
  - *As built 2026-10-25 (backend):* the drift read and both acceptances are worker routes,
    operator-only like the permission requests: `GET /v1/classifications?project=P`,
    `GET /v1/classifications/{server}/drafts?project=P`, `POST /v1/classifications/{server}/
    drafts/{draft}/accept` and `POST /v1/classifications/{server}/drift/accept`
    (`contracts/classifications.py`; [worker](../WORKER.md)), which the workspace's routes
    relay. Drift records carry each added and changed tool's digest; acceptances are
    `mcp-classification-accepted` and `mcp-tools-accepted` records naming the project.

Decided by the owner, 2026-10-25: (1) accepted classifications are project revisions in
the policy store, exportable for check-in, not repository files only (acceptance needs no
deploy and is attributed); (2) acceptance and re-acceptance are admin-only; (3) proxy-side
drift waits for the A2 decision.

## Other screens

An [agent context inspector](context-inspector.md) is available from secondary
orchestrator/agent actions as an embedded dialog. It covers context usage and
cache telemetry, fold provenance and searchable original conversation history.
Its recorded-attempt telemetry and original-history APIs are available; exact working-context composition is separately scoped in that specification.

- **Overview:** sessions, recent documents, pending reviews and useful shortcuts
  first. Policy, environment, math macros and file capture settings get a separate
  settings area; keep their existing operations and limits reachable.
- **Memory:** a searchable ordered accepted list, proposal inbox, exact before/
  after review and revision history. Display the 16,000-character agent budget
  and preserve ordering, truncation and omission semantics. Accepted text is
  distinct from proposals; changing a filter must not change the receive order.
- **Plan:** phase/step navigation with selected-step detail and existing editing.
  Recorded plan progress must not imply that a run is currently executing.
- **Agents:** parent/child list, observed state filters, selected brief, outcome
  and activity, retaining timestamps, stale warnings and truncated observations.
- **Activity:** category/run filters, chronological events and event details.
  Preserve access to reviews, jobs, kernels and explicit recovery actions;
  failure to observe one source must not mark all sources healthy or failed.

The session retains document tabs and library filters, the half-window pane,
100 MiB upload affordance and shared Markdown/math rendering. Prototype actions
are local demonstrations, explicitly labelled; a production integration needs
the existing authenticated APIs and their revision/error behavior.

## Acceptance before integration

Review the interactive design with the owner, then implement in small steps.
Check search/filter/selection, browser back, draft retention and conflict
handling, keyboard focus, narrow layouts and provenance. Derive status labels
from actual contracts, not the prototype's sample records. Check each screen's
empty/error/stale behavior and retain existing functionality. Browser evidence
for the prototype is separate from tests of a future production implementation.
