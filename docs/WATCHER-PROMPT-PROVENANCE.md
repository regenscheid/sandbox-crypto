<!-- SPDX-License-Identifier: MIT -->
# Watcher prompt provenance

The background job watcher retains its intended system and empty-tool input on
each durable call reservation. This extends the original watcher accounting in
[jobstate.py](../packages/orchestrator/src/sandbox_science/orchestrator/jobstate.py)
without changing its prompt, model routing, hourly budget, wake behavior or job
authority. MAIN prompt provenance is documented separately in
[MAIN-PROMPT-PROVENANCE.md](MAIN-PROMPT-PROVENANCE.md).

This is content identity for a reserved call. It proves neither network dispatch
nor provider receipt, model compliance, scientific validity or execution. A
pending reservation can survive a crash before the model is called. This feature
does not retry that call.

## Exact input and historical pins

[watcher_prompt.py](../packages/orchestrator/src/sandbox_science/orchestrator/watcher_prompt.py)
assembles one owned input for each due check. The system remains the exact
original `WATCHER_PROMPT` literal; its wording and whitespace are unchanged.
The two model messages remain system then untrusted job data. The actual model
call still receives the literal empty tool tuple `()`. The owned messages and
zero tools are verified immediately before the call.

The fixed envelope contains:

- `format_version=1`, `role="watcher"` and local prompt version
  `sandbox-job-watcher-1`.
- Exact historical system text, its UTF-8 byte length and SHA256.
- `tool_count=0`, ordered empty `tool_names=[]` and the empty-tool digest.
- A digest covering all the preceding fields.

Hashes use lowercase hexadecimal SHA256. Canonical envelope JSON uses sorted
keys, compact separators, UTF-8 without ASCII escaping, and disallows non-finite
numbers. The envelope digest prefixes that JSON, excluding `envelope_sha256`,
with bytes `watcher-prompt-envelope-v1\0`. The empty-tool digest hashes bytes
`watcher-tool-schemas-v1\0[]`. The domains include a literal NUL byte.

System text is limited to 2,048 UTF-8 bytes and the complete encoded envelope to
4,096 bytes. Neither is normalized or clipped. Copied or constructed models are
revalidated before reservation, including scalar types, role/version, empty
tools and all hashes. Oversized system text is refused before serialization.
These bounds are implementation choices for the short installed watcher
literal, not a general prompt catalog or approval policy.

The dynamic user message is materialized once but is outside the new digest.
The existing `input` column continues using `json.dumps(payload)`; the actual
user message continues using compact Unicode JSON from `worker_client.dump`.
Their bytes differ. This feature does **not** claim a whole-call, payload or
conversation hash. Job text, output, aliases, model names, endpoint credentials
and exception text are not new envelope fields. Existing alias, resolved-model
and usage columns remain separate. Missing model replies retain no resolved
model rather than substituting the configured alias.

## Reservation, migration and reads

`JobState.reserve(..., provenance=...)` validates and copies the envelope before
opening the write transaction. It inserts the pin with the existing pending row
inside the same `BEGIN IMMEDIATE` as due-time deduplication and hourly budget
admission. A failed insert leaves neither a partial pin nor a charged row.
All successful reservations still count toward the existing project budget,
including pending or failed calls.

`job_watcher_calls` has a nullable `prompt_provenance` column. Callers omitting
provenance leave it NULL.

The internal `watcher_provenance(context, call)` read requires an exact valid
project/session scope and positive bounded reservation ID. It returns:

- `absent`: no row in that scope, including foreign reservation IDs.
- `legacy`: the row has no retained provenance.
- `complete`: the bounded historical envelope validates.
- `corrupt`: malformed, non-text, invalid UTF-8, oversized or inconsistent pins.

SQL checks type and UTF-8 byte length before fetching at most one envelope.
Oversized metadata does not cross into Python or a JSON parser. Corrupt pins
are not repaired from today's source. This read is internal and adds no public
route, GUI response field or authority grant.

## Failure and delivery boundaries

`finish_call` changes only the existing outcome/model/usage columns. The reserved
pin survives successful, invalid, missing, oversized and tool-returning replies,
ordinary model failure, timeout and cancellation. Ordinary failure retains the
existing behavior: first write `invalid_or_failed`, then replace the outcome
with fixed needs-attention JSON. Cancellation records the failure marker and
propagates. This outcome column remains mutable call accounting, not an
append-only history of every intermediate outcome.

Cancellation while the reservation thread is running retains the existing
thread semantics: a reservation may commit after the awaiting task is
cancelled. Its pin still describes intended input; it does not prove dispatch.
No automatic retry, extra call or job cancellation is introduced. Job ownership,
completion priority, user-message interruption, checkins and MAIN's durable
run-save-before-notice-ACK boundary are unchanged.

The new overhead is bounded **per admitted call**. The existing watcher journal
has an hourly admission budget but no total historical row/byte quota. This
feature adds no global retention claim, eviction, scheduler or metadata-driven
budget policy.

## Tests and scope

Deterministic local tests exercise the actual watcher boundary with a separate
SQLite connection observing the committed pending pin before the scripted model
returns. They check exact system bytes, zero tools, Unicode/untrusted job input,
preserved attribution, fixed failure/wake and cancellation behavior. Store
fixtures cover pending restart, original historical text, concurrent legacy
migration, competing reservations, rollback, scope denial, invalid copied models
and bounded refusal of 20-MiB corrupt metadata.

These tests use local scripted peers and synthetic state. They do not execute
models, workers or research code, or establish provider wire transformations or
model quality. Dynamic payload hashes, global retention and prompt loading are not
built; reviewer and display-repair provenance have their own pages.
