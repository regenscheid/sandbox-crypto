# Planning and progress evaluator: scripted calibration

Root ran the opt-in evaluator's two local scripted controls on 2026-10-13. The positive control met the evaluator's criteria. The deliberately missing-progress control correctly failed the progress criterion and returned the expected negative result. Both marked fixtures were removed; independent source, public-result and cleanup review cleared the controls. The [exact evidence archive](planning-evaluation-evidence-2026-10-13.json) preserves the original and corrected source/probe records alongside these results.

This establishes the measurement path through actual production `run_task`, `PlanTools` and canonical `SessionPlans` reads. No real model pilot, provider request, worker, VM, service or browser ran. Model adherence, scientific correctness and broader planning acceptance remain unproved.

## Exact source and chronology

The reviewed helper was adopted at signed `0bde6f68ab187a9c1e0ed53dbdbe5deb700dbb43`; the dedicated-doc clarification child and actual calibration source was signed clean `3f0e124d3d0edf9d9d467df923d06de3e9122656`. Helper SHA-256 is `f1aefb6ac9b91a90e6b99c0dec8a6f1efb8ab23c9796c7175b945d1a71305947`, 33,413 bytes, exactly the independently reviewed corrected candidate's bytes and executable AST. The [helper contract](../../../docs/PLANNING-EVALUATION.md) describes its task, authority and bounds.

Subsequent signed integration `42f4d7866122f916188f7fdb283081a019d465a2` has exact parents `3f0e124` and delivered actual main `65989b57f3ed0d258e3b71f1efee3ba1836787d2`. It had no conflicts. The helper and all ten pinned production inputs remain exact the calibration/adoption source and actual main. At that integration tree, all other tracked inputs equal actual main, apart from the original helper, dedicated documentation and labs README. This reporting child changes only documentation and evidence; it does not change those production inputs or the executable helper. Central delivery tracking and inventory are maintained separately by root. Final integration requires independent review and all fresh exact-head CI checks before merge; no new CI result is claimed here.

## Controls and observations

Both controls used a fresh exclusive public output, eight admitted-call capacity, 64 requested output tokens per call, 512 cumulative requested output tokens, 1,048,576 cumulative application input bytes and 30 seconds. The script's wall budget begins before setup. Requested output reservations are not provider-enforced spend, and input bytes do not imply input tokens.

| Scripted control | Actual calls / canonical observations | Input bytes / requested output reserved | Observed result |
| --- | --- | --- | --- |
| Positive | 8 / 7 | 225,597 / 512 | Answered; matching in-progress assertions precede terminal assertions; current scope/steps and qualified partial outcome pass |
| Missing progress | 6 / 5 | 157,259 / 384 | Answered with qualified partial outcome, but matching in-progress assertions are absent; evaluator correctly reports `passed=false` |

The positive public result was recorded at 16:27:43.774401 UTC. Root's actual invocation returned exit 0; its retained stdout, `passed=true` result and unchanged helper exit mapping agree. There is no separate original positive exit-receipt file, and none is reconstructed. The negative was recorded at 16:28:59.970346 UTC; its separate retained root receipt explicitly records expected and actual exit 3. It is an intended negative control, not a failed implementation gate.

Both controls observed committed progress through a second read-only canonical store connection before any run save. Start evidence binds the original plan ID, progress sequence and step fingerprint. The fixed task leaves an unavailable third observation explicit: its terminal outcome is `partially_achieved` with remaining work, rather than a claim of scientific completion. Non-plan attempts and worker effects are zero. No final `RunRecord` was saved and no notice ACK was exercised.

## Original findings, corrections and scoped checks

The earlier immutable `8438ae6e` candidate failed independent plan-identity and deadline probes: a replacement plan could reuse another plan's starts, and setup time could be followed by a fresh full loop budget. Corrected `f1aefb6a` pins start evidence to the original plan and passes only the remaining original wall budget to the loop. Independent source review verified the narrow two-definition change, 20 corrected author guards, five additional boundary cases per candidate and five correction reprobes. All 26 original and 12 corrected manifest records are retained exactly.

Original lint diagnostics, a guessed nonexistent guard path and the initial independent AST fixture error are retained separately. That fixture lacked source line metadata and failed before testing criteria; its original empty captured stdout remains exact. A separately named note reconstructs the uncaptured stderr and is not presented as original stdout or stderr. Corrected fixture/probe outputs are distinct. Historical `8438` recipes and source-only reviews keep their original status; the later adopted-head recipe and calibration records establish the subsequent chronology.

Adoption's seven original scoped logs pass: format, Ruff, explicit helper typing, standard-library-only default description and help, source/AST verification and whitespace. The report author checked merge/source identities, archive bytes and local links without replaying calibration or running a full product suite. There is no product-code, prompt, tool-grant or dependency change in this slice, and no new broad-gate claim is made.

## Cleanup, evidence and limits

Both controls report adapter, model-client, read-only reader and writer closure, plus owned fixture removal. Independent read-only review at 16:34:52.360514 UTC confirms exact public JSON/stdout/source coherence, absence of both exact marked fixture paths and zero remaining fixed-prefix fixture directories or symlinks. Counters and close flags are helper self-instrumentation at the production local seams; the reviewer checked fresh fixture absence and did not inspect removed SQL contents.

The archive has 90 exact UTF-8 records, 363,787 original text bytes; archive size 407,883 bytes, SHA-256 `fea2485c3cb4619fb4013271e130615c0d8bb435c3a386f8e586dae3d87c7614`. It includes original unsuccessful candidates/fixtures, corrected guards, adoption/source proofs, current and historical recipes, actual control outputs and independent reviews. Previously accepted reports, archives and images are unchanged. No actual private model configuration, credential, login, worker/provider configuration or private hash was read or exported for this report.

A real model pilot still needs root-provided existing configuration and a separately selected finite budget. These controls do not prove external model compliance, provider wire delivery or spend limits, worker-start/progress ordering, USER-edit races, browser behavior, saved-run/notice-ACK behavior or research correctness. Cleanup may outlast a response deadline while actual owned work drains; interrupted external effects would remain unknown without automatic retry.
