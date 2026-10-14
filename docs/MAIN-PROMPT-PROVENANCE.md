<!-- SPDX-License-Identifier: MIT -->
# MAIN prompt provenance

The MAIN harness records bounded content provenance for its actual system message
and offered tools ([prompt inventory](ORCHESTRATOR-PROMPTS.md#prompt-inventory)).
The provenance layer does not change tool schemas or their order, grants, dispatch,
planning or closing behavior. [Research framing](STOCK-RESEARCH-PROMPTS.md) is
explicitly selected; the [small-scale trial guidance](PILOT-COMPUTATIONS.md) is
part of the ordinary MAIN base. Watcher, consistency reviewer and display-repair
model calls have their own provenance records; this MAIN field does not cover them.

## Assembly and actual inputs

[`prompt_bundle.py`](../packages/orchestrator/src/sandbox_science/orchestrator/prompt_bundle.py)
builds one owned MAIN bundle after active contexts enter and before the transcript
starts. The ordinary system remains `SYSTEM_PROMPT` from
[`loop.py`](../packages/orchestrator/src/sandbox_science/orchestrator/loop.py).
When the real planning adapter offers all six plan tools, the existing
`sandbox-main-plan-1` guidance is appended with exactly two newline characters.
An explicitly selected stock component goes between the base and planning, also
separated by two newlines. With no selection, only the ordinary base and optional
planning component are assembled.
Missing or inconsistent planning tool assembly refuses before any model call.
There is no new generated capability prose or permission based on a prompt ID.

The current base component's `sandbox-main-2` label recognizes exact built-in **content**.
An explicitly supplied identical system string has the same label; this does not
prove who supplied it or convey authority. Other custom strings have a `custom`
component with no invented catalog version. Matching substrings, object identity,
quoted instructions and model outputs cannot select components or tools.
Historical `sandbox-main-1` records keep their exact old text and pins; they are
never relabeled. Supplying that old text to a new run now counts as custom content.

The bundle owns a bounded encoded projection of precisely each offered tool's
name, description and parameters. Each model call receives a fresh schema copy;
neither the original mutable parameter dictionaries nor a preceding model call's
copy can change subsequent input. Immediately before `ChatModel.complete`, the
loop verifies the actual first system message and the exact tool sequence being
passed. It preserves schema object insertion order and every array/tool order.
The ordinary model adapter, its payload fields and credentials are unchanged.

## Recorded complete or unavailable state

`RunRecord.prompt_provenance` is optional. Missing/null legacy metadata means
unknown; no old record is backfilled from today's constants. Newly assembled
records use an explicit discriminated state:

* `complete`: assembly format `sandbox-main-bundle-1`, role `main`, ordered
  component versions/byte lengths/SHA256, rendered-system pin, ordered tool names,
  canonical schema byte length/hash and a bundle digest.
  Explicitly selected framing uses complete-only `sandbox-main-bundle-2`, adding
  the stock ID/version/byte/hash component between MAIN and optional planning.
* `unavailable`: the same bounded format/role plus fixed reason
  `system_too_large`, `system_not_utf8` or `metadata_too_large`. It contains no
  fabricated component/system/tool pins.

Selected installed input never falls back to unavailable metadata. Existing
unselected custom and legacy parsing remains supported; a differing custom base
cannot be combined with stock selection.

Oversized or non-UTF-8 custom system text keeps its exact existing invocation
behavior with unavailable provenance; it is never clipped or substituted. Invalid
installed base/guidance/metadata, malformed schemas and capability mismatches refuse
before invoking the model. The bundle still owns and verifies valid schemas when
custom system metadata is unavailable.

No requested alias, resolved model, endpoint, token, credential fingerprint,
configuration file, task text, provider error or exception text is added to this
metadata. Existing model attribution remains where other records already capture
it. A requested alias is not a resolved model, and varying per-call models are not
properties of a prompt bundle.

Text SHA256 covers exact UTF-8 bytes, without whitespace or Unicode normalization.
Schema hashing uses compact UTF-8 JSON, sorted object keys, original array/tool
order and finite JSON values, under domain `main-tool-schemas-v1\0`. The bundle
digest hashes its versioned complete fields, excluding the digest itself, under
`main-prompt-bundle-v1\0`, or `main-prompt-bundle-v2\0` for selected framing.
Content pins use ordinary SHA256. Equivalent object-key
order has the same canonical schema digest while its actual passed insertion
order is preserved. These are application input pins, not provider-wire hashes.

## Bounds and lifecycle

The implementation validates finite ceilings before aggregate encoding: 128 tools,
schema depth 32, 32,768 schema nodes, 4,096 entries per container, 512 KiB schema
budget, 64 KiB rendered-system UTF-8 and 16 KiB provenance JSON. Component count is
at most two for unselected runs and three for selected runs; stock text is nonempty
and at most 2 KiB. String escaping and integer decimal expansion count toward the
schema preflight; nonfinite numbers/non-JSON values refuse. Actual complete
metadata validates its final encoded size and digest too. These ceilings are
implementation bounds, not approval of the proposed prompt catalog.

[`RunRecord`](../packages/orchestrator/src/sandbox_science/orchestrator/projects.py)
checks a complete bundle against the retained first system message and each exact
component slice/separator. It validates the bundle's bounded structure/digest;
unavailable metadata stays explicitly unproved. Historical versions are never
reconstructed from current role text. Tool schemas are not duplicated in the
record, so their stored hash alone cannot independently reconstruct or attest to
the original tool bytes. Coherent rewriting of an entire record is not prevented
by self-checksums. The record is an audit observation, not a permission grant or
cryptographic attestation of a provider's behavior.

[`CLI`](../packages/orchestrator/src/sandbox_science/orchestrator/cli.py) carries the
metadata unchanged into ordinary and safely retained failed runs. The existing
file+directory fsync still precedes notice ACK. Metadata creates no new ACK path,
closing round or scientific state transition. Setup refusal before a transcript
and cancellation still do not manufacture a retained run. A custom unavailable
pin does not turn partial observations into a completed answer.

## Tests and scope

Scripted actual-input tests exercise conditional MAIN planning, ordinary control
tool data, capability-filtered schemas, fresh per-call copies and exact retained
system/component pins. They include Unicode/custom inputs, legacy reads,
malformed/large/deep/cyclic schemas, encoding expansion refused before JSON,
successful/failed CLI retention, save failure before ACK and cancellation.
No live model or service is used.

This does not version dynamic platform notices or the whole conversation, retain
every provider call/model resolution, load external prompts,
add delegation/approval/compaction or evaluate model adherence/research quality
([proposals](ORCHESTRATOR-PROMPTS.md#proposals-not-built)). Input fixtures
verify that the stock and pilot guidance reaches the application model
boundary; they do not prove live instruction adherence or scientific quality.
