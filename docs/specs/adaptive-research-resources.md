# Adaptive research resource policy

Status: architectural direction requested by the owner, 2026-10-17. The renewable
grant protocol below is planned, not implemented. Existing fixed allowances remain
in effect until a reviewed implementation replaces them.

## Purpose

Extended investigations should not lose useful work merely because an initial time,
turn, token or storage allowance was conservative. A child warns its calling agent,
checkpoints its work and requests an extension. Policy decides which allowances the
parent may extend and the hard ceiling within which it may do so. This applies to
the orchestrator and every level of delegated work, including resumed attempts.
The routine check-in's purpose is to catch stuck loops, repeated errors and
unproductive spending; a healthy investigation should normally continue under its
orchestrator, even after an initial allowance is used.

## Resource classes

| Resource | Treatment |
| --- | --- |
| Model turns and cumulative token use | Renewable research allowance; account across all attempts of the same work identity. Report input, output and cached input separately where observed. |
| Active computation time | Renewable allowance; paused time is distinct from active work. A user-specified absolute deadline may remain hard. Transport timeouts, lease expiry and single-operation safety deadlines remain separate lifecycle controls. |
| Artifact retention | Renewable storage allocation backed by actual available storage; warn before a result becomes metadata-only. Durable references do not depend on an agent container. |
| Model context window | Automatic folding/compaction with reserved headroom. The model's physical maximum cannot be overridden by a parent. A folding failure checkpoints and reports a recoverable obstruction. |
| Total user spending authorization | Hard outer ceiling unless the user changes it. Parent grants share that ceiling; concurrent reservations prevent each child spending the same remaining budget. Unknown paid-call usage has a separate conservative reservation. |
| Security policy and physical capacity | Hard boundary. A resource extension cannot grant network/filesystem access, change isolation or create disk space. Active replay/RAM backstops must respect measured process capacity; they are distinct from archival storage quotas. Streaming, chunking and bounded readers remain required regardless of allowance. |

## Warning and extension protocol

1. Policy specifies warning thresholds, renewal eligibility, per-extension maxima,
   and a hard ceiling for each resource. The initial grant is not implicitly the
   hard ceiling. Warnings arrive with enough headroom to checkpoint and decide.
2. A child checkpoints and submits a durable resource request to its parent. It
   includes work/grant identity and revision, observed usage and reservations,
   verified checkpoint/artifact references, progress since the previous grant,
   remaining uncertainty, proposed next experiment, and requested resources.
3. The parent can extend, redirect, ask for a report, or stop the line with a reason.
   It judges progress from evidence, not just the child's assertion that it is
   productive. A negative research result can still be effective progress. A check-in
   is not a scientific-completion signal and must not close an active plan by itself.
4. The trusted control service validates the decision against policy, ancestor
   grants, aggregate reservations and capacity. It atomically records a new grant
   revision and an auditable reason. A child cannot grant itself more resources.
5. If an authorized decision cannot arrive before the allowance is exhausted, the
   child pauses at a safe point with its checkpoint and pending operations intact.
   It does not report scientific completion and is not discarded. An admitted long
   tool call keeps its original operation identity; extension or cancellation must
   use the worker's supported lifecycle rather than replaying that operation.

Resource requests wake a parent waiting for children without ending those children.
The parent's decision needs a reserved control allowance so the tree does not
deadlock because its last ordinary research turn was already spent. If its authority
is insufficient, it escalates to its own parent, ultimately to the user. An exhausted
hard spending ceiling never authorizes another paid decision call.

Requests and decisions are idempotent and scoped to the current work/grant revision.
Resume restores grants, warnings, reservations and pending decisions across the whole
tree. Stale attempts cannot extend or consume a successor's grant. Stop and revocation
remain authoritative. Rate-limited warnings avoid a loop of repeated extensions;
policy can require a changed approach or independent review after repeated renewals.

## Acceptance

- A productive child crosses its initial token/time allowance after its parent
  approves an extension, retaining the same work, checkpoints and provenance.
- A waiting parent receives a resource request and decides without canceling siblings.
- Concurrent children cannot over-allocate their parent's remaining hard budget.
- Repeated/no-progress requests can be declined or redirected with a visible reason.
- A denied extension pauses safely; granting it later resumes from the checkpoint.
- Process interruption at request, reservation and grant commit yields one decision
  and no duplicate tool execution after root-level Resume.
- Large artifacts remain downloadable after container deletion; previews and model
  excerpts stay bounded even when archival quotas are extended.
- A context fold succeeds in both parent and child during a live research run;
  failure is reported without silently canceling unrelated child work.

The durable local artifact backend and conservative unknown-usage reservations are
foundations for this protocol, not evidence that adaptive grants are already built.
