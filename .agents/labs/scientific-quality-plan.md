# Scientific-quality acceptance

Prepared against `ae8ef6b2` on 2026-10-18. **No paid calls have been dispatched.**
The owner authorized $150 aggregate new testing spend on 2026-10-18. The coordinator records that authorization before startup.
This is a small reproducible cryptanalysis exercise, not a continuation of SNOVA
and not a claim of novel cryptanalysis or broad long-context qualification.

## Scientific problem and independent oracle

Study the inverse substitution `S(0)=0`, `S(x)=x^-1` over two binary fields:

- GF(16), polynomial basis modulus `x^4+x+1` (`0x13`).
- GF(32), polynomial basis modulus `x^5+x^2+1` (`0x25`).

The researcher implements a reference calculation in Sage in its research VM.
The operator-only [oracle](scientific_quality_oracle.py) instead uses integer
polynomial arithmetic and exhaustive multiplication to cross-check inverses.
Its selftest passed without network access, a model, or pytest. Never stage the
oracle or its answer output into the research VM or model context.

| Quantity | GF(16) inverse | GF(32) inverse |
| --- | --- | --- |
| Differential uniformity | 4 | 2 |
| Maximum differential probability | 4/16 | 2/32 |
| DDT witness `(a,b)` | `(1,1)` with `x=0,1,6,7` | `(1,1)` with `x=0,1` |
| Vectorial nonlinearity | 4 | 10 |
| `x^3` is a permutation | false | true |

The DDT has one row for every input difference, including zero. Its pinned byte
format is row-major unsigned 16-bit little-endian counts; exclude the zero input
difference when taking the maximum. Complete oracle digests are computed by the
helper, not copied into the research brief. This catches field-basis, zero-row,
ordered-pair and bit-order mistakes independently of the prose's plausibility.

Primary sources for the researcher to obtain and inspect:

