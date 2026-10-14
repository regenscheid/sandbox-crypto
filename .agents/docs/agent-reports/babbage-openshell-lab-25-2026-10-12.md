# Kernel start revisions in OpenShell on babbage

Root ran the sequential CP-12 kernel-revision continuation on 2026-10-12 against
clean signed worker source `629e27d39a7f2d17c6dc5f7d89e8a5f85c5eeb25`.
Its product bytes equal independently reviewed `2135e16`; its only child change
places an existing assertion in the intended test. A real package install changed
revision 0 to 1 without replacing an existing kernel. Graceful worker restart
preserved that kernel and the same VM; explicit kernel restart replaced it.

The exact archive (archived outside git, sha256 `1f89881098c7`) retains API
results, endpoint identities, the reusable helper, completed and preliminary gate
logs with their source provenance, the early connection failure, independent
review and complete teardown. Every embedded text record includes its exact byte
length and SHA-256. Private token/provider files and raw service journals are
excluded. No real model or real credential was used.

## Runtime and source

| Component | Observed configuration |
| --- | --- |
| Host | babbage, Ubuntu 26.04; service Python 3.13.15 |
| Gateway | OpenShell 0.1.3, MicroVM driver, loopback mTLS |
| Sandbox | `sandbox-science/minimal:02f85b3`, Python 3.14.8, kernel 6.12.76 |
| Isolation | Effective and declared tier 3; hidden host sentinel |
| Scope | Project `lab25`, environment `lab25-kernel-revisions`, sessions A/B/empty |
| Sandbox identity | `ss-599dab3f3605be6f`, immutable ID `841cda99-b049-4f1a-994d-82ab0a831ad1` |
| Guest boot | `5033c933-e917-466b-b1f8-c50d226ef363` |
| Services | Four owned transient units: gateway, middleware, package cache, worker |

The reviewed [helper](../../labs/kernel_revision.py) SHA-256 is
`132aee5c1045f40a90d1d288facbf03fa4d36b52de63245e837957b9d72ed2b4`.
Its mandatory owned-state marker, loopback listener, project scope, daemon mode
and managed minimal-image checks precede requests. Root alone managed service
startup/restart/teardown. Product, helper, assertions and images stayed frozen
during acceptance. Actual table main `12ddc60` is subsequently integrated at
signed `3fa211c`, preserving every CP-12 product byte and all table/history paths.

## Observed results

| Check | Result |
| --- | --- |
| Initial A | Generation 1, start/current revision 0, global `cp12_value=1729`; networkx absent |
| Real provisioning | Successful permitted Conda networkx install; environment revision 0→1 |
| Retained A | Generation 1, start 0/current 1, `predates`; global still 1729 |
| New B | Generation 1, start/current 1; actual import reports networkx 3.7 |
| Scoped status | Empty session returns no kernels; foreign project returns policy denial |
| Worker restart | Same immutable VM, boot, daemon incarnation, A generation/start and global |
| Explicit A restart | Generation 2, start/current 1; old global raises `NameError`; new global 2026 works |
| Historical execution | Original initial execution equals its post-restart API copy exactly |

Independent read-only inspection validated two environment records, seven
executions, two provisioning operations, four kernel listings and the explicit
restart response against current typed contracts. Every execution's top-level
kernel pin equals its result pin. The bounded endpoint SQL records before/after
worker restart are identical, SHA-256
`666d38418c72898508db568f1e0531b19a46fe22e96625ab9dce9a2c8d1772c2`.
The full API record SHA-256 is
`be63961f4c895dafc0d0794ffbcef9f0f7f2da1d1895c6500efa7424edfa2595`.

Root launched the first post-restart probe before the worker listener was ready.
It failed on its first read-only status connection, before any successful stage
or mutation. The separate failure log is retained. Root waited for listener
readiness and reran the unchanged helper; every continuation assertion passed.
This is a probe startup race, not a relaxed assertion or hidden product failure.

## Validation and limits

The independently reviewed product milestone passed 9,789 Python tests, 43 opt-in
skips and two existing serializer warnings. Older source-milestone logs are
retained with their identities rather than presented as final combined evidence.
Fresh actual-main union validation is recorded in the delivery section below.

This run proves the sequential kernel-revision continuation. It does not repeat
the complete CP-F7 concurrent-linking/overlap scenario, induce an abrupt
daemon/VM crash, validate every CP-13 race or prove actual loaded module versions
from package fingerprints. Ordinary result/tool-transcript disclosure is
implemented; automatic live GUI/cross-session notifications remain open. There
was no native GUI acceptance for this worker feature. Restoration, package UI,
uninstall/upgrade and producer/input lineage remain separate work.

## Cleanup

Root archived token-free records before invoking the existing owned lab teardown.
The gateway reported zero sandboxes. All transient units stopped, the 1.2 GiB lab
state was removed, driver socket directories disappeared and all six lab ports
were free. The teardown log's process match was its own parent shell containing
the script text; it was not a residual service.

Independent verification at **2026-10-12 20:34:35.276855 UTC** found all seven lab
unit names inactive/collected, no lab state or OpenShell user config/state/share
paths, no driver socket directories, no listeners on 17670/50051/8090/8080/8100/8101
and no actual lab/VM/executor process candidates. All 32 pre-teardown archived
files matched their recorded hashes. No lab service, VM or private credential
state remains.

## Delivery

Final signed product union `3fa211c` plus independently reviewed test-only child `f8a72f6` passed 9,858 Python tests (43 opt-in skips, two existing serializer warnings), 571 web tests, 24 post-build math checks, both production builds and all locked/static/license/protocol checks. The fixture-only child leaves completed web/math/static product validation unchanged; fresh Ruff checks include the helper and changed tests. Final exact-head documentation/archive review and fresh PR CI remain delivery gates.

The parallel predecessor had one detached-wrapper test exit with SIGKILL (9,856 passed). Two suites reused a global test cleanup owner, creating a demonstrated cross-suite process-cleanup hazard with matching timings; the exact signal sender was not retained. The test-only correction assigns unique fixture owners and adds a real sibling-cleaner isolation regression. Five focused cases passed in concurrent processes; the final full rerun above passed. Product/helper bytes remain exact Lab25. Both full-run logs and narrow results are retained.

The archive contains 57 exact text records, SHA-256 `1f89881098c764f301b1b0570b561bea1d8ef04175d39580f750dec6e808a044`.
