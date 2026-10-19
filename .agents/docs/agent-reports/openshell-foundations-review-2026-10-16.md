# OpenShell foundation review

Reviewed 2026-10-16 against main `57e29536`. This is a source/configuration review with local fixture tests, not a new OpenShell runtime lab. The four personal gateway, middleware, package-cache and launcher units were inactive when inspected; none was started or changed. Credential files were not read.

The overall division is appropriate: the orchestrator coordinates research, the trusted worker controls environments, and OpenShell supplies execution isolation and runtime lifecycle. Preserve that division. The remaining research-tree recovery belongs in the orchestrator and must use the worker's existing reconciliation mechanisms. One orchestrator Resume should cover the selected tree, as the owner confirmed in D87.

## What is correctly wired

- The installed CLI, gateway, supervisor, sandbox and VM-driver executables each report `0.1.3`. The personal gateway configuration selects `compute_driver = "vm"`, loopback and mTLS. This matches the [versioned runtime model](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/runtimes); version strings alone do not verify binary provenance or a live boundary.
- The worker creates managed sandboxes from operator-selected image profiles and compiled policies, attaches only named providers with `--no-auto-providers`, and checks readiness. Provider profiles that would introduce extra credential endpoints are refused. See [environments.py](../../../packages/worker/src/sandbox_science/worker/environments.py), `_check_providers` and `_create_sandbox`.
- Research execution reaches the sandbox through `sandbox exec --no-tty --no-login-shell`; the normal OpenShell backend does not fall back to local execution. The harness requires isolation tier 3 by default, with an evidence-based downgrade/refusal check. The probe is a misconfiguration check, not independent proof against a compromised sandbox.
- The worker's daemon transport reconnects to existing executions with bounded event replay. It preserves the original attempt and deadlines, and fences replacement work when a daemon incarnation changes. Normal environment deletion confirms absence. These are useful foundations to reuse, rather than rebuilding worker recovery inside the orchestrator.
- [Lab 20](babbage-openshell-lab-20-2026-10-12.md) records real OpenShell worker/bridge restart, offline completion and kernel-continuity checks, plus a corrected VM fence after daemon loss. That is historical evidence at the report's commits, not live validation of today's checkout.

## Findings

### P1: failed-creation cleanup can lose ownership of a live sandbox

In [environments.py](../../../packages/worker/src/sandbox_science/worker/environments.py), lines 614–626, retrying a `creating` record first saves the new sandbox name and only then discards the old sandbox. The exception path also marks the new environment `deleted` after `_discard`. `_discard` at lines 1240–1246 merely logs a delete failure and never verifies absence. A timeout or unavailable gateway can therefore leave compute behind while the current environment record says deleted or names its replacement. The normal `_delete` path already performs the stronger absence check.

