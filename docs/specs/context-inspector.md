# Agent context inspector

Status: requested by the owner, 2026-10-17; interaction design and local prototype
only. Per-answer cache counters are now durable for new main and delegated calls;
the production timeline, remaining telemetry and workspace API integration are pending.

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
  a complete context-inspector API. Full archive search, scoped timelines, context
  snapshots and run-attempt navigation need authenticated read-only projections.
- Input occupancy, configured limits and fold estimates need explicit provenance
  and capture points before the graph can claim to represent actual live behavior.
- `ViewMeter` retains only its first 100 call estimates. Long-session charts need
  a durable bounded-page time series, not a graph drawn as though that sample were
  complete. Existing archive search skips indexing history-retrieval echoes; the
  inspector's full-history search must cover retained original text independently
  of that agent-retrieval optimization, with explicit coverage during migration.

Production implementation must preserve project/session/run authorization and
bounded reads. It must not expose credentials or raw configuration through debug
metadata. Acceptance covers parent/child and continuation scoping, unknown usage,
cache accounting, fold-to-original navigation, archive paging/search coverage,
large outputs, dialog focus/close behavior and preservation of background drafts.
