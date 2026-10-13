# OpenShell v0.1.3 Lab28: passive original kernel lineage

Root ran this lab on babbage from independently reviewed signed source
`1deb9b72eae6e62cfa7c5592cd6f5970409743c7`. The passive lineage product is
byte-identical to author `02afaedf6e36e8be5b43dd8731ba34ec43a984e5`; the separate
internal notice inbox matches `0b8cf17a1099e57d4572e6597c51a66217994118`.
Helper `.agents/labs/kernel_lineage.py` is pinned by SHA-256
`4d90664b11b73f118f01b17a5650ecad123398aff94a25cd509a8832c6e52de8`.
The [exact evidence archive](babbage-openshell-lab-28-evidence-2026-10-12.json)
retains 39 text records with byte lengths and SHA-256. No raw gateway credential
annotations or private provider registration output are exported.

## Result

Actual OpenShell VM execution at declared/effective tier 3 passed. The single
owned VM `ss-70b0d47529bce9d8` has immutable ID
`6591d39e-608b-4066-8c20-57e5162ecb24`, using
`sandbox-science/minimal:02f85b3`. Host development Python was 3.13.15; this
helper did not remeasure the guest interpreter version.

| Observation | Retained result |
| --- | --- |
| Package change | Session A installed networkx 3.7; revision 0 → 1. Original A birth/start 0 remained exact and predates 1; fresh B started at 1 |
| Passive route/client parity | 55 typed successful lineage observations in total, official client and unchanged flat GUI observer; empty session and foreign scope checked |
| Worker restart | Same original birth/hash, immutable VM, guest boot and global 1729; bounded retained SQL unchanged |
| Explicit kernel restart | New birth and normalized generation 2 in the same daemon incarnation; old global absent, new global 2026 |
| Idle polling | 45 observations: 43 HTTP 200, two HTTP 503; actual VM stopped |
| No wake | Thirty further HTTP 503 responses; environment and birth/endpoint/generation/execution SQL unchanged; gateway independently reports the exact original VM stopped |
| Ordinary resume | Same immutable VM, new daemon incarnation and guest boot; raw generation 1 maps to normalized 3, old global absent, state loss disclosed |
| Retained result | Original terminal execution unchanged after resume |
| Executions | Seven unique terminal executions: six successes and one expected NameError when checking the removed global |

The original incarnation is `inc-b06991fba3a3dbade7ad9719`; ordinary resume
reports `inc-dd8da966e654ea7616f748e6`. Raw and normalized generations remain
distinct; the route never fills in an original start or VM from today's state.
The gateway proof is a bounded, dated projection tied to this run/source/helper.
Its independently reviewed projector exports only safe fields plus raw output
length/hash. It does not establish continuous or atomic VM health.

## Preparation and cleanup

The first setup lacked the synthetic `github-readonly` provider and failed
before the first execution. That disposable lab was completely removed. A fresh
provider registration then exposed the missing imported profile before its worker
or environment started; importing the documented profile corrected the fixture.
Separate preliminary logs retain both setup failures. The accepted product and
helper were unchanged. Only an obviously synthetic provider was used; no external
account credential or external model was involved.

Root teardown reports zero sandboxes and 1.2 GiB removed. Independent read-only
cleanup at **2026-10-13T00:27:45.638866Z** confirms all seven named lab units
inactive, no lab state/XDG paths, owned process, driver socket, lab listener or
hypervisor. A concurrent unrelated pytest LocalBackend fixture in root's teardown
log was not Lab28 state and was gone by the independent check. Cleanup makes no
claim that no executor can exist elsewhere on the machine.

## Local gates and boundary

Root full Python on product-identical signed union `31a53f3` passed **10,153 tests**,
43 opt-in skips and two existing serializer warnings. Required locked/static,
license and generated-protocol checks passed. Author focused coverage passed 139
lineage and 45 inbox cases; the helper's 28 disposable guard/SQL/DTO/gateway probes
and style checks passed. The unchanged GUI source/builds reuse accepted PR #99
614 web and 24 post-build math cases. Final integration gates are recorded below
after integrating the separately reviewed prompt changes. Signed union `840b48f240c06a1b1839c67805211d7018f7ee38` passes **10,155 Python tests**, 43 opt-in skips/two existing warnings and required static checks. Actual prompt PR #100 main `c14d1ea412591aa8793b91da0a071a7d9416286a` is integrated with no additional product/helper/test/config changes; the two added prompt-input tests explain the count increase. Worker/helper bytes remain exact accepted Lab28. The archive retains the corrected frozen 139-case lineage log; its independent archive review records the earlier draft predecessor-log mismatch and closure.

Missing/corrupt/legacy/unmanaged birth and unknown-comparison cases were tested
with local hostile fixtures, not manufactured in this real lab. The internal inbox
has bounded claims, leases, ACK receipts, holds and replay; it was validated with
local persistence probes only. This lab does not deliver notices to MAIN, save or
ACK a model run, wake an idle agent, re-enroll a changed binding, add a GUI route,
or complete CP-12. Transport authentication, control session lifetime, actual
safe-point delivery/save-before-ACK and replacement reconciliation remain open.
Fresh exact-head PR CI is a separate delivery gate.
