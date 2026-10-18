# Research-tree Resume

Implementation contract for RC-1 through RC-13 and RC-F16. Automatic crash restart
remains disabled. A bounded control-service scheduler can queue the same durable
Resume request after a saved model-phase failure. This document describes the
implementation target, not completed runtime acceptance.

## Identity and ownership

A work item's identity is its original run name. A continuation has a new run name,
linked to that work and its predecessor. Root/parent work, role, original inputs and
stable child-kernel scope never change. An attempt's archive and outcome are immutable.
New roots and children register before agent/model effects; child admission records
its recovery blueprint before scheduling its coroutine.

The root owns a monotonically increasing tree epoch. One session lease admits a tree
owner; an explicit Resume atomically claims its durable request and advances the
epoch before any branch is dispatched. Every attempt carries the epoch. Trusted
state writes check it inside their transaction. The trusted control service installs
the epoch at the worker, and worker effect admission rejects older epochs. Already
admitted effects remain obligations to reconcile. An old runtime cannot mint a new
epoch or capability. Legacy unowned calls are refused once a session is fenced.

Claiming Resume atomically queues held, unconsumed chat offered to unfinished work
in the selected tree. This survives interruption immediately after the operator's
durable request, before a coordinator exists. Other trees, terminal branches and
canonical consumed receipts are excluded. The workspace does not globally unhold
chat for a tree Resume; legacy sessions without durable work retain that fallback.

Work states are running, waiting, paused, blocked, completed and stopped. A failed or
limited attempt pauses work; a missing result blocks it. A completed parent has no
unfinished descendant or unconsumed child outcome. Stop records a subtree intent
before cancellation, and stays stopped across continuation attempts. Stopped children
are reported to their parent; Resume never restarts them implicitly.

## One action and recovery ordering

The existing session Resume action queues an idempotent durable request for the
latest unfinished root. A selected work API may narrow this to a subtree. No held
chat message is required. Ordinary message requeueing retains its existing behavior.
The service gives Resume requests priority over ordinary admission in that session.
A durable response reports counts and paged branch states/reasons, including blocked
branches; it never calls an incomplete tree successfully resumed.

The control service also checks saved paused roots for model-phase failures. It may
queue one Resume for a root whose unfinished branches are only model failures or
parents waiting on them. The request and a small retry observation commit together;
another service sees the same request after restart. Without a new completed work
item or recorded model answer, at most three automatic requests are made, separated
by 30 and 60 seconds; later recovery needs an explicit owner action. The scheduler
defers to a newly queued user message so a correction can be handled first. It
does not create new work, reset grants, re-dispatch a tool, or override the normal
cost, policy, environment and kernel checks. It leaves stopped/cancelled work,
spent token grants, uncertain tool effects and other failures for explicit handling.
Unknown model charges remain visible but do not by themselves prevent this bounded
continuation. Its positive classification is intentionally narrow: a saved `error`
attempt must name failure phase `model`. The scheduler does not infer that every
model error is transient; bounded retries contain persistent configuration errors.

An `answered` root with an active scientific plan is still a checkpoint that
requires a separate continuation feature. That feature can share the control
service's durable admission and session lease, but must create a new root input
from the current plan and evidence rather than falsely reopening completed work.
Automatic recovery after process loss, lost kernels, worker failures, and explicit
budget/grant decisions also remains open.

Recovery traverses the durable tree in bounded pages. It first reads explicit Stop,
then current policy, project/environment identity, model and pinned dependencies,
spending and pending operations. Only after reconciliation does it schedule eligible
continuation attempts. Current policy may be stricter; it cannot silently widen the
recorded grant. Unavailable pins or changed dependencies produce a visible blocked
branch. Resuming supplies a new turn allowance, but cumulative work tokens and all
recorded project spending remain charged.

A completed child is read and delivered to its parent, never recomputed. A limited
or interrupted child retains its original brief and role. Independent-check history
contains only its own predecessor chain, never parent or sibling reasoning. A parent
may run to handle available outcomes while other branches are blocked, but cannot
complete until those branches are resolved or explicitly closed with a reason.

## Effects, kernels and delivery

