# Explicit research-tree Resume

Implementation contract for RC-1 through RC-13 and RC-F16. Automatic crash restart
remains disabled. This document describes the implementation target, not completed
runtime acceptance.

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
Gateway enforcement remains authoritative. A new model call requires current budget
admission and explicit Resume, not an assumption that the lost request was free.

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
The same inactive-work, original-attempt, current-epoch, exact-identity and
idempotency checks as other recovery resolutions apply; changing the decision or
switching between observed usage and reservation conflicts.

The reservation permits reconciliation to continue, but does not issue Resume or
widen the original grants. Its cost counts against the project's daily admission
budget using the original dispatch time, separately from observed-spend reporting.
Its input/output bounds reduce the original child token grant across all attempts;
a spent grant or project budget still blocks continuation. Unknown calls without
an explicit reservation still block. Status retains unknown actual usage and
shows the reservation/operator evidence separately. This does not recover a lost
response, determine the actual provider charge or authorize replaying the lost call.
