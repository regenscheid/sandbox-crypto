# Agent context ownership

The 2026-10-21 refactor starts from remote `dev` at `391cb7a7`, following the
owner's development refactoring review. Both main and delegated agents already
fold through the shared `Folder` engine. This work consolidates ownership of
that behavior; it does not introduce another folding algorithm.

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
