<!-- SPDX-License-Identifier: MIT -->
# Pilot before a large computation

General MAIN research guidance asks for a small representative pilot before
starting a long or large computation, or reuse of applicable current pilot
evidence. This applies without selecting an optional research template.

Check correctness against known cases or invariants and check output shape.
Resolve failed pilot checks before scaling up. Record observed runtime and memory
when available, state missing measurements or a nonrepresentative trial, and
qualify scale estimates with their assumptions, including nonlinear time or memory
growth. A smoke test does not prove correctness or feasibility of the full
computation. A successful process exit is not a substitute for those checks.

When the actual plan tools are offered, record the pilot, validation evidence and
scale-up decision there before the large run. Existing relevant observations may
be reused; trivial work needs no ceremonial pilot. Trials and larger runs stay
within existing budgets and authorization. Unknown effects must be reconciled
before repeating them. This adds no mandatory USER approval gate.

## Where it lives

The guidance is short original text in `SYSTEM_PROMPT` in
[loop.py](../packages/orchestrator/src/sandbox_science/orchestrator/loop.py), part
of MAIN base content version `sandbox-main-9` (`MAIN_VERSION`). There is no measurement or cost-estimation tool, enforced
computation classifier, trial executor, additional model call, controller decision
or permission for pilots. Nothing mechanically certifies representativeness,
correctness or a scaling estimate; actual results and missing capabilities still
need honest reporting.

System assembly is the base, an optional [research template](STOCK-RESEARCH-PROMPTS.md)
and the conditional [planning guidance](ORCHESTRATOR-PROMPTS.md#implemented-stateful-main-planning-tools),
joined by two newlines ([MAIN provenance](MAIN-PROMPT-PROVENANCE.md)).

Run records made before this guidance carry base version `sandbox-main-1` and keep
their exact system text; reading them does not backfill today's guidance. A caller
that supplies the old text explicitly gets those exact bytes with custom-content
provenance, not a false built-in pin, and combining that custom base with a
template is refused. Safe failed runs keep the current base pin, fixed failure
classification and no final answer.

## Test limits

Scripted actual-input tests cover default and selected assembly with and without
plan tools, unchanged tools and call count, an immutable `sandbox-main-1`
historical fixture, old-text custom callers and safe failure pins. They prove
input, version and history behavior, not that a live model chose a useful pilot or
that a large research computation succeeded. No live research computation or
model-quality evaluation has been run.
