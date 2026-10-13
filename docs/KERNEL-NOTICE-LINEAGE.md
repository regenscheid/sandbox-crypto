<!-- SPDX-License-Identifier: MIT -->
# Passive kernel lineage for notices

The worker has a separate, bounded observation route for the internal CP-12
notice consumer. It reports an existing kernel with its exact retained original
birth, or explicitly unknown lineage. It does not enqueue or deliver notices,
inject agent messages, wake idle sessions, restart kernels or complete CP-12.
The existing GUI observation and flattened workspace response are unchanged.

## API and proof boundary

`POST /v1/kernels/lineage` accepts the existing `KernelStatusRequest`, containing
the authenticated project, environment and session context. The normal worker
project/environment checks apply before dispatch. `WorkerClient.observe_kernel_lineage`
returns `KernelLineageObservation`: that exact scope, nullable `current_revision`,
an aware worker receipt timestamp and at most two `KernelLineageEntry` records.

Each entry contains the existing `KernelInfo`, `lineage_status`, an optional
`KernelBirthPin`, and an optional finite `unknown_reason`. A `proved` entry requires:

- The retained original project, environment, session and language match the request.
- Its exact daemon incarnation, kernel ID, birth ID and raw executor generation
  match the live reply on the unchanged connected channel.
- Its original managed sandbox name and immutable ID match a single bounded,
  incarnation-paired endpoint record and the synchronous trusted managed identity
  available after the reply. There is no CLI, VM lookup or lifecycle action.
- The saved worker generation mapping belongs to that incarnation and maps the raw
  generation to the separately reported normalized generation.
- The original known start revision equals `KernelInfo.start_revision`.

`proved` describes retained original birth identity, not the truth of loaded module
state or continued VM health after receipt. Birth metadata is executor observation,
not authority supplied by a model. A missing, damaged, contradictory, legacy or
unmanaged binding stays unknown; today's endpoint or package state cannot fill in
an original birth. Unknown entries carry no partial birth pin. Damaged generation
metadata or invalid live kernel identities can make the whole observation
unavailable. SQL prefixes bound retained birth and endpoint fields before Python
allocation; the route neither repairs nor overwrites damaged records.

The original start and current revision are separate. A valid original birth can
remain proved while `current_revision` is unknown; its revision comparison is
then unknown and a notice consumer must hold rather than infer staleness.
`observed_at` is host receipt time, not an atomic snapshot or a session-open
lifetime. These observations grant no package, execution, download or lookup
permission and do not bypass CP-13 recovery admission.

## Identity and lifecycle

`KernelBirthPin.generation` is the raw executor-local generation. The accompanying
`KernelInfo.generation` is the worker's normalized monotonic generation. Both are
required by `KernelLineageIdentity`; neither can replace the other. Its
`kernel_lineage_sha256` helper hashes sorted, compact ASCII JSON under the exact
domain `kernel-lineage-v1` followed by a NUL byte. The key includes the complete
original birth and normalized generation. Current revision and receipt time are
excluded. Worker endpoint/configuration identity and the control-owned session-open
lifetime belong to the trusted consumer scope, outside this worker birth key.

A same-incarnation worker restart can retain an already recorded exact birth and
its key. First observation after an unseen start does not establish the original
start revision. An explicit successful kernel restart creates a new birth/key.
The retained JSON schema and the existing atomic kernel-start transaction are
unchanged; this route only reads them.

The route and ordinary `/v1/kernels/observe` share one immediate admission lane:
eight active observations globally, one per environment, no queue and the same
bounded session-close token. They use the same single read-only `kernels` RPC and
two-second whole-call worker deadline, including transport write-lock acquisition.
Neither route opens, reconnects or recycles an executor, refreshes activity,
increments the busy counter, holds a lifecycle lock, persists provisional births
or consumes `state_lost`. Idle release, replacement and session close can win
while the RPC is pending. Post-reply object/channel/incarnation checks reject old
responses, including empty replies. A completed matching close invalidates the
old active token; a fresh post-close call can observe the empty session.

Disconnected, detaching, recovering or lifecycle-fenced channels are unavailable,
not empty. A readable CP-13 package hold remains readable and unchanged. Legacy
executors lacking scoped kernel status are unsupported. The client uses only this
route, an 8 KiB success/error cap, five-second total HTTP deadline and no redirects
or lifecycle-active status fallback. It verifies exact returned scope and typed
birth/kernel/current-revision coherence.

## Current MAIN consumer

The independently reviewed [explicit stateful MAIN consumer](KERNEL-NOTICE-DELIVERY.md) now implements authenticated control lifetime/configuration mapping, bounded owned passive reads, historical version-1 USER input and typed receipt/RunRecord evidence. Successful host return marks delivery; a real fsynced run and fresh original proof precede ACK. Failed first calls, failed saves and cancellation retain the qualified replay/audit boundary. Changed bindings, incomplete lineage or initialized inbox loss refuse rather than re-enroll. [OpenShell Lab29](../.agents/docs/agent-reports/babbage-openshell-lab-29-2026-10-13.md) passes actual worker/package/idle-resume acceptance and independent complete cleanup; PR #109 is delivered at `cef64a0` after independent final archive review and all four fresh checks. No idle scheduler, automatic replacement reconciliation, delegate routing or full-history guarantee is added. The prerequisite evidence below remains at its original source pins.

## Historical prerequisite validation

Focused local tests use disposable existing executor backends, including real
Jupyter kernels and authenticated HTTP composition. They cover retained globals
and birth keys across daemon reconnection, explicit restart, unknown current
revision, original-binding changes, missing/corrupt/oversized proof, strict identity
and counter validation, shared idle/close/rebind fences, cancellation/admission
cleanup, and client scope/byte/redirect/deadline refusal. Synthetic managed names
in those fixtures are test bindings, not VM attestation.

The source milestone passed 139 focused Python tests on Python 3.13, affected Ruff
and formatting checks, and repository Pyright with zero errors. Full repository
tests were intentionally left to the combined delivery owner.

Root-owned [OpenShell Lab28](../.agents/docs/agent-reports/babbage-openshell-lab-28-2026-10-12.md) passed on signed `1deb9b7`, with independently verified complete teardown. Signed prompt-integrated union `840b48f` passes 10,155 Python cases/43 opt-in skips/two existing warnings and required static checks; unchanged 614 web/24 math/builds are explicitly reused. Unknown/missing/corrupt/legacy cases remain local-fixture coverage. Fresh exact-head PR CI remains a delivery gate. No services, VMs or models were started by this source
author. At that historical source the companion did not wire a consumer. The later MAIN integration above independently binds the configured worker/session lifetime and preserves unknown holds and ordinary save/ACK authority; idle scheduling and automatic replacement reconciliation remain open.
