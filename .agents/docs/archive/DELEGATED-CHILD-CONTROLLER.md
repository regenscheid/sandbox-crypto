<!-- SPDX-License-Identifier: MIT -->
# Explicit restricted child conversations

> **Archived.** This code was removed from main; it is preserved on the tag `archive/delegation-2026-10`. See the [refactoring plan](../../refactor-plan/PLAN.md), decision 2.

This internal callable connects the original [delegation records](DELEGATION-RECORDS.md),
[child-attempt journal](CHILD-ATTEMPTS.md), [own-track adapter](DELEGATED-PLAN-PROGRESS.md)
and authenticated selected-run transport. It installs no launcher, parent/child tool,
CLI mode, scheduler, model profile, role policy, fallback or automatic activation.
MAIN prompts, stock templates, grants, queues and save/ACK behavior are unchanged.

`ChildController(journal, records, worker, applied_model, configuration, plans=...)`
borrows those owners. `run(authority, prepared, original_session, operation_id,
admitted=...)` requires the original canonical prepared child, explicit controller
attribution, original held session snapshot and fresh cooperative controller admission.
Construction and DTO hashes are not authentication. The production selected-worker
routes independently authenticate the explicitly enabled controller client; the worker
trusts that controller's canonical attribution and cannot read the control database.
There is no atomic cross-host revocation guarantee across an already admitted request.

`ChildConfiguration` supplies the nonsecret requested profile/alias, all journal budget
fields and the exact positive tool subset. `AppliedChildModel` borrows a `ChatModel`
and explicitly states the caller-applied per-call output allowance. It must match the
reservation allowance. `ChatModel.complete` has no token-limit argument, so this is a
trusted composition assertion, not private model introspection or a verified provider
hard cap. Non-None monetary enforcement is unsupported and refused. Nullable actual
reply model/usage is retained separately; requested alias never fills missing evidence.
Output usage above the reservation stops further work. Reserved calls/output/input
are not refunded, and unknown usage does not establish zero spend.

All cell, transport, poll and model budgets are explicit finite supplied inputs.
`calls_per_turn=1` is the only supported explicit execution ceiling. A model turn with
more calls receives bounded per-call refusals with zero dispatch. This ceiling does
not define global delegation concurrency, depth or policy defaults. Cleanup/actual
owned work can outlast the response wall budget; cancellation is not proof of provider
or remote-worker noncommit.

## Restricted actual inputs and dispatch

`child_prompt.py` supplies original versioned delegated instructions, independent of
MAIN prompt/provenance assembly. The journal retains the exact system, full logical
message prefixes and ordered owned tool/schema bytes. Each invocation gets fresh
schema/message projections, checked at the actual model boundary. These are logical
host input observations, not provider-wire, spend or scientific-verification proof.
The child sees its original brief rather than the parent's transcript/configuration.

`child_tools.py` checks both the offered-name subset and the positive handler table
before parsing. Available names are selected `python`/`sage`, `plan_read`,
`plan_history`, `plan_progress`. Restart is intentionally absent in this first slice;
there is no generic Toolbox or legacy execution fallback. Arguments contain research
values and exact plan cursors, never scope, controller, binding or actor overrides.
The operation identity is host-derived from the original attempt/reservation/call.
Duplicate JSON keys, oversized/invalid copied inputs, invalid counters and foreign
selected replies refuse. Child schema counters fit the existing journal's smaller
logical integer ceiling; core/MAIN schemas are not changed.

When a plan/track exists, the supplied adapter must be the exact original child's
current explicit grant, canonical store and requested alias. Complete current reads
include only the granted track/steps. History/replay cannot satisfy a reread. A fresh
successful actual call containing the exact complete read is required to clear its
host-only correlation; same-batch reads, historical replies and restart cannot clear
it. USER changes and exact status/step CAS remain guarded by the adapter's actual
transaction. Current plan text/evidence remains data, not execution authority.

## Durable ordering and unknown outcomes

A fresh controller invocation has a unique internal mutation identity, retained with
the caller operation as host observation data. Any already allocated child attempt,
even Begin-only, conflicts before enrollment/model/effects. Concurrent controllers
cannot use journal replay to repeat work. Explicit scoped journal inspection remains
available; automatic resume/retry/takeover is absent.

The controller commits remote enrollment intent before contacting the worker, and
commits each full model reservation before calling the model. It rechecks original
admission after those awaited writes at the actual call boundary. Returned logical
responses are retained before tools. Failure/timeout/cancel/retention failure leaves
an explicit unknown reservation; it never becomes proof that no call happened.

Before a selected cell or progress write, the exact complete request, including its
host operation, original binding, timeout/output settings or plan/status cursors, is
durably retained. Admission is checked again after that write. A cell's initial TOOL
response is the durable intent. Accepted execution/attempt/binding IDs and terminal
results are separate explicitly labeled trusted-host observations in protocol
`role=user` messages; they are not human USER instructions or grants. Their exact
logical content is saved before polling or advancing. This preserves valid one-call
message ordering without removing a sticky journal hold.

Lost submit/control acknowledgment, state loss, LOST/timed-out/cancelled cell state,
missing result or ambiguous selected response stops further model/effects. Known
accepted uncertain executions retain sticky exact effect pins. A crash after accepted
IDs but before a terminal observation still leaves an existing attempt that cannot be
reattempted by a new controller. There is no implicit resubmit or MAIN fallback.

Selected cleanup has a durable original-binding intent and bounded typed receipt.
`done`, `pending` and `unknown` are distinct; wrong/foreign/malformed receipts cannot
certify cleanup. A done receipt is the original selected control observation, not VM
teardown or new-incarnation proof. No whole-session close is used for one child.

The local journal seal commits before `DelegationRecords.finish(expected_revision=1)`.
The host ending, nullable actual reply model and bounded raw/strict child handback
remain separate. A child's `complete` assertion does not prove scientific success or
finish the session plan. Failed canonical-save acknowledgment stays qualified; the
immutable original handback store owns reconciliation. No MAIN notice ACK is inherited.

## Ownership and validation limits

One controller is single-use. It owns its actual model/tool tasks and synchronous
journal/store threads through repeated cancellation, then propagates the original
interruption. `close()` withdraws new work and drains that same owner. It does not
close the borrowed worker/model, plan adapter, canonical stores or original snapshot
FD. Callers must retain those owners until run/close have actually settled.
Late bounded response/checkpoint/cleanup/seal facts may be retained after revocation,
logical close or canonical handback; they cannot reserve work, restore grants or clear
model delivery. No project/SQL lock spans model or worker awaits.

Focused tests use real disposable canonical stores, the production controller and
own-track adapter, scripted model returns and selected-only worker fakes. They cover
actual-input/reservation and intent ordering, during-run progress, USER races, unknown
holds, historical/crash refusal, copied-response bounds, cleanup receipt distinctions
and actual ignored-cancellation/thread drainage. They do not establish real HTTP
composition, OpenShell behavior, real-model compliance, provider caps or scientific
correctness. Root owns integrated gates and finite authenticated HTTP/OpenShell
acceptance; any provider pilot needs explicit existing configuration and budgets.
