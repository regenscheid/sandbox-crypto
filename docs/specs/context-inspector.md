# Agent context inspector

Status: requested by the owner, 2026-10-17. A read-only inspector for saved and live
main and delegated attempts is implemented in the current working branch: measured per-answer
input/cache chart by call number, timestamped live answer/fold observations,
the latest auxiliary-intent snapshot, saved fold summaries, explicit unavailable
working-view fields, and server-side original-message search. Recorded continuation
attempts can be listed and their history searched through the stable work identity;
children and siblings remain separate. Exact captured working-view provenance is
still pending.

## Entry and scope

Each orchestrator and delegated agent has a **Context management** action in a
secondary menu. It opens a large embedded dialog over the workspace, not another
browser or OS window. The dialog keeps the underlying session, document drafts
and navigation intact, restores focus on close, and supports keyboard navigation.
Its header identifies the project, session, agent, parent and selected run attempt.
An agent selector includes the orchestrator and children; a separate attempt
selector distinguishes interrupted work from its continuation. Never silently
combine different agents' context windows or stitch gaps across attempts.

The inspector is read-only. Its controls change only the view, not the agent's
context, history, execution, folding policy or cache. A recorded timestamp and
observation status distinguish the latest captured state from a live model call.

## Usage timeline

Plot input context occupancy against time, with an optional call-number axis.
Show the applicable context ceiling, fold trigger and hard wall. Keep provider
reported input separate from estimated working-view size; a fold's estimated
before/after reduction must not be presented as measured provider usage. Missing
measurements are gaps. Model changes, ceiling changes, interruptions and resumptions
have explicit boundaries. Cumulative billed tokens are a separate quantity.

Mark fold start/application, summary enrichment, skeleton-only fallback, overview
merge, tool-output stubbing, and rejected or cancelled transformations when
recorded. Selecting an event shows its time, source message span, outcome and
before/after size when available, with links to its summary and original messages.
Use an accessible event list alongside the graph; interaction cannot depend on
hover or color alone.

Show cached input share when the provider reports the breakdown: cache-read
tokens divided by normalized total input tokens, per call and weighted across
calls with known accounting. Report coverage (known calls / eligible calls),
fresh input and cache writes separately when available. Cache writes are not
hits; cached tokens still occupy context. Unknown counters are unavailable, not
zero. Do not infer hits from a cache key, speed or low cost, or label an average
of per-call percentages as an aggregate hit rate. Summarizer calls are distinct
from the selected agent's main conversation calls.

## Current context

Show the ordered composition of the last captured working view: system prompt
sections, memory/skills, preserved user text and research state, fold summaries
and merged overviews, tool-result stubs, and the recent verbatim message tail.
Display the source and precision of size accounting; do not invent exact token
counts per section from aggregate usage.

Selecting a folded section shows the retained summary/outline, source message
range and IDs, transformation state and nested constituent folds. Provide direct
navigation to the original range and individual stubbed tool results. Distinguish
model-written summaries from code-built outlines and merges. Exact historical
request reconstruction is shown only when that request was captured; otherwise
label a reconstruction or latest checkpoint as such. Unavailable or opaque
provider payloads are not decoded or replaced with invented reasoning text.

## Original history

Offer a chronological browser of the original retained messages, unaffected by
the working view's folds and summaries. Search the entire selected agent/attempt
archive on the server, with role, tool, time and message-ID filters. Search result
snippets lead to the original message and surrounding turns. Allow explicit
search across that agent's continuation attempts, identifying the attempt for
each result. Never imply that searching the loaded page searched the full archive.
Derived fold/summary records are separate from the original conversation, with
an explicit option to include platform records.

Use cursor pagination or virtualization; read large bodies and tool records in
chunks on demand. Show completion/coverage and any legacy omissions, missing
payloads or retention limits. Original model-visible tool text may itself have
been bounded; label any separately retained full tool output distinctly. Preserve
original content and message IDs. Apply the existing Markdown/math renderer to
text, with a plain-text view, and treat all archived content as untrusted data.

## Existing data and integration work

Verified against the working branch on 2026-10-17:

- `history/archive.py` retains original entries, scoped search and message reads.
  `history/record.py` keeps fold spans, source IDs, skeletons, narratives, states,
  stubs and overview parts; `history/view.py` represents the working view.
- `history/folds.py` emits transformation events. `history/usage.py` journals
  response usage with observation time and run identity. `ModelUsage` now records
  provider-reported cache-read and cache-write input counters for new main and
  delegated answers, preserving null when a counter was omitted or a call predates
  this change. The context-screen projection and reviewer/fold cache accounting
  still need integration.
