<!-- SPDX-License-Identifier: MIT -->
# Producing display-repair input identity

The existing math-repair response can include optional `input_provenance` for
the static host inputs of its producing model call. This descriptor travels with
the answer and its process-local cache entry. It is not a provider dispatch or
spend receipt, durable call history, scientific validation or source attribution.
The browser may ignore the additive field; no history view or new route is added.

## Exact static inputs

[repair_prompt.py](../packages/workspace/src/sandbox_science/workspace/repair_prompt.py)
owns an immutable strict descriptor, at most 4 KiB encoded. It retains:

- role and descriptor format version;
- exact installed system text, UTF-8 byte count and SHA-256;
- builtin content version or explicit custom-content identity with no builtin version;
- the exact empty-tool count/hash;
- the literal request formatter fields/version/hash;
- a canonical envelope hash under the `display-repair-envelope-v1` domain.

The unchanged installed system is the existing permitted MIT adaptation: 575
UTF-8 bytes, SHA-256
`6c3eb945a1fc5fb98ce4be807d9dc7e4ea1a1702302cde8957df22eadb541151`,
identified as `workspace-display-repair-1`. Different installed content is custom
content, without falsely claiming that version. Recognition identifies bytes;
it does not attest who supplied them. No new upstream prompt wording was imported
or adapted in this implementation.

The fixed formatter is `workspace-display-repair-request-1`: mode, KaTeX error
(or the existing `unknown` fallback), then expression. Fresh actual messages are
verified against the owned input immediately before `complete(messages, ())`.
The system, formatter, user message and zero tools sent to the model are unchanged.
Dynamic expression/error text or hashes are absent from the descriptor, as are
project, macro context, session, run, cookie, URL, credential and model identifiers.
The existing answer's `model` remains the configured alias, not an inferred
resolved model. This is not a full dynamic-request fingerprint or unique call ID.

Static system text is bounded to 2 KiB UTF-8, each formatter scalar to 128 bytes,
and the full descriptor to 4 KiB. Conservative JSON escape accounting precedes
aggregate encoding, including the HTTP encoder's ASCII escapes. Copied or
constructed model instances are revalidated before use/serialization. Exported
descriptor text is bounded before JSON parsing. Invalid UTF-8, malformed fields
and oversized or inconsistent metadata cannot create an accepted descriptor.

## Producing answers and cache semantics

[mathrepair.py](../packages/workspace/src/sandbox_science/workspace/mathrepair.py)
forms the descriptor at `_call`, after the existing queue/semaphore admission,
then verifies the actual host inputs before invoking the model. A usable repair,
unusable/UNFIXABLE answer, ordinary gateway failure or timeout keeps that same
static descriptor. Sanitization and the browser's renderer/safety validation are
unchanged. A renderable repair does not prove mathematical equivalence or resolve
a mechanical/scientific finding; saved TeX remains untouched.

Cache keys still use project/macro cache scope, mode and TeX, excluding KaTeX
error. In-flight joins and cache hits keep the **first producer's** descriptor;
they never reconstruct it from another waiter's error or today's installed text.
The existing shared cache/TTL/rate behavior remains unchanged. An exported
historical descriptor validates its own retained fields/hashes independently of
today's prompt, without establishing catalog authenticity or external execution.

Missing/legacy metadata remains unknown. Disabled, busy and rate-refused work,
and queued work that never enters `_call`, acquire no producing-call descriptor.
A local metadata failure returns fixed `repair input metadata unavailable`, with
no descriptor or gateway call and no raw exception in the answer/log. It follows
the existing one-hour failure cache and browser-rate charge; it does not create
a project spend reservation or automatic retry.

Canceling one waiter preserves the existing shielded shared producer. It may
finish and populate the cache for another waiter. Service close cancels/gathers
active and queued producers as before; a canceled producer invents no terminal
answer or cancellation receipt. Neither cancellation nor timeout establishes
provider receipt, completion or zero cost. Repeated-close cancellation hardening
is a separate existing lifecycle limit, not a new guarantee of this change.

## Limits and remaining LD-46 work

The descriptor lasts only with the answer/in-memory cache: cache eviction or
server restart loses it. The existing 512-answer and 10-in-flight limits bound
the added per-entry metadata, without a new global retention quota. Authentication,
Host/origin checks, request limits, alias selection, timeouts, concurrency,
sanitizer, cache keys/TTLs and browser-session rate limits remain unchanged.
Client project/context values are cache identities, not proven document lineage;
the browser-cookie rate key is not a logical research session or run.

LD-46 durable per-project call records, resolved-model/tokens/cost and atomic
project-budget admission remain open. Trusted billable project/source context,
project reviewer selection, durable attempt/recovery/replay and retention need a
separate coherent slice. No database, accounting policy, GUI change, provider
execution claim or larger research authority is introduced here.

Scripted actual-input, producer/cache/history, failure/cancel, bound/copy and
in-process authenticated API tests cover this descriptor. Root owns broader
gates, integration and fresh CI. No live provider, research computation, worker,
VM or model-quality evaluation is claimed by author validation.

Author validation passes 28 new cases, 55 combined repair/provenance cases and
143 affected cases, whole-workspace typing, focused lint/format and installed
licenses. Independent source probes close the subtype and DEL escape bounds.
Initial old-field expectations and test-only style/type diagnostics remain
separately recorded; they are not represented as successful final runs.
