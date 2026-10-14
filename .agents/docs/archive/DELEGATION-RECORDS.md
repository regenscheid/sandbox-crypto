# Internal delegation briefs and handbacks

> **Archived.** This code was removed from main; it is preserved on the tag `archive/delegation-2026-10`. See the [refactoring plan](../../refactor-plan/PLAN.md), decision 2.

Date: 2026-10-13. This records-only prerequisite supports the queued W4/D86
delegation work and confirmed [D81](../../../docs/DECISIONS.md). It installs no launcher,
model tool, prompt, route, worker action, approval or grant. A prepared record
does not prove dispatch or a running child. An unfinished intent retains an
unknown terminal outcome; replay does not authorize repeating possible effects.

The [plans/delegation specification](../../../docs/specs/plans-and-delegation.md) remains a
draft beyond its confirmed requirements. The record vocabulary below is an
explicit engineering increment, not adoption of all PD-11–22 defaults. No
concurrency/depth, one-correction-call, model fallback, cost, approval, question
or resume policy is enabled.

## Identity and authority

`DelegationRecords`
borrows an existing canonical `DocumentStore`. Every operation has separate
trusted attribution. `ParentAuthority` binds project, session, environment and
original parent run. `HandbackAuthority` additionally binds the original
delegation and generated child-run IDs. Construction is not authentication;
model JSON, a stored record or a reference claim cannot manufacture authority.
A future caller must derive it from its actual authenticated control context.

The parent binding cannot silently change environments. IDs are bounded ASCII;
the parent and child cannot impersonate USER or be the same run. Preparation
atomically allocates one delegation ID and child-run ID after checking replay.
No session record, workspace directory, environment or kernel is created. This
store is scoped to a logical session, not an opaque cross-request open lifetime.
Historical reads/replays confer no current open-session or execution eligibility.

Briefs retain task/context-summary text, bounded same-project reference claims,
an optional historical plan/track/step-fingerprint pin, requested profile/model
alias pins and explicit finite requested limits. These are intentions and data,
not applied settings, a skill loader, tool selection, current delegated-progress
grant, verified evidence or resource access. No parent transcript, private
endpoint/configuration or credential field is accepted. An unknown resolved
model stays null in the trusted host observation.

## Operations, raw data and immutable receipts

`prepare(authority, operation_id, brief)` records intent only.
`finish(authority, operation_id, expected_revision=1, observation, raw_payload)`
records one terminal host observation and the child's optional structured data.
`get(parent_authority, delegation_id)` returns the exact historical prepared
mutation, optional outcome and separate original raw bytes.
`list(parent_authority, after=0, target=None, limit=8)` pages a pinned parent-local
event prefix with actual continuation; another parent's activity creates no gaps.

Each immutable stored mutation is also its event and historical receipt. Its
action, operation ID, canonical request hash, original identity and scoped
sequence commit in one row. Exact replay returns that original receipt unchanged;
the same operation with different content conflicts. A terminal outcome cannot
be overwritten, and a finish operation cannot name another child. There is no
mutable current projection, ACK queue, dispatch state or automatic reconciliation.

Host `ended` is one of `returned`, `failed`, `cancelled`, `limit_reached` or
`interrupted`, supplied only through the separate trusted controller input.
Child `status` is `complete`, `partial` or `blocked`; summary, result pins,
deviations and open questions are all explicit required fields in
`sandbox-delegation-result-1`. They remain assertions. A child `complete` cannot
change a host failure, and a child `blocked` is not inferred to be a host failure.
Neither changes plan status, scientific findings, publications or permissions.

Original raw `None`, `b''`, invalid UTF-8 and malformed/duplicate JSON remain
distinct. The raw bytes are immutable; their exact length/hash enter the request
identity. The derived result is strictly validated from those bytes. Missing,
invalid or over-budget derived data stays null with a fixed validation code;
fields are never filled in and the host ending remains unchanged. No correction
model call is made. Reads rederive the result and bind every stored mutation's
operation/hash to its own authoritative fields. These are coherence checks, not
cryptographic authenticity against an administrator rewriting all database state.

