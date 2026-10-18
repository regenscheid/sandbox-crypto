# Scientific-quality lab operator runbook

**Prepared, not yet run with real models.** The owner authorized USD 150 aggregate
new testing spend on 2026-10-18. The earlier SNOVA allowance is not reused. No lab
service is started by importing the driver, its offline selftest, `prepare`, or
`authorize`. The last operation records an authorization supplied by the human
operator; it does not independently establish that the owner actually gave it.

Read [the experiment plan](scientific-quality-plan.md) first. This kit reuses the
qualified recovery lab's unique private root, dedicated OpenShell gateway,
ownership ledger, image checks, bounded replacement drain handling and complete
teardown. It never operates on the personal instance or preserved SNOVA lab.
It does not restart the old monitor.

## Offline preparation

From the final reviewed source checkout:

```sh
uv sync --locked
.venv/bin/python -I .agents/labs/scientific_quality.py selftest
.venv/bin/ruff check .agents/labs/scientific_quality*.py
```

The selftest uses two scripted reviewers against a temporary real document and
review store. It validates the reference arithmetic, numerical grading with
changed claims and bytes, immutable evidence wiring, a contradiction finding, a
checked correct control, and model configuration serialization without opening a
key. Its output explicitly says scientific quality is **not** qualified. It
neither starts OpenShell nor contacts a model endpoint.

Build the exact final `orchestrator`, `minimal`, and `crypto-base` images using
the image recipes in the recovery section of `.agents/labs/README.md`; build the workspace or pass an
existing read-only static directory. `prepare` checks all image revision labels
and refuses dirty image inputs. Do not reuse a mismatched old orchestrator image.
The host and image revisions are recorded in `identity.json`.

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py prepare --static /absolute/path/to/workspace/dist
```

The printed random `/var/tmp/ss-recovery-...` directory is the only lab root to
use below. Preserve that exact path as `QUALITY_LAB`; never substitute a personal
or old acceptance root. The private `quality.json` contains the randomized
correct/incorrect case assignment and the expected receipt marker. Do not send
it, the oracle, or this operator runbook to either model.

## Authorized startup

Only after the owner explicitly authorizes this new allowance, record their
actual authorization text and the already configured **absolute key file path**.
Do not print, open, list or copy the owner's key yourself. The existing launcher
alone consumes the referenced key and prepares the confined runtime.

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py authorize "$QUALITY_LAB" --allowance 150 --key-file /absolute/authorized/key-file --authorization 'Exact owner authorization'
.venv/bin/python -I .agents/labs/scientific_quality.py up "$QUALITY_LAB"
.venv/bin/python -I .agents/labs/scientific_quality.py inspect "$QUALITY_LAB"
```

The live configuration pins Sol research/delegation/narration and Luna review to
Responses/max. A gateway listing and local validation do not prove accepted
runtime effort; inspect the actual saved profiles and call records. No watcher
or skills-generation model is enabled. Startup never sends a research message.

The lab's owned `-model.service` unit is repurposed as a five-second lifetime
estimate guard after the unused synthetic model peer is stopped. It reports at
USD 100 and sets the lab project's daily admission cap to zero at USD 130 observed
plus explicit reservations. Unknown/unpriced calls remain visible. Do not reset
the guard or silently spend the USD 20 reserve; it is headroom for in-flight
settlement and reporting, not automatic authorization to increase the cap.
The upstream gateway remains the primary spending control. Inspect uncertainty
at each meaningful checkpoint; do not interpret an unavailable cost as zero.

All mutation stages record an intent before dispatch. If a stage fails, inspect
that intent, the original worker operation or message ID and receipts. A second
invocation intentionally refuses to replay an unknown result.

## Real reviewer calibration

Run the two neutral cases sequentially, waiting for settlement between them:

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py calibrate "$QUALITY_LAB" --case calibration-a
.venv/bin/python -I .agents/labs/scientific_quality.py inspect "$QUALITY_LAB"
.venv/bin/python -I .agents/labs/scientific_quality.py snapshot "$QUALITY_LAB"
```

Repeat for `calibration-b` only after the first has no live lease. Each case runs
the independent reference calculation in the real worker, validates its exact
receipt, then seeds a neutral fixture-authored document and that actual execution
as immutable reviewer evidence. The script queues a normal review and sends a
Sol message asking it to await the reviewer. It never inserts a live finding,
patches the document, or confirms a correction. Real Luna must find the wrong
claim; real Sol must correct it; real Luna must confirm it. In the correct case,
a positive checked-evidence record and no false scientific failure are required.

Check finding anchors, review outcome and coverage, document revision authors,
and ordering. A model fixing the document before the reviewer detects the issue
does not pass the reviewer-effectiveness gate. Cap/timeout/unavailable outcomes
are not passes. Run the study only if calibration does not reveal a blocking
harness defect or prohibitive repeated-call behavior.

## Study, continuity and controlled interruption

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py materials "$QUALITY_LAB"
.venv/bin/python -I .agents/labs/scientific_quality.py initial "$QUALITY_LAB"
```

