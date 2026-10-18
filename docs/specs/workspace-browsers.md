# Workspace browsers

Status: proposed interaction details for owner review, 2026-10-17. The owner
accepted the ledger's list/detail direction and requested the same usability
work on the other screens, especially Skills and MCP services. Local Codex
has produced a connected prototype with synthetic records. The ledger and Skills
browsers are integrated on local `dev`; the first MCP service browser is implemented
locally, while its tool and policy detail, Overview, Memory and session refinements
remain proposals. Authenticated synthetic browser exercise of the initial Skills and MCP screens is recorded in the [acceptance report](../../.agents/docs/agent-reports/context-continuations-2026-10-18.md); it does not qualify actual MCP calls or deployed configuration.

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

The first authenticated, project-scoped inventory endpoint projects only the
configured top-level service name and operator-written purpose, the project's
policy revision, and the newest matching observation within a bounded sample of
recorded main runs. Each sampled session scans at most 64 recent run entries for
up to eight main runs. Because the sample favors recently opened sessions, it may
miss newer runs in older sessions or behind many delegated runs. It reports the
sample and skipped records. It never connects
to the proxy when viewed. No raw configuration, credential reference, auth
header, endpoint URL or remote failure message is serialized. The launcher must
provide the project's safe service identities to the workspace; an independently
started workspace without them shows an empty inventory. Auth mode, specific
policy rules and live availability are not projected yet.

Run connector summaries currently retain listed counts, loaded tool names and
specification hashes, refused names and unavailable reasons. They do not retain
complete tool schemas or establish a live connection. The proposed inventory
must define the source and age of any future tool metadata, associate observations
with their run, and report missing data. The current screen shows listed tool
counts only. It does not add browser access to the MCP proxy, connection tests,
credential entry or approval actions.

## Other screens

An [agent context inspector](context-inspector.md) is available from secondary
orchestrator/agent actions as an embedded dialog. It covers context usage and
cache telemetry, fold provenance and searchable original conversation history.
Its production telemetry/API integration is still pending.

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