[OpenShell's lifecycle contract](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/overview) explicitly distinguishes deletion acceptance from completed cleanup and allows create/start work to continue after a caller disconnects or times out. Keep durable cleanup obligations for every original sandbox and reconcile them before replacing or declaring deletion. RC-26/F18 cover this. This finding is established from control flow; no live failure was injected. Existing failed-creation fixtures exercise successful cleanup, not a pending/failed delete.

### P1: policy resource fields do not all reach the VM boundary

The policy [Resources model](../../../packages/policy/src/sandbox_science/policy/model.py) accepts CPU, memory, PID and workspace-disk limits. The [compiler](../../../packages/policy/src/sandbox_science/policy/openshell.py) says resources are applied by the driver, but `_create_sandbox` does not translate those fields into allocation settings. Worker callers of `effective_resources` use job time/concurrency and provisioning wall time; they do not implement those four caps.

The personal gateway instead gives each VM the configured 8 vCPUs, 16 GiB RAM and 32 GiB overlay. These are real configuration bounds, but they are not enforcement of a smaller project or pool policy. Client environment-count limits and job-count limits exist and should remain; neither establishes aggregate RAM admission. Upstream also documents that generic `--cpu`/`--memory` flags do not change allocation for the VM driver in v0.1.3, so adding those flags alone would be insufficient. See the same [sandbox management documentation](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/overview).

Before relying on resource policy for long autonomous runs, map supported constraints to verified driver controls and reject or explicitly report unsupported ones. Record effective VM allocation separately from requested policy, and account for concurrent environments, including the proxy. No claim of an unbounded VM or a measured exhaustion failure is made.

### P2: idle suspension does not know whether research still needs a kernel

The inspected personal configuration sets `suspend_after = 3600`. [suspend.py](../../../packages/worker/src/sandbox_science/worker/suspend.py), `_idle`, uses worker activity, environment changes and kernel CPU. It has no research-tree ownership signal. A long literature/model-only interval can outlive this threshold and lose previously computed kernel globals even though the research task remains active. The same risk applies to untracked low-CPU background work; accepted worker jobs are tracked as active.

Stop/start retaining files is appropriate OpenShell usage. It does not preserve kernel memory. Add bounded, renewable environment claims or an explicit checkpoint/release policy tied to research ownership (RC-25/F17). Expiry and pool limits must still prevent abandoned work retaining resources indefinitely.

### P2: the normal launcher does not provide the documented orchestrator sandbox

[parts.py](../../../packages/launcher/src/sandbox_science/launcher/parts.py), `harness_argv`, and [supervise.py](../../../packages/launcher/src/sandbox_science/launcher/supervise.py) start the run service as a host subprocess. Research execution still goes through the isolated worker, but the control application itself does not receive the filesystem/egress boundary of the separately tested orchestrator image. Environment-variable filtering is not process isolation.

The original [images description](../../../docs/IMAGES.md#the-orchestrator-image) read as though every harness used that sandbox. This review corrects the documentation to distinguish the image/lab from the normal launcher; it does not add control-plane confinement. The owner subsequently confirmed D47: a separate restricted orchestrator sandbox, with trusted worker/OpenShell management outside, and durable scoped state access resolved before launcher integration. RC-27 to RC-29 and RC-F19/F20 track implementation and acceptance; confinement is not yet deployed. Shared research environments likewise remain a shared trust domain, despite separate agent kernels.

## Release and operational follow-ups

- [OpenShell v0.1.4](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.4) was released October 13. Its changes include stop-lifecycle concurrency handling, credential placeholders, VM image-digest handling and upload backpressure. Qualify the complete release set in a disposable fixture lab before upgrading; the inspected installation remains consistently on 0.1.3.
- The personal bootstrap/default image references use a tag, and research profiles use source-revision tags. The research image build itself pins dependencies and prints a digest. Add an installation manifest for the resolved runtime binaries and image identities; a source-revision tag is not an immutable deployed-image identity. No unverified claim is made about what image a stopped VM last used.
- Treat application interruption, worker/bridge disconnection, daemon death, VM stop and host loss as separate recovery cases. The in-VM daemon journal is bounded and volatile. Orchestrator-level Resume must report lost kernel state and unresolved operations, retain completed child outcomes, and leave explicitly stopped branches stopped. It must not require the user to visit each agent.
- Keep OpenShell proposal approval manual and preserve middleware enforcement. Existing on-demand admission recompiles policy under the pool ceiling; it is not a reason to enable upstream automatic raw-connection approvals. Policy reloads can interrupt transfers, so the existing ahead-of-use admission and explicit operation reconciliation matter during long runs.

## Validation and scope

Ran the existing local suites `test_openshell_cli.py`, `test_environments.py`, `test_daemon.py`, `test_recovery_gate.py`, `test_isolation.py` and launcher `test_sandboxes.py` with four pytest workers: **228 passed in 33.82 s**. They cover fake CLI behavior and local executor/daemon fixtures. They do not establish live VM enforcement, the new research-tree resume contract, or the missing failure scenarios above.

Only review, design, tracking and the image documentation were changed. No runtime code, personal settings, services, providers or sandboxes were modified. The research-continuity design and backlog contain the proposed follow-ups.
