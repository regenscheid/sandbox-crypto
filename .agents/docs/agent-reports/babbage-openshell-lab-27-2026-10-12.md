<!-- SPDX-License-Identifier: MIT -->
# Visible session kernel observations in OpenShell on babbage

Root ran disposable Lab27 and the production workspace browser on signed
`70f6761a0e9d58b4ad54dc3338812befd21b99d5`. Backend author `a9beaa8` and frontend
author `b49da60` were independently reviewed; all ten product/test paths remain
byte-identical. The authenticated GUI observed a real shared-environment package
change without executing another cell in the stale session. Passive observations
allowed both real VMs to suspend, and subsequent reads did not wake them.

The exact evidence archive (archived outside git, sha256 `27545cfd2f97`)
retains safe API/SQL/gateway projections, browser accessibility/network records,
source/build/helper pins, local gates and independent review/cleanup. Embedded text
records have exact byte lengths and SHA-256 hashes. Private settings, login URLs,
cookies, credential headers, tokens, TLS keys, provider records and raw gateway
records/journals are excluded. Only disposable local authentication tokens and a
visibly synthetic provider value were used. No external account credential, model
or converter was used.

| Runtime | Observed configuration |
| --- | --- |
| Host / service Python | babbage / 3.13.15 |
| Actual guest Python | conda-forge 3.14.8 |
| OpenShell | 0.1.3, VM driver, loopback mTLS |
| Image / isolation | `sandbox-science/minimal:02f85b3`, declared/effective tier 3 |
| Shared scope | `lab27` / `lab27-default-0f97a1dee905` / A, B, empty and closed sessions |
| Shared immutable VM | `ss-6c62b5e6542f5d44` / `e0fb044f-bdf3-44c6-8d0f-2ee76f0db446` |
| Dedicated C scope | `lab27` / `lab27-c-dc700c436723` / C |
| Dedicated immutable VM | `ss-4e767c9d7c8a2c81` / `5a98cb28-a34f-467c-bff4-20201a213eaf` |

The accepted [runner](../../labs/kernel_gui.py) SHA-256 is
`e4f1a6245432d8e5074fbdeba11824870471701c322a247c5eb5f0067d719939`;
the [bounded read-only inspector](../../labs/kernel_gui_inspect.py) SHA-256 is
`fde1ecde71b170757018ab69b309b173e79a7ca3162f2935d8b8ab0510ffb6cb`.
Ownership/source/loopback guards and mocked failure/cleanup paths passed separate
probes. Root alone operated services and VMs. The two inspector database reads
are bounded receipt-time projections, not an atomic live snapshot. A reviewed
one-use local browser login bridge changed no product authentication behavior;
it stopped after use and its listener was removed.

| Check | Result |
| --- | --- |
| Initial connection | Unavailable before executor connection; healthy empty session is empty |
| Scope | Foreign worker project is unavailable 503; absent/closed session is 404 |
| Initial A / C | Generation 1, start/current revision 0; globals 1729 / 314159; networkx absent |
| Real B install | networkx 3.7 changes shared revision 0→1; GUI A shows predates with original start 0 and no A execution |
| Fresh B / dedicated C | B starts at revision 1; C remains current at 0 with its global unchanged |
| Explicit A restart | Generation 2, start/current 1; old global absent; same guest boot |
| Graceful worker stop/start | GUI shows unavailable and dated previous state, then recovers; A global 2026, C global 314159, boots, VM IDs, endpoint/birth/generation pins unchanged |
| Continuous idle observations | 44 reads over 9.768 seconds: 43 successful, one unavailable; both VMs suspend |
| Actual stopped state | Gateway reports the exact two original immutable VM IDs Stopped |
| Further stopped reads | Thirty 503 responses; retained environment, endpoint, birth, generation and execution identities unchanged |
| Ordinary A resume | Generation 3, old global absent, `kernel_state_lost=true`; original terminal execution unchanged |
| Browser lifecycle | Closed session has no kernel panel; no kernel requests after logout cursor |

Root and reviewer typed-validated eleven successful executions with matching
start/result kernel pins. Reviewer validated 47 successful retained observations.
The browser network projection retains 198 kernel events without truncation or
eviction. The final post-resume browser capture shows dated previous generation 3
and unknown current state: the deliberately short idle threshold had already
stopped the VM again. It is not evidence of a final active kernel.

*A observes a shared package change without another A execution: screenshot `babbage-openshell-lab-27-stale-2026-10-12.jpg`, archived outside git (sha256 `abc849ac8184`)*

*Stopped environment retains a dated previous observation: screenshot `babbage-openshell-lab-27-idle-2026-10-12.jpg`, archived outside git (sha256 `524da19f2542`)*

Native hidden-tab pause was not proved: browser presentation hiding and a second
tab both left `document.visibilityState` visible in this automation surface.
The 39 focused validator/lifecycle/transport cases cover hidden state, ignored
abort, late replies and logout fencing; those tests do not substitute for native
visibility acceptance. Browser checks used the exact 70-file production build.

Local combined gates pass **10,032 Python tests**, 43 opt-in skips and two existing
serializer warnings; **614 web tests**, **24 post-build math checks**, both builds
and locked/static/license/protocol checks. Fresh exact-head PR CI remains a
separate delivery gate. No product, test, helper or configuration changed during
runtime acceptance; subsequent changes record documentation and evidence only.

The workspace runner removed its private login file and listeners. Both browser
tabs were closed. Full lab teardown records zero sandboxes and removed its marked
state. Independent read-only verification at **2026-10-12 23:55:19.738364 UTC**
found all seven units inactive, no marked state/private paths, lab/GUI/bridge
listeners, driver socket directories, owned processes or hypervisor processes.

This accepts visible-session eventual GUI observations. Durable agent inboxes,
save-before-ACK transport, cross-session agent delivery, idle scheduling, package
restoration and protected document staging/conversion remain separate open work.
