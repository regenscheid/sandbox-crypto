<!-- SPDX-License-Identifier: MIT -->
# Explicit original research framing

`sandbox-science-harness run --research-template ID` selects one short first-party
MAIN research template. The five choices are `intake`, `literature`, `experiment`,
`proof-audit` and `report`. Omit the option to retain the existing default prompt
and provenance format. Selection is explicit trusted CLI/caller configuration,
fixed for the run. Task text, quoted documents and tool output cannot select or
replace a template. There is no automatic classifier or project default.

For example, add `--research-template proof-audit` to an otherwise configured
`run` invocation. The same option works with a scripted model for validation and
with the configured model gateway. It creates no additional model call.

## What the templates do

| ID | Version | Original framing |
| --- | --- | --- |
| `intake` | `sandbox-stock-intake-1` | Establish goal, known inputs, useful evidence, assumptions and feasibility; ask only for a decision that blocks progress. |
| `literature` | `sandbox-stock-literature-1` | Inspect available sources, distinguish source claims and deductions, compare methods and limits, and disclose inaccessible material. |
| `experiment` | `sandbox-stock-experiment-1` | State hypothesis, controls, inputs, stopping condition and expected evidence; preserve actual execution scope and partial/negative results. |
| `proof-audit` | `sandbox-stock-proof-audit-1` | Separate assumptions and deductions from finite computation, examples and heuristics; distinguish gaps from counterexamples. |
| `report` | `sandbox-stock-report-1` | Connect claims to inspected evidence and existing artifacts; separate conclusions, workflow status, uncertainty and remaining work. |

These are original framing aids, independently written under D56. No upstream
prompt, code or schema is copied, and no parity with an upstream product is claimed.
Scripted tests prove selection and input integrity; improved research behavior
would need a separate budgeted evaluation.

## Assembly and authority

Selected system content is exactly the existing MAIN base, two newlines, the
selected template, and—only when the six real plan tools are offered—two newlines
plus the unchanged planning guidance. The original user task remains its exact
user message. Ordered tool schemas, descriptions, grants, dispatch, policy,
isolation checks and closing budgets are unchanged. Templates name no extra tool
or permission; unavailable retrieval, execution or planning is disclosed instead
of fabricated. A template is not a loaded external skill or a role switch.

The installed catalog is a fixed immutable mapping of five bounded records in
[stock_prompts.py](../packages/orchestrator/src/sandbox_science/orchestrator/stock_prompts.py).
Each text is nonempty valid UTF-8 and at most 2 KiB; IDs and version formats are
fixed. There is no file, environment, plugin or network loader, interpolation or
model-controlled selection. Invalid selection or installed content refuses before
model invocation. Selected aggregate system and metadata limits also refuse;
selected runs never fall back to missing/unavailable provenance.

The [MAIN plan tools](ORCHESTRATOR-PROMPTS.md#implemented-stateful-main-planning-tools)
keep the plan and its progress when they are offered. Selection introduces no
mandatory planning ceremony or approval gate. Existing authorization remains
effective; `awaiting_approval` cannot be self-activated. USER approval, delegation,
effect-to-step lineage, compaction and offline wake are not built, and a template
cannot supply any of them through wording.

## Retained input provenance and compatibility

Unselected runs retain `sandbox-main-bundle-1` exactly, including custom-system
input, unavailable metadata and legacy records without metadata. `run_task` accepts
optional trusted `research_template`; a differing custom `system` plus selection
is refused. Explicitly supplying bytes identical to the installed MAIN base is
recognized by content, without claiming who supplied those bytes or granting
authority.

Selected runs use complete-only `sandbox-main-bundle-2`. The bounded component
sequence is MAIN, stock, optional planning. The stock pin contains exact ID,
version, UTF-8 byte count and SHA-256; the full system and ordered schemas retain
their existing pins. Its bundle digest uses the distinct
`main-prompt-bundle-v2` domain. It remains content identity, not origin attestation,
authorization, provider wire proof or evidence of model compliance.

Historical reads validate the retained system and component bytes and hashes;
they never substitute today's catalog text or require the historical version to
remain installed. New catalog text/version affects a later run, while an active
run keeps its original immutable assembly and fresh owned schema projections.
Version-1 and legacy parsing still accept their original default fields.

Safe failed-run retention carries the same selected provenance with fixed failure
metadata and no final answer. Selection adds no notice or ACK. Existing
successful-model delivery and durable save-before-ACK rules remain in force; a
failed/duplicate save cannot acknowledge a new notice. Cancellation still
propagates under the existing owned cleanup rules.

## Source and tests

[prompt_bundle.py](../packages/orchestrator/src/sandbox_science/orchestrator/prompt_bundle.py)
owns the separate selected schema and exact retained validation;
[loop.py](../packages/orchestrator/src/sandbox_science/orchestrator/loop.py)
resolves the trusted selection once;
[cli.py](../packages/orchestrator/src/sandbox_science/orchestrator/cli.py)
offers only the five fixed choices. The pure tool encoder is shared with the
reviewer provenance.

Scripted tests exercise all five selections with and without plan tools,
unchanged tasks/grants, document injection, mid-run catalog/schema mutation,
invalid/bounded installed content, coherent empty-stock corruption, historical
catalog replacement, real CLI retention and safe failed saves. They are contract
checks, not live research quality evaluations; no live model, service, browser,
worker or VM is involved.
