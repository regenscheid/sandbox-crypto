<!-- SPDX-License-Identifier: MIT -->
# MAIN prompt input provenance, 2026-10-12

## Result and scope

Independently reviewed signed author `34591d398ad579f714ee40a685d1537183ec1b08`
adds exact MAIN system/tool content pins. Signed combined gate source
`c64a4d334b93bb66f1d1095ce1271235995838d3` integrates actual planning main
`25510abf8c411f136936b5283fda2d880bd2bf28`. All seven author paths remain exact;
all other product/test/configuration/dependency paths remain actual main.

The [implementation boundary](../../../docs/MAIN-PROMPT-PROVENANCE.md) describes
owned schema copies, immediate model-boundary verification, optional complete or
unavailable metadata, truthful custom/built-in content identity and legacy unknowns.
Base/planning wording, tool schema values/order, grants, dispatch, closing and
save/fsync-before-ACK behavior remain unchanged. Metadata covers application
system/tool inputs, not dynamic messages, provider-wire transformation, resolved
models, other model roles, instruction adherence or scientific correctness.

## Review and validation

Independent source review found and corrected schema allocation undercounting for
large integer decimal expansion, and built-in content/metadata overflow fallback.
The corrected preflight refuses before aggregate JSON; invalid installed content
refuses before a model call. Oversized/unrepresentable custom system text keeps
its actual invocation with explicitly unavailable metadata. The final signed seven
blobs exactly match the reviewed saved paths. Coherent rewriting of a whole record
is not prevented by its self-checksums; these are audit pins, not authentication.

Author validation: 172 affected cases passed in 18.47 s; the final 32 bundle cases
passed in 0.49 s after an assertion-only Ruff correction. Whole-orchestrator and
changed-test Pyright, Ruff/format and all 75 Python distribution licenses pass.
Root combined gates on signed `c64a4d3` pass 10,344 Python cases/43 opt-in
skips/two existing serializer warnings in 381.47 s, full Pyright/Ruff/format,
protocol freshness and 75 Python licenses; fresh web license/install/check,
656 web cases, both production builds and 24 post-build math cases also pass. No external model, service, worker or VM
was started for this metadata slice.

Two stale planning documentation statements were corrected separately: installed
six-tool names and the final current-plan reread guard now differ explicitly from
future automatic effect/liveness/approval/delegation behavior; the implementation
plan's old D82 integration focus is refreshed. This expands no runtime capability.

## Delivery and retained evidence

The [exact evidence archive](main-prompt-provenance-evidence-2026-10-12.json)
retains source pins, independent review and original focused/combined logs. Fresh
exact-head CI and final signed delivery review are required before merge.

Planning/live progress was already delivered as PR #102 at `25510ab` after all four
fresh checks passed on independently reviewed signed `1d4205f` (run `38332303702`).
Its [browser evidence](session-planning-2026-10-12.md), original runtime freeze,
corrected test-only provenance and complete owned cleanup are unchanged.

Watcher prompt provenance is a separate in-process slice; reviewer/display-repair
pins, broader catalog loading, USER plan editing/approval, delegation, questions,
automatic effect lineage, compaction and live-model quality evaluation remain open.
