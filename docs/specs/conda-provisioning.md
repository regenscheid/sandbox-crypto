# Automatic Conda provisioning

Date: 2026-10-11. **Status: Approved for implementation as drafted (2026-10-11, by the user).** Shell installs go through wrappers (open question 1), and new executions wait while packages link (7); the other open questions are settled as the implementation reaches them, and recorded here. W0 behavior specification; conventions in the [index](README.md). The worker implements the first slice: shell and kernel installs take turns through wrappers (CP-4, CP-11), executions wait while one runs (CP-12), and each change is a revision with a lockfile (CP-16, CP-18, CP-19; [worker](../WORKER.md#package-commands)), which settled open question 2. The second slice is the provisioning tool ([worker](../WORKER.md#provisioning-operations)): requests (CP-1 to CP-3, CP-5, CP-15), dry runs and decisions (CP-6 to CP-9), installs in the environment's turn with the policy's wall time and a visible queue (CP-10, CP-11, CP-14), records and reports (CP-16, CP-17, CP-25), probes (CP-21 to CP-24), and failures that changed the packages reported as `needs_recovery` (CP-13 in part). It settled open questions 3, 5 and 6 for now. The [CP-13 admission prerequisite](../RECOVERY-FENCE.md) now refuses new ordinary work after changed or uncertain managed installs, with exact internal exemptions, actual-launch and original-binding deletion guards; it passed independent review and real OpenShell Lab21, and merged as PR #85 after all three fresh CI checks passed. The [kernel start-revision continuation](../KERNEL-REVISIONS.md) records observed kernel births, preserves exact pins through worker restart and discloses stale kernels in ordinary results and the retained GUI transcript; root-owned [Lab25](../../.agents/docs/agent-reports/babbage-openshell-lab-25-2026-10-12.md) passed the sequential package-change/restart checks. The [visible-session GUI panel](../WORKSPACE-KERNELS.md) passed real browser/OpenShell Lab27 on reviewed signed `70f6761`; fresh delivery CI remains pending. Durable cross-session agent notification remains open. Restoring a revision (CP-13, CP-20), disk limits (CP-14), uninstalling and upgrading through the tool (CP-20) and the GUI (CP-26) are not built yet.

## Scope

An agent asks for a tool or package; the platform installs it into the project or session environment, records the change and checks that the tool works (D16, confirmed). This spec covers the request, the policy decision, installation through the package cache, environment revisions and lockfiles, the functional probe, failure and rollback, and concurrency in a shared environment.

Out of scope: base images and the `crypto-base` recipe (D46); the cache itself ([package cache](../PACKAGE-CACHE.md)); policy evaluation ([policy](../POLICY.md)); environment capture and restore (W3), which consume the records defined here; shell and kernel semantics ([tools](tools.md)).

Terms:

- **Environment**: a project's default environment or a dedicated session environment, described by an `EnvironmentRecord`. Its managed Conda prefix is a declared writable volume (D48).
- **Environment revision**: a counter of the environment's dependency and configuration state. Revision 0 is the base profile's image digest and lockfile. Each completed change adds one.
- **Capability**: a named function a worker can perform, such as `sage`, `gp` or `latex`, with a versioned probe.
- **Probe**: a small functional check run in the environment that passes only on an expected result. An import or version check is a weaker probe and is labeled as one.
- **Operation**: one provisioning request and everything that follows from it.

## Behavior

### Request

- CP-1. An agent requests a capability or package specs with the provisioning tool, naming the target environment (default: the session's environment) and the purpose. The user can make the same request in the GUI. Installation MUST run only in the target environment's worker, never in the orchestrator or on a host (D11, D16).
- CP-2. A capability request checks first. If the capability's probe passed at the current revision, nothing is installed, and the result names the packages and the probe record that satisfy it ([monitoring](../MONITORING.md), Conda workflow step 2).
- CP-3. A versioned capability catalog maps capabilities to conda-forge package specs and probes (new). Each operation records the catalog revision it used. Requests for packages outside the catalog are allowed under policy; their default probe is an import or version check.
- CP-4. Package-manager commands run from the shell or a kernel against the managed prefix (`conda`, `mamba`, `micromamba`, `pip`, `uv pip`) go through wrappers that the environment layer installs. The wrappers hand the command to the environment manager, so it is queued and recorded like a tool request (new). A change to the prefix that no operation explains, detected after each execution, is recorded as an *unmanaged* revision caused by that attempt and flagged in the GUI (new).
  - The wrappers come first on `PATH`, and a `sitecustomize` hook covers the same managers run by the environment's Python as modules (`python -m pip`, IPython's `%pip`). A command that may change the environment (`install`, `uninstall`, `update`, `remove` and the like) takes its turn; one that only reads (`pip list`) runs at once. Until the environment manager exists, a wrapped command runs where it was started, in its turn, and is recorded as a managed revision without the decision, dry-run and probe steps (settled 2026-10-11).
  - A manager run by its full path, or by a Python that ignores `PYTHONPATH` (`python -I`), bypasses the wrappers; its change is the unmanaged revision above. An unmanaged change found when an execution starts was made before it, and names no attempt.
- CP-5. System packages (`apt` and similar) cannot be installed at runtime. A request that needs one fails with `base_image_change_required` and names the dependency (D48).

### Decide

- CP-6. Every package request leaves the sandbox through the package cache, and the policy middleware decides it with the policy's `packages` section. Index reads are allowed unless the ecosystem is denied, package files take the ecosystem's action, and unlisted Conda channels are denied (D71). The hard floor and the pool ceiling apply. The environment manager makes no policy decision; it records the decisions it received.
- CP-7. Before installing, the environment manager resolves the request in a dry run, which needs only index reads, and records the exact package files: channel, subdir, file name, version, build, SHA-256 and declared license (new).
  - pip resolves from wheels only, whose metadata the index serves, so that its dry run builds no source distribution; a PyPI package with no wheel cannot be installed through the tool yet. The Conda install is pinned to the decided builds (implemented 2026-10-11).
- CP-8. If the policy action for any resolved file is `escalate`, the operation creates one `PermissionRequest` whose scope is exactly that file set, and waits in `awaiting_permission` (new). On approval it installs only those files; a fetch outside the set is denied. On denial or timeout the operation ends `denied`, and no package file has been downloaded.
  - The request is a `packages` permission request in the policy middleware's event store, answered like the others (D76); an approval lets exactly the listed files download to the environment's sandbox under that policy revision, until the operation ends, the approval expires or the request's own deadline passes (implemented 2026-10-11, [worker](../WORKER.md#provisioning-operations)).
- CP-9. A resolved set containing an AGPL-licensed package is refused before download unless the project explicitly allows that package; the refusal names the package and its license ([licensing](../LICENSING.md), tier 3; the check is new).

### Install

- CP-10. Solving, downloading, linking, post-link scripts and any pip or build steps MUST run inside the target environment's sandbox, under the same policy, network path and resource limits as its other executions (D16; implementation plan W2). The environment layer points the clients at the cache ([package cache](../PACKAGE-CACHE.md#client-configuration)).
- CP-11. Operations on one environment are serialized in request order; waiting operations are visible as a queue. Operations on different environments are independent ([projects and sessions](../PROJECTS-AND-SESSIONS.md), W3).
  - Shell and kernel installs take the environment's lock in the order they came, as far as the kernel's file locks keep it. The queue is not visible yet.
- CP-12. While an operation links packages, new executions in that environment wait; executions already running continue and are marked as overlapping the change (new). Every execution records the revision current at its start, and a kernel also records the revision at its own start. After a revision change, the agent and the GUI are told which running kernels predate it (new).
  - An execution waits at most its own time limit, which covers the wait; still waiting then, it ends `timed_out` without running and names the operation it waited for. The revision at its start is the one after the wait. It overlaps a change when its start and end revisions differ, or when an operation still held the lock as it ended (settled 2026-10-11).
  - Kernel start/current comparison and retained tool-result disclosure are implemented ([kernel revisions](../KERNEL-REVISIONS.md)). The separate observation-only worker/client route inspects an already healthy connection without waking it or extending ordinary activity; [OpenShell Lab26](../../.agents/docs/agent-reports/babbage-openshell-lab-26-2026-10-12.md) passed actual idle stop/no wake, startup continuity and ordinary resume/state-loss disclosure, with complete teardown. The [visible-session GUI panel](../WORKSPACE-KERNELS.md) passed real browser/OpenShell Lab27 with complete cleanup; fresh delivery CI remains pending. Durable cross-session agent notification delivery remains open. The observer grants no restart or package-recovery authority.
- CP-13. A failed operation leaves the environment at its previous revision. A failure before linking leaves the prefix untouched (outcome `failed`). A failure during linking or in a post-link script restores the previous revision, from a volume snapshot or by rebuilding from its lockfile (outcome `rolled_back`, D48). If restoring fails, the environment becomes `needs_recovery` and refuses executions until it is rebuilt. It MUST NOT be left silently in a mixed state (new).
- CP-14. Installs have wall-time and disk limits from the policy's resources and can be cancelled like any execution ([tools](tools.md)); a cancelled install follows CP-13.
- CP-15. pip inside the Conda environment is allowed through the cache under the `pypi` action. Falling back from Conda to pip for a package conda-forge lacks is recorded as a fallback, with the reason (monitoring, Conda workflow step 4).
  - Through the provisioning tool, Conda and pip packages come in separate requests, the Conda ones first, so that pip resolves against the Conda packages as installed and brings its own copy of one only where a version constraint demands it (2026-10-11).

### Record

- CP-16. A completed operation advances the revision by one and records (monitoring step 4; D09, D48):
  - the request: specs or capability, purpose, requester (agent run and tool call, or the user) and catalog revision;
  - the revision it was planned against, and the revisions before and after;
  - the decisions: policy revision, matched rules and permission requests;
  - the installed files with version, build, channel, SHA-256 and license, and the installer versions;
  - a lockfile for the new revision and the logs, both stored as artifacts; timings; the worker attempt that ran it.
  - Until the provisioning tool exists, a revision made by shell or kernel installs records the package commands that made it (the last 8) and a lockfile the executor writes, which the worker keeps in a store of its own rather than as an artifact. Since research code can write what the executor reports, each environment keeps its newest 500 revisions and the lockfiles of its newest 50, in 64 MiB at most ([worker](../WORKER.md#package-commands)). Settled by the user (2026-10-11), with one rule to come once results record their lineage ([research artifacts](research-artifacts.md)): a lockfile that a recorded result's lineage names is never dropped.
- CP-17. Failed, denied and cancelled operations are recorded with the same request data, the stage where they stopped (resolve, permission, download, link, post-link, probe) and the error. The revision does not advance.
- CP-18. Lockfiles identify packages by channel, subdir, file name and SHA-256, never by the cache's address, so the environment can be rebuilt in another pool (new; [package cache](../PACKAGE-CACHE.md#planned), lockfiles that travel).
  - A lockfile is the platform's own JSON (`Lockfile`): each Conda package by name, version, build, channel name, subdir, file name, SHA-256, MD5 and license, then each distribution pip or uv installed by name, version and installer, with its URL and SHA-256 for a direct-URL install. A rebuild in a pool turns it into an explicit spec with that pool's channel alias (settles open question 2).
- CP-19. The `EnvironmentRecord` points at the current revision and its lockfile, and every execution records the revision it ran under. A dedicated session environment has its own history starting from the base image (D22). Installs into the project environment never change it, nor the reverse (D21).
  - An environment's history starts when the worker creates it: revisions recorded under its ID before, by a sandbox made by hand or an earlier environment of the ID, are not its own (D78).
- CP-20. Uninstalling, upgrading and returning to an earlier revision use the same request, decision, record and probe flow. Returning to revision *n* creates a new revision whose lockfile equals revision *n*'s (new).
  - Returning to revision *n* needs its lockfile, which the worker keeps as CP-16 says: those of an environment's newest 50 revisions and, once results record their lineage, every one a recorded result's lineage names (settled by the user, 2026-10-11, with that rule to come).

### Probe

- CP-21. After a completed install, the requested capability's probes run in the same environment and policy as ordinary executions. The result (`passed`, `failed`, `error`), probe ID and version, output and duration are recorded against the new revision (monitoring step 5).
- CP-22. An import or version check is recorded as `import_check`. It does not satisfy a requirement for a functional probe (CScience distinguishes an import check from a functional witness).
- CP-23. If the install completes but the probe fails, the revision stands and the capability is recorded as failing at that revision. The result says so; nothing is uninstalled automatically.
- CP-24. Capability status belongs to an environment revision. Skill availability and worker routing read it ([skills](skills.md)). Probes run again when a capability is next needed after a revision change, and when a restored environment is verified (W3).

### Report

- CP-25. The tool result states the outcome (`already_satisfied`, `completed`, `awaiting_permission`, `denied`, `failed`, `rolled_back`, `needs_recovery`), the revisions before and after, the package changes, the probe results and, on failure, the stage and a suggested next step. It claims nothing the records do not show.
- CP-26. The GUI shows each environment's revision history, package differences between revisions, licenses with AGPL flagged, probe status and pending operations.

## Contracts used

- `EnvironmentRecord`: base image digest and lockfile at revision 0; current revision; per revision, the lockfile, licenses and probe results; status `ready`, `changing` or `needs_recovery` (proposed additions in the [index](README.md#proposed-contract-additions-new)).
- `ExecutionContext`: the dry run, the install and each probe are executions in the target environment and revision, with the requester as actor and origin.
- `PermissionRequest`: scope is the exact resolved file set; the continuation is the waiting operation, retried with the same operation ID and a new attempt.
- `Mutation`: an operation names the revision it was planned against. Serialization replaces rejection, and the record keeps both revisions.
- `ArtifactRef`: lockfiles and logs.
- `Event`: `provision.requested`, `provision.awaiting_permission`, `provision.finished` (with outcome), `environment.revision_created`, `environment.unmanaged_change` and `probe.finished`, produced by the environment manager and the worker. Per-request package decisions are policy-middleware events.

## Acceptance fixtures

**CP-F1 Install with a functional probe** (`worker`; CP-1, CP-6, CP-10, CP-16, CP-21)
- *Given* a project environment at revision 0 from the `minimal` profile, under the shipped [example policy](../../examples/policy.yaml).
- *When* the agent requests the capability `gp`.
- *Then* one operation moves the environment to revision 1. The revision 1 lockfile lists `pari` with channel, build, SHA-256 and license. The probe runs `print(factor(2^64+1))` in `gp` and passes on `[274177, 1; 67280421310721, 1]`. Cache and middleware logs show every package request decided under `packages`, no request from the sandbox reached a registry host, and nothing ran in the orchestrator container.

**CP-F2 Already satisfied** (`worker`; CP-2)
- *Given* CP-F1. *When* the agent requests `gp` again. *Then* the outcome is `already_satisfied`, the revision stays 1, and the result cites the revision 1 probe record.

**CP-F3 Escalated install, denied then approved** (`worker`; CP-7, CP-8, CP-17)
- *Given* `packages.conda.action: escalate`.
- *When* the agent requests `fpylll`, the user denies it, the agent asks again and the user approves.
- *Then* each attempt creates one permission request listing the resolved files with builds and SHA-256. After the denial the outcome is `denied`, the revision is unchanged and the cache log shows no package-file fetch. After the approval exactly those files are installed; an injected fetch of another file is denied.

**CP-F4 Unlisted channel** (`worker`; CP-6, CP-17)
- *When* the agent requests `bioconda::samtools`. *Then* the operation ends `denied` at the resolve stage with the matched rule, and the revision is unchanged.

**CP-F5 Failed post-link script** (`worker`; CP-13, CP-17)
- *Given* an environment at revision 1 and a local test channel, enabled for the fixture, whose package `ss-bad-postlink` writes a marker file into the prefix and exits 1 from its post-link script.
- *When* the agent requests `ss-bad-postlink`.
- *Then* the outcome is `rolled_back` at stage `post-link` with logs stored; the environment is at revision 1 and its prefix matches the revision 1 lockfile, without the marker. With restoration made to fail by fault injection, the environment is `needs_recovery` and refuses executions until rebuilt from the revision 1 lockfile.

**CP-F6 Install hooks stay confined** (`worker`; CP-10; also in the adversarial suite)
- *Given* a test package in the local test channel whose post-link script writes `/etc/ss-marker` and sends a POST to `https://upload.example.org/`.
- *When* the agent installs it.
- *Then* the write fails on the read-only root filesystem. The POST sends nothing: depending on host admission (D69) it either fails to resolve or is decided as an `http` write, which escalates under the example policy. The operation's record links both observations.

**CP-F7 Serialized installs in a shared environment** (`worker`; CP-11, CP-12, CP-16)
- *Given* workbooks A and B in the project environment at revision 1, and a kernel in A started at revision 1.
- *When* A requests `sympy` and B requests `networkx` at once, and B submits an execution while A's operation is linking.
- *Then* the operations run one after the other (revisions 1 → 2 → 3), each with its own lockfile, and the second records that it was planned against revision 1. B's execution starts after linking and records revision 2. A is told its kernel predates revision 3.

**CP-F8 Dedicated environments stay independent** (`worker`; CP-19)
- *Given* workbook C with a dedicated environment created from the base image.
- *When* the project environment installs `gp` and C installs `python-flint`.
- *Then* the `gp` probe fails in C, the `python-flint` probe fails in the project environment, and each environment has its own revision history.

**CP-F9 Installs from the shell** (`worker`; CP-4)
- *When* the agent runs `mamba install -y tomli` in the shell, then runs `python -m pip install` on a wheel in its workspace, which the wrappers do not intercept.
- *Then* the `mamba` command becomes a recorded operation whose requester is that execution. The pip change is detected after the execution and recorded as an unmanaged revision caused by that attempt, flagged in the GUI.

**CP-F10 AGPL refused** (`worker`; CP-9)
- *When* the agent requests `pymupdf`. *Then* the operation is refused before download, naming `pymupdf` and its AGPL license; no package file is fetched and the revision is unchanged.

**CP-F11 Rebuild from a lockfile** (`worker`; CP-18, CP-20)
- *Given* an environment at revision 3. *When* a new environment is built from the revision 3 lockfile in another sandbox. *Then* both have the same packages, builds and SHA-256 digests, the probes recorded at revision 3 pass, and the lockfile contains no cache host name.

## Open questions

1. Shell-invoked installs: route them through wrappers (proposed, CP-4), or refuse them and require the provisioning tool? Settled 2026-10-11: wrappers.
2. Lockfile format: an explicit spec with SHA-256 digests, conda-lock or pixi? D48 assumes one exists. Settled 2026-10-11: an explicit lockfile of the platform's own, in JSON (CP-18). conda-lock needs a URL and hash for every pip entry, which an index install does not record, and an explicit spec names a host.
3. One permission request per resolved install (CP-8) needs escalation grants in the policy middleware. Is a per-install grant the right unit for the user? Implemented as proposed (2026-10-11), to confirm in use: one request per operation for its whole resolved set, its files listed under the request in the operator's `pending`, and the middleware honoring the approved set. An operation that waits keeps the environment's turn, so later operations there wait behind it (CP-11), until it is answered, times out or is cancelled.
4. Restoring the previous revision after a failed link: volume snapshot or rebuild from the lockfile? W3 chooses. Until then a failed install that changed the packages ends `needs_recovery` (2026-10-11).
5. Where do AGPL exceptions live: the policy's `packages` section, or the environment profile? Settled in implementation (2026-10-11), to confirm: the policy's `packages.agpl_exceptions`, a list of package names per ecosystem (`conda`, `pypi`). The policy already decides installs, and its revisions are the project's record of what it allows; an environment profile is shared by projects (D46).
6. May skill packs and project profiles extend the capability catalog, or only the platform? Only the platform for now (2026-10-11): the catalog is a reviewed file shipped with the worker. The question stays open for skill packs and profiles.
7. Should new executions wait during linking (proposed, CP-12), or run with a warning? Settled 2026-10-11: they wait.

## Sources

[Monitoring](../MONITORING.md) (Conda workflow), [package cache](../PACKAGE-CACHE.md), [policy](../POLICY.md) (packages), [projects and sessions](../PROJECTS-AND-SESSIONS.md), [architecture](../ARCHITECTURE.md) (storage and dependencies), [licensing](../LICENSING.md), [implementation plan](../IMPLEMENTATION-PLAN.md) (W2, W3), decisions D09, D16, D21, D22, D46, D48, D71. CScience behavior from the [Claude Science report](../../.agents/docs/agent-reports/claude-science.md) (environment management and provisioning).
