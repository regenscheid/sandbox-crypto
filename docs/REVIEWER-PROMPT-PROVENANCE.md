<!-- SPDX-License-Identifier: MIT -->
# Consistency-reviewer prompt provenance

Each new consistency-reviewer call reservation retains a bounded historical
rubric and ordered tool-schema pin. The feature identifies intended input; it
does not add review authority or prove that a provider received the call, obeyed
the rubric, or verified a scientific result. MAIN and watcher provenance remain
separate in [MAIN-PROMPT-PROVENANCE.md](MAIN-PROMPT-PROVENANCE.md) and
[WATCHER-PROMPT-PROVENANCE.md](WATCHER-PROMPT-PROVENANCE.md).

## Actual input

[consistency/prompt_bundle.py](../packages/orchestrator/src/sandbox_science/orchestrator/consistency/prompt_bundle.py)
assembles one owned reviewer bundle from the unchanged `RUBRIC`,
`RUBRIC_VERSION="sandbox-consistency-1"` and ordered five `SPECS` in
[runner.py](../packages/orchestrator/src/sandbox_science/orchestrator/consistency/runner.py).
It reuses MAIN's schema encoder (`prompts.schema_json`), without MAIN's sections.

The actual schemas, descriptions and order remain `review_manifest`,
`review_read`, `review_checked`, `review_finding`, `review_decide`. Each call
passes `SPECS` itself; the pins are recorded with the call's reservation and not
checked again before the call (owner decision 3). The existing tool dispatch whitelist, evidence grants, read audits, finding
operation replay, scientific decisions and confirmation CAS are unchanged.

The fixed first user instruction, dynamic evidence/tool results and final-format
retry instruction are unchanged and outside this system/tool pin. This is not
a conversation or complete provider-request hash. No alias, endpoint,
credential fingerprint, task, evidence, output or exception text is a new
envelope field. Requested alias and provider-observed `reply.model` remain
separate existing columns; a missing resolved model is not replaced by an alias.

## Historical envelope and bounds

The envelope contains format version 1, assembly version
`sandbox-reviewer-bundle-1`, fixed role `reviewer`, bounded rubric version, exact
rubric text and its UTF-8 byte length/SHA256, ordered tool names/count, canonical
schema byte length/hash and a bundle digest. The installed rubric is 2,450
UTF-8 bytes and fits inside the complete 16-KiB envelope ceiling.

Hashes use lowercase hexadecimal SHA256. Canonical JSON uses sorted object
keys, compact separators, UTF-8 without ASCII escaping and no non-finite values.
Arrays and tool order remain significant. The schema digest hashes canonical
ordered `{name,description,parameters}` projections after the byte domain
`reviewer-tool-schemas-v1\0`. The bundle digest hashes every envelope field
except `bundle_sha256`, after `reviewer-prompt-bundle-v1\0`. Each domain ends
with a literal NUL byte. Object keys are sorted for hashing only; the call gets
the schemas as defined.

The envelope retains exact historical rubric text, never substituted from
today's source. It retains **only a hash and names for schemas**, so historical
schema reconstruction requires the matching source/build record; the digest
alone cannot recover old parameter definitions or descriptions. These hashes
are content identity, not authentication or model-behavior evidence.

Installed input must be valid UTF-8 and representable: system at most 64 KiB
and 128 unique bounded tool names.
Metadata itself is capped at 16 KiB, with copied/constructed model fields
preflighted and revalidated before SQL. These are engineering bounds, not new
tool grants or a custom prompt loader. Malformed or oversized installed content
refuses before a model call; there is no custom-input fallback.

## Reservation and legacy reads

`review_calls` has a nullable `prompt_provenance` TEXT column
([store.py](../packages/orchestrator/src/sandbox_science/orchestrator/consistency/store.py)).
Internal callers may omit the optional pin, and the row then keeps NULL. An
UPDATE-OF trigger protects the input column while answered/usage updates remain
allowed.

`reserve_call(..., prompt_provenance=...)` validates and encodes before the write
transaction, then inserts the pin with the original reservation and counter
update atomically. The pin's rubric version must match the canonical review
header. The runner also refuses a nominal review rubric different from the
currently installed rubric. Historical reads accept bounded earlier versions
and their exact saved text, without claiming the current implementation used
those bytes for a new call.

The internal `call_provenance(project, session, review, call)` requires exact
scope and a valid positive reservation ID. It joins the call to its canonical
review and returns `absent`, `legacy`, `complete` or `corrupt`. Read-only databases
without the new column use a NULL projection without migration. A present
non-text, oversized, malformed or inconsistent pin is corrupt, never legacy.
SQL checks metadata type/byte length before Python/JSON allocation, and bounds
the nominal rubric version before comparison. No call history is added to a
public API, GUI, manifest or review header.

A committed reservation is a budgeted intended attempt, including when a crash,
renewal failure, cancellation, timeout or provider failure leaves `answered=0`.
Pins and charges are not erased, refunded or automatically replayed. Existing
hourly/per-review/token admission, duplicate-usage behavior and failed-window
recovery remain unchanged. At most 30 calls add at most 480 KiB of pin metadata
per review; this is not a global historical disk or RSS quota. No eviction or
new retention policy is introduced.

## Owned accounting and limits

The reservation, pre-call renew, provider-usage, finish and heartbeat-renew
awaits use the existing cancellation-owned `threaded` helper. Repeated caller
cancellation cannot abandon those actual SQL operations. Tests block reserve,
usage and finish, cancel repeatedly, and require the host/store-close boundary
to wait until the SQL work drains. Already completed accounting or review
outcomes remain durable; a late dropped finish cannot overwrite a terminal
outcome or a replacement owner.

This ownership covers the accounting awaits only. Raw tool, evidence,
model-observation, confirmation and controller thread paths keep their own
lifetime rules; this is **not** proof of whole-runner or whole-controller SQL
ownership under every cancellation race. No SQL transaction spans a model await.

Deterministic tests use a real local ReviewStore with scripted models to inspect
the committed pin at actual call entry, check exact schema/text/order, preserve alias/resolved-model separation, and exercise failure, timeout,
owner/counter rollback, concurrent budget, readonly migration, corrupt metadata
and repeated-cancellation drain. No model, worker, service, browser or VM is
started by these tests. Provider transformations, live model quality and a
prompt catalog are outside this feature.