1. Kaisa Nyberg, [Differentially uniform mappings for cryptography](https://users.ics.aalto.fi/knyberg/publications/diffuni.pdf),
   EUROCRYPT '93, pp. 55–64. Proposition 6 and the following discussion on printed
   page 62 distinguish inverse mappings in odd and even binary extension degree.
2. NIST, [FIPS 197, updated 2023](https://csrc.nist.gov/pubs/fips/197/final),
   section 5.1.1, as a separate source for inverse-plus-affine S-box construction.
   It does not establish a complete attack from these toy measurements.

The study must preserve the distinction between an exhaustive result in a named
small field, a theorem with conditions, and a statement about a complete cipher.
Literature selection beyond these starting points is allowed but not required.
Record download URL, byte hash, PDF page/line anchors and actual read extent.
The researcher starts with only Nyberg's first two PDF pages read, so later
technical claims must trigger retrieval of the relevant remainder.

## Model and budget proposal

A read-only call through the existing `cm_drive.py preflight` helper confirmed
these gateway descriptions on 2026-10-18. Only the existing helper consumed the
configured key path; no agent inspected or listed secret files. The example
configuration intentionally lacks the two Responses API pins, so this old
round-1 preflight exited 1 for that configuration issue. That is not a model
inference failure. The new lab must explicitly set both aliases to `responses`.

| Role | Alias and effort | Advertised input/cache/output USD per million tokens |
| --- | --- | --- |
| Main, delegated, fold, overview | `arrcodex-gpt-6-sol`, `max` | 2 / 0.20 / 10 |
| Built-in consistency reviewer | `arrcodex-gpt-6-luna`, `max` | 0.10 / 0.01 / 0.50 |

Both advertised a 922,000-token input window. Their listed prices above 272,000
input tokens are respectively 4/0.40/15 and 0.20/0.02/0.75. This experiment proposes
a 160,000-token working ceiling and checks the serialized request stays below
the long-context threshold. The listing does not prove max-effort transport or
review quality; record actual requests and outcomes when the lab runs.

Authorized allowance: **USD 150 total estimated spend across this new test campaign**.
Expected size: 60–100 Sol research calls, 4–8 Sol fold/overview calls, and 20–40
Luna reviewer calls, roughly USD 20–45 at the described rates depending on cache
and reasoning output. This is a forecast, not a cost guarantee. Inspect progress
at USD 100; stop admitting new research at USD 130, leaving USD 20 for settlement
and a short report. Do not reset the lifetime estimate or silently launch another
arm. Upstream LiteLLM controls remain primary; unknown costs stay visible and do
not alone become an admission refusal. Warn if unpriced work makes the estimate
unreliable. Preserve the user's 20-million-token starting grant and explicit
approval requirement for any later 10-million-token increment.

## Exact research prompts

### Initial root message

> This is a reproducible research-harness evaluation on toy binary-field S-boxes,
> not a novelty contest. Investigate S(0)=0 and S(x)=x^-1 over GF(16), using the
> polynomial basis x^4+x+1 and integer coefficient-bit encoding. Establish a
> literature-grounded plan, obtain and inspect the cited primary sources, and
> develop an independently runnable Sage reference implementation. Start by
> reading only the first two PDF pages of Nyberg's paper; distinguish what those
> pages establish from what you have not yet read. Retrieve further pages when
> needed. Use the investigation skill in discover mode if the installed pack
> supplies it, and the built-in document-writing guidance for your report.
> Primary starting sources are https://users.ics.aalto.fi/knyberg/publications/diffuni.pdf
> and https://csrc.nist.gov/pubs/fips/197/final (follow its official PDF link).
>
> Delegate one sustained task to compute and explain the complete truth table,
> DDT, differential uniformity, witness solutions and component nonlinearity.
> Require a permutation when considering alternative x^3. Record why any path
> is rejected and the condition under which it should be reconsidered. The child
> should preserve exact evidence and report limits, not agree with a preferred
> conclusion. It may read its history and communicate with you.
>
> Create a living document with an annotated outline, then fill it section by
> section with standalone research findings, citations, Markdown prose and LaTeX
> math. Update the research plan and evidence ledger as results arrive. Serialize
> the DDT as row-major unsigned 16-bit little-endian counts, including row zero,
> and record its SHA-256. Keep quality/reference.sage and
> quality/results-gf16.json. The JSON fields are degree, modulus_hex, inverse_table,
> differential_uniformity, max_differential_probability as a fraction string,
> witness (input_difference, output_difference, solutions), nonlinearity, and
> ddt_u16le_sha256. Save the binary table as quality/gf16.ddt.u16le. Keep the
> downloaded primary PDFs at quality/sources/nyberg1993.pdf and
> quality/sources/fips197.pdf, and record their retrieved URLs, hashes and read pages.
> The maximum excludes input difference zero. A toy S-box property is not a
> complete cipher attack. Do not claim novelty, manufacture citations, or write
> an account of agent rounds into the final document.
>
> After the initial computation and outline are durable, send a progress message
> and keep the plan and child active for the later comparison and continuity
> questions. Have the child send its checkpoint with message_parent and wait for
> the next instruction using wait_agent_message, up to 300 seconds per wait.
> Do not complete that child or start a replacement merely to cross a checkpoint.
> As its parent, use the mailbox/delegation wait while awaiting the next message.

### Child continuity instruction, delivered through the parent mailbox

> Forward these instructions to your existing computational child. Have it read
> quality/parameters.json once and record the exact receipt_marker with its
> initial computation checkpoint so it can later recover the original evidence.
> Preserve the original computation and its archived output. You will receive a
> controlled batch of reference-table appendices for comparison before the final
> questions. Their bulk is intentional context pressure for this evaluation, not
> evidence of scientific difficulty. Read the specified ranges, check the stated
> invariants, and summarize only findings and caveats. Do not regenerate already
> verified tables merely because older context has been folded. Later, use the
> history search/read tools to recover the original exact receipt when asked.

### Assumption change after at least two accepted child folds

> Extend the analysis to GF(32), polynomial basis x^5+x^2+1. The permutation
> requirement remains. Revisit every conclusion that depended on extension degree,
> including the rejected x^3 candidate; retain GF(16) results under their original
> scope. Recover the original GF(16) DDT hash, witness, and evaluation receipt
> marker from the child's archived history, with the original message/run pointer.
> Do not reconstruct the marker or claim a partial source was read in full.
> Complete the missing source read before citing its technical result. Explain
> whether the two fields disagree or satisfy different theorem conditions.
> Save the comparison as quality/results-gf32.json with the same field names and
> quality/gf32.ddt.u16le; retain the original GF(16) files and scope.

### After the controlled interruption and one root Resume

> Continue the existing work tree from its recorded checkpoint. Inspect the current
> plan, original child results, pending messages, findings and living document.
> Reconcile any interrupted operation under its original identity before running
> code again. Finish the GF(32) comparison, address open reviewer findings, update
> the plan and document, and report evidence-backed conclusions with exact scope.
> Do not rerun completed reference calculations without explaining a changed input.

Use this continuation text only if an ordinary root Resume cannot carry a saved
message; prefer one explicit Resume with the durable tree and mailbox intact.
Do not send a second root message while the same work is running.

## Reviewer calibration and correction

Prepare a separate, explicitly synthetic author fixture in the same disposable
project using the existing consistency-review fixture pattern. Its immutable
document says: “For the inverse S-box over GF(16) defined here, differential
uniformity is 2 and the largest differential probability is 1/8.” The paired
execution evidence is the actual successful reference calculation with δ=4,
probability 1/4 and the four witness inputs. Use short numbers so the platform's
long-number heuristic cannot supply the answer in place of the model.

Run the **production reviewer and built-in rubric with real Luna inside the confined runtime**, through its normal review request and checkpoint flow. Do not
tell that reviewer the expected finding or inject a synthetic finding. It must
read the pinned evidence and raise `contradicting_value` with document and
execution anchors. The fixture's synthetic authorship must be reported separately
from real researcher-authored output; this calibration does not claim the real
researcher spontaneously made that mistake.
Randomize the order of the incorrect and correct cases and give both neutral
document titles. Keep the hidden case labels and expected verdicts in operator
state, never the review snapshot or rubric. Retain the committed case ordering
in the final evidence so the blind comparison can be reproduced.

Then send the real writing main agent this exact message:

> Review the open findings on the synthetic draft calibration document. Check the
> underlying pinned calculation, correct the document if warranted, and respond
> through the finding tool with the new revision as evidence. Do not dismiss or
> confirm your own finding. Keep the report self-contained and preserve the
> original revision in history.

A subsequent real Luna confirmation must verify the corrected revision and mark
the finding confirmed fixed. Also check an already correct calibration document
to measure obvious false positives. A cap, missing coverage or inconclusive
review is not a pass. Record complete window coverage and cursor progress.
Automatic reviewer dispatch/response rounds on the actual research document are
an additional observation, distinct from this manually staged calibration.

## Controlled context and recovery sequence

1. Bring up a new marked random lab using the recovery kit's unique units, ports,
   XDG/TLS roots, owned sandbox ledger and verified cleanup. Build/pin the exact
   final reviewed host/runtime revision supplied by the coordinator.
2. Perform a synthetic selftest of the driver and scoring projections before
   replacing its model peer with the approved gateway. No current personal or
   preserved SNOVA service is read or changed. The offline arithmetic selftest
   is already passed; full driver selftest remains to be built.
3. Run the initial real-model research and standalone reviewer calibration.
   Archive a fresh random evaluation receipt marker with the first child output.
   Keep the marker out of later prompts and retain its expected value privately.
4. If normal work has not folded, feed bounded, deterministically generated
   affine-equivalent DDT appendices through ordinary child reads. Label this as
   artificial context pressure. At most eight roughly 128 KiB text packets;
   inspect fold events between packets and stop after two accepted child folds.
   Tool truncation is not a fold; if the ceiling never triggers, mark this
   boundary not reached instead of claiming success or forcing unlimited input.
5. Queue the changed-assumption instruction through the main agent and verify
   parent-to-child delivery. After a completed scientific checkpoint and with no
   unresolved mutating tool operation, interrupt only the owned orchestrator,
   preserve the worker/sandbox, restart and issue exactly one root Resume.
6. Verify the same work tree resumes, the child retrieves exact original history,
   and the parent integrates its result into the existing plan/document. Preserve
   attempt links, mailbox acknowledgements, fold records and all failed outcomes.
7. Produce a final report and score all gates. Export only sanitized receipts with
   hashes; verify complete owned teardown. No monitor is created or restarted.

## Acceptance rubric

| Gate | Evidence needed for pass |
| --- | --- |
| Grounding | Real fetched primary sources, byte hashes and page/line anchors support the stated theorem conditions; partial read extent is honest. Missing access remains explicitly unverified. |
| Reference calculation | Worker-executed script and complete result file match the independent truth tables, DDT bytes/hashes, witnesses, uniformities and nonlinearities for both exact bases. |
| Scientific scope | Distinguishes inverse/cubic, even/odd degree, permutation requirement, exhaustive measurements/theorem, and S-box/full-cipher claims. No invented novelty. |
| Child folding | Two accepted model-written child folds, pinned Sol/max profiles and canonical usage; original archive remains readable. Controlled pressure is reported. |
| Exact recall | After folds, actual child `history_search`/`history_read` results contain the original receipt marker and exact values before reuse. Matching prose without retrieval does not pass. Parent can retrieve the child's original record too. |
| Assumption change | GF(16) remains scoped, GF(32) is independently computed, and the previously rejected cubic is reconsidered under the new permutation condition. |
| Reviewer effectiveness | Luna independently anchors the seeded δ/probability contradiction, main corrects the document through tools, and Luna confirms the pinned correction. Correct control is not falsely failed. |
| Plan/document continuity | One plan/document identity survives; outline precedes filled sections, later revisions incorporate corrected findings, active work is neither silently marked complete nor duplicated on Resume. |
| Recovery | Original work/child identities and checkpoint artifacts survive one owned interruption/root Resume; pending instruction is delivered once; no completed calculation replay without reason. |

Report each gate as passed, failed, not reached, or needs human judgment. Do not
collapse them into a plausible-sounding overall success. Separately report calls,
tokens, observed/estimated/unknown cost, cached-token fields when supplied, fold
latency, repeated identical tool calls, avoidable errors and wall time. Missing
provider cache telemetry is unavailable, not zero. This single lowered-ceiling
GPT case does not fulfill the full CM-P1–P9 two-family/600,000-token campaign.

## Reuse and remaining preparation

- `recovery_acceptance.py` and `recovery_acceptance_up.sh`: unique resource
  ownership, exact image checks, explicit Resume, archive reads and full teardown.
  Refactor or adapt only lab code; do not run old shared `lab_up/down.sh` unchanged.
- `consistency_review.py`: production immutable evidence, reviewer grants and
  confirmation patterns. Its scripted findings are replaced by the real reviewer.
- `cm_lab/scenario.py`, `score.py`, `export.py`: probe design and evidence-order
  scoring. The old CLI-oriented driver is not reused to bypass runtime isolation.
- `launcher_config.py` and `launcher/parts.py`: path-only credential configuration
  and normal confined runtime upload. The coordinator supplies approved paths;
  launcher/helper processes alone consume them. No key content enters evidence.
- Initial sources can use direct read-only HTTPS fetch through the research
  policy. An MCP retrieval subcheck needs the coordinator's approved provider
  environment-file path and the normal launcher importer; it is optional and
  separately scored, not silently replaced by host-side retrieval.

The staged driver, independent grading and synthetic reviewer selftest are prepared
in `scientific_quality.py` and `scientific_quality_checks.py`; see
[the runbook](scientific-quality-runbook.md). Offline validation confirms arithmetic,
mutated-answer rejection, correct/incorrect reviewer evidence wiring and record
export. It does not qualify a real model's scientific judgment.

Remaining before paid execution: owner-authorized budget, coordinator-verified
final source/image revision and independent review of lab boundaries. Actual
max-effort/API behavior and targeted PDF access remain live evaluation gates.
No product edits or competing pytest suites are planned.
