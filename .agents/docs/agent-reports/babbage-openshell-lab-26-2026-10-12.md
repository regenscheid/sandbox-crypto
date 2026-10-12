# Passive kernel observations in OpenShell on babbage

Root ran fresh disposable Lab26 on signed
`a6bb469a35541f6daa308a4c85a808e96bcc08a2`, with independently reviewed observer
product `8df49fe` and the test-only readiness correction `89bd8a7`. The new
observation route neither opens an executor nor refreshes ordinary idle activity.
Continuous observation allowed the real managed VM to stop; further observations
did not wake it. An ordinary cell resumed execution and disclosed lost kernel state.

The [exact evidence](babbage-openshell-lab-26-evidence-2026-10-12.json) retains
API results, safe SQL/gateway projections, source/helper pins, passing and failed
gate logs, independent review and complete teardown. Every embedded text record
has a byte length and SHA-256. Private settings, tokens, provider records, TLS
keys and raw journals are excluded. No real credential or model was used.

| Runtime | Observed configuration |
| --- | --- |
| Host / service Python | babbage / 3.13.15 |
| OpenShell | 0.1.3, VM driver, loopback mTLS |
| Image / isolation | `sandbox-science/minimal:02f85b3`, effective and declared tier 3 |
| Scope | `lab26` / `lab26-kernel-observation` / sessions `a` and `empty` |
| Sandbox | `ss-f795da569789df25` |
| Immutable sandbox ID | `fcbd772a-8482-4262-a934-8b8ba49ea9c0` |
| Initial guest boot | `589ed702-68c5-45e9-92f4-3e69461fb014` |

The accepted [helper](../../labs/kernel_observation.py) SHA-256 is
`33abd17b0592c30fe008225f1e71fa8a11fdd42ff875371d3a5b7bbf4d8e74c6`.
Its existing ownership/marker/loopback/project/profile guards were independently
checked with ten disposable local probes. Root alone started, restarted and removed
services. The separate bounded SQL inspector exports safe scalar metadata only;
its phase labels and two database reads do not assert live or atomic state.

| Check | Result |
| --- | --- |
| Before first executor connection | Observation unavailable, with no attach |
| Connected observations | Twenty exact kernel listings; birth/epoch/generation rows unchanged |
| Scope / client | Empty session empty; foreign project and unauthenticated request refused; bounded client agrees |
| Graceful worker restart | Startup recovery already reconnects; fifteen observations preserve exact pins, rows and environment |
| Continuity before idle | Same VM, daemon endpoint, guest boot, kernel generation 1 and global value 1729 |
| Continuous idle observations | 49 calls over 9.983 seconds: 47 successful, two unavailable during release; suspension completes |
| Actual VM stop | Independent gateway projection reports the same immutable sandbox `Stopped` |
| After stop | Thirty unavailable observations plus typed client unavailable; environment and stored rows unchanged |
| Ordinary resume | Generation 2, old global absent, `kernel_state_lost=true` |
| Historical result | Original terminal execution exactly unchanged |

Root validated four distinct typed executions and an exact prior-execution replay;
their start/result kernel pins agree. The remembered endpoint across worker restart
is identical. Private worker/client token values were absent from the selected
retained runtime records before teardown.

The full Python suite passed **9,926 tests**, 43 opt-in skips and two existing
serializer warnings on `89bd8a7`; all package/test bytes are unchanged on accepted
`a6bb469`. Static/license/protocol checks, 575 web tests, both builds and 24
post-build math checks pass; the helper-only correction passed fresh Ruff checks
and independent review. Fresh exact-head PR CI remains a separate delivery gate.

Two preliminary failures are retained explicitly. The first full suite failed an
existing test's immediate ownership read after `Popen` (9,925 passed). A 500-child
probe reproduced 202 transient unreadable owners, all live and subsequently
readable. The test now waits for the child's bounded readiness signal while
preserving real cleanup and sibling-survival assertions; the full rerun passed.
The first lab helper incorrectly expected unavailable status after worker restart.
Existing startup recovery reconnects remembered endpoints before the API serves,
so its fifteen successful observations were correct. That lab was completely
removed; the accepted fresh run above uses the corrected reviewed helper. No
product assertion was relaxed or product source changed for either correction.

Root teardown reported zero sandboxes, stopped every owned transient service,
removed 1.1 GiB of lab state and freed ports and driver sockets. Its process match
is the parent shell containing the teardown command text. Independent teardown
verification at 22:02:47 UTC found all seven units inactive, six ports free and no state/socket/process residue; a second executable scan at 22:04:17 UTC found no hypervisor or OpenShell process. The final SQL checkpoint had already idled stopped again; its `connected` phase label is only an operator label, and its generation mapping still proves the resumed normalized generation 2. No GUI poller, cross-session inbox,
automatic agent notification, package install or new concurrent CP-F7 run is
claimed. Closed-channel races, timeouts, admission and CP-13 holds remain local
hostile-test coverage; `observed_at` denotes host receipt, not an atomic snapshot.