The materials are an unpredictable receipt marker and explicitly labeled
GF(256) affine-DDT context-pressure appendices. They contain neither the private
GF(16)/GF(32) answer key nor reviewer case assignments. All research code,
literature downloads and PDF reads must happen in the research worker.

Inspect actual progress. Once the original reference results, outline and child
checkpoint are durable, use `child` to request original-history continuity. Send
each bounded `pressure --appendix 1` through `--appendix 8` only as needed and
only once. These are user instructions to the root, which must relay them to the
existing child. Verify actual full bounded reads; a filename acknowledgment is
not context pressure. Stop pressure after two applied child folds. `inspect`
counts durable applied folds across child attempts; a paid fold call or merely
an overflow marker is not counted. If eight appendices do not induce two folds,
record the gate as not reached rather than weakening it.

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py child "$QUALITY_LAB"
.venv/bin/python -I .agents/labs/scientific_quality.py pressure "$QUALITY_LAB" --appendix 1
.venv/bin/python -I .agents/labs/scientific_quality.py inspect "$QUALITY_LAB"
.venv/bin/python -I .agents/labs/scientific_quality.py changed "$QUALITY_LAB"
```

`changed` refuses until two accepted child folds exist. The GF(32) assumption
change must reopen the permutation decision, keep GF(16) results scoped, retrieve
the original receipt/hash/witness from archived child evidence and complete the
previously partial source read. Judge these from exact tool receipts, not a
plausible final answer or a well-known theorem recalled without evidence.

While the GF(32) branch is still unfinished:

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py hold "$QUALITY_LAB"
.venv/bin/python -I .agents/labs/scientific_quality.py interrupt "$QUALITY_LAB"
```

`hold` gives both agents standing post-Resume instructions before waiting.
`interrupt` refuses unless at least two unresolved operations are non-mutating
mailbox waits, no calibration lease is live and no Resume is already queued. It
captures the original work IDs, removes only the owned orchestrator VM, replaces
it and queues one root Resume. A known bounded drain refusal may retry only
after the old runtime is disconnected, as in the recovery fixture. It does not
replay an unknown paid/tool operation. The sampling race remains subject to the
production original-identity recovery checks. If the checkpoint is missed or
the child has already completed, mark recovery not reached instead of inventing
an interruption or creating replacement child work.

Watch the same root and child work IDs continue. Do not add a second root run to
make a blocked Resume appear successful. A new failure is evidence to inspect;
the driver is not an unbounded retry loop.

## Evaluation and teardown

Once no live leases remain:

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py finish "$QUALITY_LAB"
```

This exports full textual archive/tool receipts, folds, plan records, all living
document revisions, review/finding records, and the bounded named reference,
result, DDT and primary PDF files. It checks field arithmetic and binary DDT
content against the independent oracle. PDF magic only proves plausible PDF
bytes; primary-source provenance and actual page reading require transcript
review. Inspect citations, assumption changes, exact archived recall and real
reviewer/author correction order using the plan rubric. `finish` deliberately
does not award an overall scientific-quality pass. Additional unregistered
worker files are not automatically exported; explicitly preserve any important
ones before cleanup. The full 600k/two-family context campaign and MCP retrieval
are outside this prepared run's qualification.

```sh
.venv/bin/python -I .agents/labs/scientific_quality.py cleanup "$QUALITY_LAB"
```

Normal cleanup requires terminal evidence. On a failed or abandoned run,
`abort` is the separate explicit teardown path and does not require numerical
success or zero leases. Inspect and preserve needed evidence first. An incomplete
store export is reported without preventing owned teardown. If gateway listing
fails during incomplete startup, the abort fallback still attempts every exact
owned unit stop, checks those units, observed PIDs and owned ports, and retains
the private root. That fallback explicitly reports incomplete VM cleanup; it
never adopts unknown VMs or claims full teardown. Both normal paths reuse the
qualified cleanup checks for exact owned VMs, units, PIDs, ports and driver
directories; refusal on an unrecorded VM requires ownership inspection, not
automatic adoption. Sanitized evidence is copied to the printed sibling
`ss-recovery-evidence-...` directory with a SHA-256 manifest before the private
lab root is removed. Do not report a lab stopped until those checks pass.