Every pending tool intent is matched to adapter-specific operation identities before
new effects. Supported worker operations are queried by their original idempotency
key: running means wait/reattach, terminal means archive the observed result, and an
unknown outcome blocks the branch. An unsupported external/MCP effect remains unknown;
its old arguments are never automatically executed again. Explicit reconciliation
must name authoritative evidence or acknowledge the uncertain effect; elapsed time
is not proof that nothing ran.
The live tool path uses the same distinction: a definite application refusal becomes
an error the agent can correct; a lost worker or MCP reply leaves the original
intent pending and ends the attempt. A child failure reaches its parent as a bounded
outcome notice with the trusted failure phase, while the unresolved operation remains
blocked for orchestrator Resume. Neither parent nor child invents a successful result.

Observe sandbox and kernel identity/generation without waking them. Reuse the stable
child kernel scope only when its saved observation matches. A lost kernel is a visible
blocked recovery condition unless a recorded checkpoint can be restored or the user
explicitly authorizes recomputation. No automatic kernel restoration is claimed.
Paused work protects its kernel from the harness's orphan sweep; worker idle-retention
claims still expire under their separate policy and do not promise indefinite RAM.

Response delivery receipts commit with the assistant response and usage (the memory
storage contract). Recovery uses those source/notice identities to acknowledge inputs
already consumed before a crash. Child outcome versions are delivered once per parent
work, across attempts. Pending questions and received answers retain their identities;
an interruption does not discard an unanswered question or re-ask an answered one.

Model intent is recorded before a paid dispatch. An intent without an observed response
is shown as an unknown outcome/charge; it is never represented as a zero-cost call.
Gateway enforcement remains authoritative. Explicit Resume may continue past this
unknown charge under the gateway's spending control. It does not replay the lost
model response or erase the unknown charge. Original tool effects still require
reconciliation before their branch can continue.
If an earlier attempt has no saved starting-input snapshot, recovery logs the
provenance gap and records the continuation's current prompt/tool/skill pins;
missing historical pins alone do not block. A known mismatch with saved pins
still blocks under the current pinning policy.

## Acceptance

Production service/CLI/API fixtures must exercise an interrupted root, child and
grandchild, a completed child, an explicitly stopped branch, an unavailable environment,
concurrent Resume, stale owners, delayed effects, lost final saves and received user
answers. They must show one action resumes eligible work and reports blocked branches,
with no duplicated operation or consumed outcome and no role/context widening.

Worker fencing tests cover delayed admission after an epoch change and already-running
operations retaining their identities. Actual OpenShell replacement must additionally
verify surviving execution reattachment, explicit lost-kernel reporting and durable
UI history. Deterministic tests alone do not establish that runtime acceptance.

### Conservative reservation for an unknown paid call

An authenticated operator may explicitly resolve a still-unknown model intent with
`model_reservation`, supplying a positive monetary upper bound and positive input
and output token upper bounds, with a reason. This is a budget reservation, not an
observation: the original call's actual usage remains NULL/UNKNOWN and no assistant
or tool response is synthesized. The archive records that distinction visibly.
The same original-attempt, current-epoch, exact-identity and idempotency checks
as other recovery resolutions apply; accounting-only model decisions may also
be attached after work completes, while effect resolutions still require
unfinished inactive work. Changing the decision or
switching between observed usage and reservation conflicts.

A cancelled fold can leave an estimated usage copy in its run collection. When the same run has a sole durable fold intent with observed usage or an exact reservation, this copy is diagnostic rather than a second unpriced charge. An unsettled or ambiguous copy retains its unpriced status.

The optional reservation gives local cost/token estimates a conservative bound, but
does not issue Resume or widen the original grants. Its cost counts against the project's daily admission
budget using the original dispatch time, separately from observed-spend reporting.
Its input/output bounds reduce the original child token grant across all attempts;
a spent grant or project budget still blocks continuation. Unknown model calls without
an explicit reservation remain visible and do not alone block Resume. Status retains unknown actual usage and
shows the reservation/operator evidence separately. This does not recover a lost
response, determine the actual provider charge or authorize replaying the lost call.