- The workspace already exposes run records, message-by-ID reads, paged saved
  history collections and chunked tool records. These are building blocks, not
  a complete context-inspector API. The first read-only route pages retained
  original messages for one selected run attempt, or all recorded attempts of the
  same stable work, and searches their content and tool-call arguments in SQLite,
  including retrieval echoes the agent's FTS
  deliberately omits. Search is exact and case-sensitive, submitted explicitly;
  each request first bounds 64 session positions (plus one lookahead), then filters
  the selected work/role/kind and sizes candidates without loading bodies. This
  prevents sparse-agent queries walking an entire shared session. It scans at most
  64 rows or 8 MiB of text, apart from one content field of up to
  64 MiB. Tool-call JSON above 8 MiB is omitted from search and counted in the
  response; its original bytes remain accessible by message chunk reads. A
  continuation cursor advances a search
  even when that slice has no matches; the UI distinguishes partial from complete
  results. Each response returns short snippets and links to the original message.
  The message viewer falls back to exact 64 KiB byte pages for a body over the
  whole-message 1 MiB read limit, loading and displaying no more than 1 MiB per
  click. Derived
  fold/state records are excluded by default. Native provider payloads and
  separately stored tool-record bytes are not searched. A legacy run with no
  archive produces an empty page. Current context snapshots still need capture.
  The attempts endpoint pages immutable same-work
  ancestry and marks records that were never saved; those have no broken navigation
  link. Automatic search advances through up to 100 pages or 200 new matches per
  click, then offers cursor continuation. Stop, closing the dialog and changing
  tabs abort the active fetch; server SQL remains independently bounded. At most
  200 result snippets are retained in the dialog. Each match identifies its
  original attempt.
- Input occupancy, configured limits and fold estimates need explicit provenance
  and capture points before the graph can claim to represent actual live behavior.
- `ViewMeter` retains only its first 100 call estimates. Long-session charts need
  a durable bounded-page time series, not a graph drawn as though that sample were
  complete. The current chart pages the saved attempt's complete usage collection
  when the run record's 128 KiB inline preview is absent, and labels a partially
  loaded prefix. The Folds tab likewise pages archived summaries and reads
  oversized summaries through exact byte chunks (up to a 2 MiB display limit),
  and reports larger or unavailable records instead of saying no folds were saved.
  The chart uses only answered calls in the saved run record or archive,
  with gaps for absent input counts and a call-number axis. It cannot place folds
  on that axis or show reviewer/fold model usage and provider timing. Its cache
  share is input-token weighted over calls with reported cache-read counts;
  unavailable counters remain unknown, and writes are reported separately.

Production implementation must preserve project/session/run authorization and
bounded reads. It must not expose credentials or raw configuration through debug
metadata. Acceptance covers parent/child and continuation scoping, unknown usage,
cache accounting, fold-to-original navigation, archive paging/search coverage,
large outputs, dialog focus/close behavior and preservation of background drafts.

### Bounded live timeline slice

`GET …/runs/{run}/context/timeline` reads one authenticated attempt, including
an attempt whose final run record has not been saved. It pages the immutable
`archive_model_usage` answer journal by response ordinal and `run_events` by
event sequence. Each request reads at most 51 answer rows and 51 event rows
(one lookahead row per cursor);
the event slice is bounded before fold filtering, so sparse folds advance an
empty-page cursor without scanning an entire run. The UI plots up to the latest
500 loaded answer observations and fold events on a time axis, with an accessible
list and explicit partial-page coverage. The visible inspector polls one bounded
page every five seconds, resumes both cursors and checks for new observations;
manual refresh is also available. Hiding the page, closing the dialog, signing
out or switching tabs aborts pending reads.

`research_model_calls` supplies an independently displayed latest-50 auxiliary
intent snapshot. A partial index on project/session/run/created/identity,
excluding foreground calls, bounds this read. It is refreshed from the latest
rows rather than advanced by a random identity cursor: equal-time insertions
and delayed settlement of an already listed intent can change the snapshot.
The view explicitly says older auxiliary intents are outside the snapshot.
Its `created` timestamp is intent creation, not response observation. An
unsettled intent's usage is unavailable, never zero. Fold, reviewer, watcher,
skills and embedding usage are not added to the agent-input curve.

Label answer timestamps as response observations, never dispatch times. Fold event
times do not establish exact working-context occupancy; reductions remain
estimates. Keep legacy calls with missing timestamps on a call-number axis, and
retain explicit partial-page coverage. Do not combine auxiliary model calls with
the selected agent's main-input series or infer historical context ceilings.

Exact ordered working-context composition still requires a durable capture at
model-dispatch time: source references for prompt sections and folded/stubbed
regions, plus separately labelled estimated section sizes, keyed to the model
intent. The present journal/events cannot reconstruct that view. This capture
seam remains a separate harness change; until then the Current context tab
states that an exact snapshot was not saved.