`get` returns Python bytes, including arbitrary non-UTF-8 values. It defines no
HTTP/JSON transport for them. A later consumer must choose and bound a binary
representation explicitly; these bytes must not be implicitly decoded, executed
or rendered as trusted markup. Stored Markdown/LaTeX/source text is inert data.

## Storage, initialization and ownership

The constructor defaults to `initialize=False`: a wholly absent namespace is
reported not found. Explicit trusted first setup may use `initialize=True`.
Creation is transactional; concurrent initializers observe the same completed
schema. Partial schemas or unsupported versions refuse, including during explicit
initialization. Readonly callers never add this namespace or its schema. An
absent legacy namespace has no invented history. Complete removal of every
namespace table is not distinguishable from prior absence without an external
retained initialization proof; explicit initialization is not such a proof and
must not be used as automatic lost-history recovery.

| Bound | Limit |
| --- | --- |
| Canonical brief | 64 KiB |
| Original raw handback | 64 KiB |
| Canonical validated result | 8 KiB |
| Metadata per immutable mutation | 96 KiB |
| Reference claims per brief/result | 32 |
| Event page | At most eight items and 128 KiB complete canonical metadata |
| Default retained delegations | 256/project; 1,024/global |
| Default logical retained bytes | 64 MiB/project; 256 MiB/global |

These are storage safety bounds, not concurrent-agent defaults. Requested turns,
wall time and output-token bounds must be explicit and are not enforced runtime
budgets. Typed host copies are checked before overridable methods or aggregate
JSON encoding, with an exact UTF-8/escaping budget and finite depth/node counts.
Raw payloads refuse their type/size before write admission. SQL projects bounded
types and byte prefixes before Python parsing, preserving empty BLOBs explicitly.
Every read checks its row's recorded byte charge and raw/derived/request identity.

There is no unresolved eviction or compaction. One write transaction checks
retained counts and bytes before and after append. Every mutation/event/receipt
row, raw byte, stored variable column and fixed row reservation counts; the
namespace metadata has a fixed global reservation. Orphans and invalid retained
accounting refuse rather than creating capacity. These are logical accounting
bounds, not a claim about SQLite page/index allocation, RSS or arbitrary external
database growth. A full quota can refuse a terminal append while keeping its
original intent unresolved; callers must not invent a terminal result.

The store owns no connection, executor, thread, asynchronous task or close method.
All work is synchronous under the borrowed store's read/write transactions.
Cancelling a future does not prove admitted SQL stopped. A future async adapter
must retain actual queue/task/file/connection ownership and drain through timeout,
repeated cancellation and shutdown before closing its owner. No generic lifetime
or cancellation change is made here. The owner's existing lock serializes close
with a write already admitted under that lock; this is not a queue-drain guarantee
for future adapters or a terminal-close API.

## Required later integration and local proof

D81 requires each child to use its session's environment with its own kernels.
Today's `ExecutionContext` has no run-to-kernel identity, and the worker keys
kernels by `(session, language)`. A later explicit run/kernel protocol, lineage and
cancellation-scope increment is required before delegated Python/Sage calls.
Creating fake sessions to get separate kernels would violate D81. Later launcher,
role/tool restrictions, fresh plan/session checks, original handback prompts,
successful-model delivery, save/ACK, cancellation/restart reconciliation, GUI and
real worker acceptance remain separate.

Focused isolated control-store tests exercise exact identity and raw retention,
host/result separation, missing fields, replay/substitution, two-connection
concurrency, quota/rollback, corrupt-row prefetch, scoped paging, historical pins,
strict copied/subtyped inputs and actual admitted-write/owner-close ordering.
They start no model, provider, service, browser or VM and establish no live
delegated-run behavior. Combined gates, independent review and delivery belong
to root integration.

Local author validation: 56 new focused cases and 233 affected record/plan/document/
project/tool cases pass on Python 3.13. Whole-orchestrator typing, scoped Ruff and
format, locked dependency sync and the 75-distribution license check pass. The
first focused run retained 51 passes and one empty-BLOB projection failure;
the corrected bounded SQL explicitly preserves `b''`. Original unsuccessful
style/type logs and that source snapshot remain separate from final evidence.
