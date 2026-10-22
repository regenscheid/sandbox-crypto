# Agent context ownership

The 2026-10-21 refactor starts from remote `dev` at `391cb7a7`, with the subsequent
`dev` recovery update `1ad689e3` merged into this branch, following the owner's
development refactoring review. Both main and delegated agents already
fold through the shared `Folder` engine. This work consolidates ownership of
that behavior. The follow-up separates the folding engine from execution and
refines retention without changing the archive or folding record formats.

## Package boundary

`orchestrator/context/` owns conversation preparation, active context state,
archived history, scoped retrieval, folding, and diagnostics. Existing archive,
folding and capture implementations live in its `history/` subpackage. The
package initializer remains small to avoid importing the execution loop through
tool registration.

An `AgentContext` owns messages, prompt provenance, model usage, the rebuilt
working view and its offsets, archive-first appends and bounded working storage.
The execution transcript owns turns, stop reasons, accepted answers and delivery
acknowledgement policy. Existing turn hooks keep their observable order.

Context preparation selects main-session history or the delegated attempt's
history, pins the child brief and constraints, and constructs folding, logging
and retrieval from the same trusted scope. Retrieval visibility can include a
child's descendants; the automatically prepared child prompt remains scoped to
its own attempt. A model cannot choose or broaden that scope.

The confined runtime imports rebuilt context through an explicit state operation.
The host retains authority and validates committed message and usage counts.
The transport schemas and canonical archive formats remain unchanged. Captured
dispatch inputs are still committed as part of paid-call admission, not during
initial context preparation.

## Implementation stages and verification

1. Move the existing history implementation under the context package and update
   callers, without changing behavior.
2. Extract active context state, message appends and model usage; route the loop
   and confined runtime through that owner.
3. Extract scoped preparation and connect history tools to that prepared scope.
4. Review the integrated paths and verify main, delegated, resumed and confined
   inputs, archive-first writes, prompt ordering, fold cleanup and retrieval
   isolation through existing public characterization tests.

Attempt finalization and workspace request execution are parallel changes from
the same review. They have separate commits and implementation owners. Worker
lifecycle, capability bundles and the remaining candidates remain later work.

## Reading the implementation

| Responsibility | Owner |
| --- | --- |
| Scope, initial root/child view, digest and constraints, reconstruction after Resume | `context/preparation.py`, `HistoryScope` and `PreparedContext` |
| Working messages, replay input, prompt pins, usage, fold offsets and archive-first appends | `context/agent.py`, `AgentContext` |
| Context-only folding contract and ordered cleanup adapter | `context/folding.py`, `FoldPoint`, `FoldLifecycle` |
| Background folding and enrichment, overflow handling and learned ceilings | `context/history/folds.py` and `enrich.py` |
| Complete archived messages, bounded reads, search and descendant visibility | `context/history/archive.py` and `tool.py` |
| Captured dispatch composition, timeline and inspector indexes | `context/history/capture.py`, `timeline.py` and `inspector.py` |
| Turn scheduling, tools, accepted answers and delivery decisions | `loop.py`, `Transcript` composed with `AgentContext` |

The execution transcript has no context forwarding properties. Hooks read
`RunView.context`; folding receives `FoldPoint(context, call_index)` and never
imports the execution transcript. Internal callers construct
`Transcript(context=AgentContext(...))`.
`AgentContext.start` initializes archive-backed or confined scratch collections;
`append` commits before exposing a message, `complete` carries prompt pins to the
model, `record_reply` records answered usage, and `rebuild`/`restore` replace the
working view while retaining original history.

`PreparedContext` supplies the folder, message log and history tool from one
admitted scope. Model-role selection and tool assembly remain harness responsibilities.
`FoldLifecycle` occupies the folder's existing ordered hook slot. It settles
background enrichment and canonical saves before final summaries, and resets
overflow tracking only after the answered response is committed. The confined
runtime forwards context operations; only the host owns this engine lifecycle.
Store lifetime stays with its existing caller. Root preparation now passes the configured clock into the folder,
as child preparation already did; the production default remains the system clock.

The structural extraction changes no database version, archive wire format or
confined protocol schema. It does not launch research or migrate running services.

## Folding retention policy

The working context has three jobs: retain important takeaways, leave exact retrieval
anchors for omitted bulk, and preserve recent conversation verbatim. Narrative folds
prioritize findings and their scope, standing constraints, assumptions, failed
approaches and their stated reasons, unresolved questions and identified next steps.
They distinguish hypotheses from conclusions. Old tool output and document passages
are retrieved from canonical history instead of copied into every overview.

At each fold, stubbing protects complete recent assistant/tool-result units covering
the configured raw floor, in addition to the newest five results and all history
retrievals. Crossing the floor keeps the whole unit, even when its output is large.
Old saved folds remain unchanged. The model summary is lossy and fallible: exact
values remain in the original archive, with searchable keys and message pointers.

Overview validation scans the original scoped span in bounded text pages, rather
than loading all covered messages and native replay payloads. Candidate facts still
need original evidence; a prior summary is not evidence for a new value. A fragment
that cannot be proven within bounded validation is omitted with a diagnostic, not
accepted or turned into a new stop of the research run. Summarizer instructions
budget narrative space after mandatory record content; no room means a documented
code fallback without a paid summarizer attempt.

## Local verification

The first integrated context/finalization suite passed 426 tests; the full workspace
suite passed 855. The follow-up passed 743 tests covering context, confinement, resume,
changed consumers and the research-state hold path. Independent agents reviewed the
engine boundary and evidence validation, including fixes for large numeric arrays
and signed values split across pages. The integrating agent reviewed the delivered
code and ran the combined checks. Final validation details are in the report below.

Broad testing also exposed pre-existing dev fixture and SIGTERM failures. The
[validation report](../../.agents/docs/agent-reports/context-refactor-2026-10-21.md)
distinguishes those from corrected integration failures and records the remaining
qualification limits. No live gateway, OpenShell process or paid model was used.
