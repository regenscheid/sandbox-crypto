# Opt-in planning and progress evaluation

The [`planning_progress_evaluation.py`](../.agents/labs/planning_progress_evaluation.py)
helper measures a fixed synthetic task through the existing production
`run_task`, `PlanTools` and canonical `SessionPlans` reader. It changes no
planner, prompt, tool grant or product runtime. The existing plan tools,
durable progress and viewer/editor are already implemented; see
[session plans](SESSION-PLANS.md), [plan tools](../packages/orchestrator/src/sandbox_science/orchestrator/plan_tools.py) and
[the workspace reader](SESSION-PLAN-WORKSPACE.md).

Root ran the positive and deliberately missing-progress scripted controls on
2026-10-13 through the production local path. The positive met the evaluator's
criteria; the negative correctly failed its progress criterion. Both fixtures
were removed, with independent source/result and cleanup review. The
[calibration report](../.agents/docs/agent-reports/planning-evaluation-2026-10-13.md)
records exact source, budgets, exit-evidence qualifications and retained failures.
No real model pilot has run. That separate pilot remains blocked on
root-provided existing model configuration and an explicitly selected finite
budget. The helper adds no approval gate or provider configuration discovery.

The helper and calibration archive were delivered as [PR #112](https://github.com/regenscheid/sandbox-crypto/pull/112)
at `0bc82b3` after independent review and all four fresh CI checks. The accepted
report/archive retain their original calibration source pins and qualifications.

## Default and active modes

With no arguments, the helper prints a fixed description and exits. This path
and `--help` use only the Python standard library: no production imports,
fixture, database, worker, model client or network operation occurs.

```bash
python -I .agents/labs/planning_progress_evaluation.py
```

Active modes require an absolute repository path, its exact signed clean Git
head, a fresh absolute exclusive public output path and all five budget
arguments. Ten installed production files must match the reviewed byte hashes;
loaded modules must also resolve to that repository. No changes to the pinned
production inputs are accepted by this helper.

Root ran the two small scripted controls against the same production path,
each with a new output file. A future repeat must also use fresh outputs. For
example, the positive control's eight calls include its final answer:

```bash
uv run --no-sync python .agents/labs/planning_progress_evaluation.py calibrate \
  --case positive --repo "$PWD" --head "$(git rev-parse HEAD)" \
  --output /tmp/OWNED-EVIDENCE/planning-positive.json \
  --calls 8 --output-each 64 --output-total 512 \
  --input-total 1048576 --seconds 30
```

The `missing-progress` control deliberately omits in-progress assertions and
must fail the progress criterion. It is a calibration of the evaluator, not a
successful planning result. Root owns active controls; the helper author and
independent reviewers did not replay the calibration.

`pilot` uses the same fixed task and production path with a root-selected
existing model configuration. The private configuration must be a current-user
owned, non-symlink, mode-0600 regular file of at most 8 KiB, with exactly
`base_url`, `model` and an absolute `api_key_file`. The credential file has the
same ownership/type/mode requirements and a 4 KiB bound. HTTPS is accepted;
plain HTTP is restricted to loopback. Redirects are disabled. Neither private
file, its contents nor its hash is exported or printed. Root must retain these
private files outside the repository and public evidence.

## Task, authority and observations

The synthetic task has two stable step IDs: `inspect` checks the supplied toy
observations, and `report` remains blocked because an independent third
observation is absent. A passing result requires an answered run with no
failure, matching current scope and steps, each step's in-progress assertion
observed before its terminal assertion, and an explicit `partially_achieved`
terminal outcome with remaining work. Those are attributed workflow claims,
not scientific verification.

Observations come from a second read-only canonical control-store connection
after actual tool commits, before any saved run record. Start evidence includes
the original plan ID, progress sequence and step fingerprint. A replacement
plan cannot reuse another plan's starts. A same-plan revision can retain start
evidence only for unchanged intent. The evaluator records criteria; it never
creates missing progress or infers completion from host work.

The helper creates only a fresh marked local synthetic control fixture. It
constructs no `WorkerClient` instance or worker network session. All returned
non-plan tool requests, including a mixed batch, are refused before tool
dispatch. No worker, research code, file-read tool, browser or
VM action is authorized by the fixture. Ordinary production tool schemas and
authority checks remain unchanged.

## Bounds, output and interruption

Required finite limits cover application calls (1–12), requested output tokens
per call (64–1,024), cumulative requested output reservation (at most 8,192),
cumulative input bytes (128 KiB–2 MiB), and wall time (5–180 seconds). Per-call
input and response bounds are 128 KiB and 32 KiB. A request is refused if its
remaining reservation is insufficient. Input bytes do not imply input tokens;
requested output reservations are not proof of provider-enforced spend limits.

The wall deadline starts before fixture setup; the production loop receives
only the remaining time. Interrupted or timed-out provider effects remain
unknown and are never automatically retried. Owned reads and cleanup drain
through repeated cancellation, so cleanup can outlast the deadline. Fixture
removal requires its exact owned identity and successful component cleanup.

Public JSON content is bounded to 32 KiB, plus its final newline, and reserved
exclusively before private configuration or model work. It contains source/helper pins, fixed criteria,
budgets, counters and cleanup results. It excludes arbitrary model output,
transcripts, plan notes, provider errors, private configuration, credential
values and their hashes. A noncompliant model result remains a failed
measurement. In active modes, exit 0 means the evaluator's criteria passed; exit
3 is an observed negative/unknown evaluation, and exit 2 is refusal or
setup/publication failure.

This helper proves no actual worker-start/progress ordering, live USER-edit
race, browser delivery, saved `RunRecord`/notice ACK behavior, scientific
correctness, provider wire receipt or durable call/spend accounting. Root owns
any future calibration, one small finite model pilot before broader cases,
evidence review and complete cleanup. The completed scripted controls establish
the local measurement path and its deliberate negative, rather than model
adherence to planning guidance.
