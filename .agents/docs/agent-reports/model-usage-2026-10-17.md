# Observed model usage survives interrupted runs

Schema 60 commits each main/delegated response's usage and cost with its archived
assistant message, including native response data, before observers, delivery or
tool dispatch. The immutable row names the scope, role, answered-response ordinal
and observation time; usage JSON is limited to 16 KiB. Migration preserves legacy
records without inventing usage.

Budget queries prefer the journal per response, then a final record, then an event
when no final record exists. Matching copies are suppressed regardless of the time
window. Journaled responses use their observation time, so calls from a run older
than a day still consume its daily budget. Legacy and fold costs retain their
run-start window. Unknown costs retain the existing refusal rule. Evidence export
v3 includes the journal; v1/v2 verification keeps the original field sets.

Two independent GPT-6.1-Sol reviewers examined storage, budget precedence and the
loop's ordering. No actionable findings remained. The storage reviewer also
checked added cancellation-during-write and mixed-role/fold-cost tests.

Validation: the final affected gate passed 99 tests in 9.44 seconds, including the
new migration, atomic-write failure, interruption/cancellation, exact native data,
mixed-role/source accounting, report ordinal, unknown cost, daily window and export
cases. The orchestrator/workspace gate passed 4,626 tests in 332.37 seconds, with
17 dependency/live skips and one documentation-inventory failure: it ran before
the new table entry was written. That check passed in the final affected gate.
Ruff formatting/lint, strict pyright, license, generated-protocol and repository
hygiene checks pass. No product behavior changed after the broad gate started.
Evidence outside Git:

- `research-foundations-model-usage-final.log`, SHA-256
  `a878e5d55958e1304895653bcb0d829e5cfb2cc561341dedb39dedd547ecfe38`
- `research-foundations-model-usage-packages.log`, SHA-256
  `ee423c9d80353b841232b9dd04e34a01bc0330c6ef7e669a7977180977e4d357`
- `research-foundations-model-usage-types.log`, SHA-256
  `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332`

This does not journal model intent before dispatch, recover an unobserved response
or unknown gateway charge, reconcile operations, resume a tree, or establish
whole-run memory bounds. The 16 KiB limit bounds stored usage, not temporary
serialization memory. No live model gateway, OpenShell or personal service was used.
