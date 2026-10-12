# Internal document export-input validation

The internal `ExportInputs` API pins exact Markdown source, parser/profile/map
identity, original TeX spans and effective macro context. It retains immutable
operation receipts and bounded logical quotas. This is preparation for LD-35–38;
it does not stage files, run a converter, expose a public route or produce an export.

Signed author source `f06be467856db3ef0f48a683d01a1d3e620701b3` changes six
declared paths from main `6eb9a2c`. Independently reviewed signed union
`c29ada6b6930f0511e4daa77ed1d9152b7d77283` integrates the separately reviewed
kernel observer and test-readiness correction. All six export paths match the
author source byte for byte; all observer product/test paths match `89bd8a7`.
Later observer helper/docs commits change no gated package, web or configuration
bytes. Final delivery includes accepted helper `a6bb469` and reviewed observer
report correction `8fd62e8`; actual observer main `d61e4adf183866d66a50c8103645bb5714657ce6`
is integrated without package, web or configuration changes. Lab26 remains separate
observer acceptance. PR #97 merged at `d61e4ad` on independently reviewed signed `8fd62e8` after all four fresh checks passed (run `38315213515`).

The [exact local evidence](document-export-inputs-evidence-2026-10-12.json)
retains author and root gate logs and independent source/union review, each with
its text byte length and SHA-256. No model, converter, worker, browser or VM was
started for this export slice. OpenShell integration tests were intentionally
skipped; the separately owned kernel-observation lab is unrelated acceptance.

| Validation | Result |
| --- | --- |
| Affected document/store/macro tests | 134 passed, including 53 export cases |
| Root full Python suite on `c29ada6b` | 9,979 passed, 43 opt-in skips, two existing serializer warnings |
| Root web suite | 575 passed |
| Post-build math suite | 24 passed |
| Locked dependencies, Ruff, Pyright, licenses, generated protocols | Passed |
| GUI and isolated math builds | Passed |
| Independent exact author and union review | No remaining actionable finding |

Current-source preparation selects the head and current project macros in one
read snapshot. Numbered revisions select their saved context. Parsing and replay
reconstruction happen outside transactions; the short atomic write rechecks replay
and actual retained byte/count quotas. Later head/settings changes do not repin a
completed request. Quota refusal writes nothing and does not veto ordinary saves.

Independent review reproduced two defects before the signed freeze. A coherently
forged receipt could change derived spans or macros while retaining canonical pins;
replay now reconstructs and compares those fields with canonical rows and the
independently retained original selection header. TeX resource controls followed
by digits evaded a word-boundary detector; `input0`, `openin1`, `write18` and encoded
control syntax now produce an explicit unsupported-resource hold. Both corrections
passed independent reprobes and retained regressions.

The supported boundary is conservative, resource-free Markdown. Unknown saved
macro context, unsupported profiles/resources, oversized maps and deleted sources
remain explicitly held where a bounded truthful pin is representable. This is not
a TeX safety certification or a reference resolver. Protected staging, worker tool
provisioning, converter cancellation/recovery, output/producer lineage and actual
`.tex`/PDF acceptance remain open. Fresh exact-head PR CI is a separate delivery gate.
