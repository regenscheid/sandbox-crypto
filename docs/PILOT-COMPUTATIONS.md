<!-- SPDX-License-Identifier: MIT -->
# Pilot before a large computation

General MAIN research guidance now asks for a small representative pilot before
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

## Implemented boundary

This is short original instruction text in
[loop.py](../packages/orchestrator/src/sandbox_science/orchestrator/loop.py), with
the MAIN base content version changed from `sandbox-main-1` to `sandbox-main-2`
in [prompt_bundle.py](../packages/orchestrator/src/sandbox_science/orchestrator/prompt_bundle.py).
It adds no measurement or cost-estimation tool, enforced computation classifier,
trial executor, additional model call, controller decision or new permission.
It does not mechanically certify representativeness, correctness or a scaling
estimate. Actual results and missing capabilities still need honest reporting.

The optional [stock research framing](STOCK-RESEARCH-PROMPTS.md), its five text
versions and the conditional [planning guidance](SESSION-PLANS.md) are unchanged.
System assembly remains base, optional stock, optional actual planning guidance,
using the same separators. Assembly versions `sandbox-main-bundle-1` and
`sandbox-main-bundle-2`, tool schemas/grants/order, pure encoder, dispatch,
closing and durable save-before-ACK are unchanged.

Historical RunRecords retain their original exact system text and base version;
reading a `sandbox-main-1` record does not backfill today's guidance. A caller
explicitly supplying the old literal keeps those exact custom system bytes and
receives custom-content provenance, rather than a false current builtin pin.
Combining that differing custom base with a stock selection remains refused.
Safe failed runs retain the actual new base pin, fixed failure classification and
no final answer or fabricated delivery.

## Validation limits

Focused scripted actual-input tests cover default and selected assembly with and
without plan tools, unchanged tools/call count, an immutable real builtin-1
historical fixture, old-literal custom callers and safe failure pins. These prove
input/version/history behavior. They do not prove that a live model chose a useful
pilot or that a large research computation succeeded. Root owns combined gates,
delivery and any separately authorized runtime or model evaluation.

Author validation passes 9 new cases and 265 affected cases, whole-orchestrator
typing, focused lint/format and installed-distribution licenses. Independent
scripted probes also verify all five selected inputs and retained builtin-1
histories. No live research computation or model-quality evaluation was run.
