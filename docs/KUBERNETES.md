# Kubernetes deployment work

The `k8s` branch starts Kubernetes support from `dev`. No cluster deployment has
been qualified. Examples describe mixed Linux AMD64/ARM64 nodes with different
CPU, memory and storage capacity; endpoints, credentials, node names, storage
classes, ingress and bucket names belong in private configuration.

## Placement

Keep the existing worker API and OpenShell lifecycle. OpenShell owns sandbox pods;
the platform must not add a competing Job controller or replay an interrupted
research operation merely because a pod restarted. Pin CLI, gateway, drivers and
runtime images to the same OpenShell release (currently v0.1.5).

Use resource requests and trusted pool configuration to select capacity. Each
Kubernetes profile must pin `kubernetes.io/arch` to `amd64` or `arm64`: OpenShell
v0.1.3 retained the multiarchitecture image reference without recording the selected
platform. A search of v0.1.5's Kubernetes driver and server found no platform
resolution or recording, but this is unverified on 0.1.5 (source search only, no
cluster test). The pin keeps retained native packages compatible across pod replacement.
Separate approved profiles can share the same multiarchitecture image reference.
Automatic placement across architectures needs platform discovery and durable
pinning before it can safely support recovery. An ARM64
node is not necessarily small, and an AMD64 node is not necessarily large. Expose
CPU, memory, ephemeral storage, runtime class and node selectors per profile.
Agents choose an approved profile; they cannot supply Kubernetes JSON, PVC names,
host mounts or scheduling authority. Sandbox creation/recovery must retain the
original selected settings.

The worker accepts `[openshell.environments.kubernetes.PROFILE]` in private
configuration. Kubernetes pools must configure every offered profile; a nonempty
map with missing profiles is refused. The [example fragment](../examples/kubernetes/worker-placement.toml)
sets a node selector and runtime class plus explicit CPU millicores, memory MiB
and ephemeral-storage MiB requests/limits. CPU and memory limits narrow to the
effective project/pool policy; requests that no longer fit are refused before
allocation. Arbitrary pod JSON and volumes are not exposed. The selected JSON is
stored atomically with initial sandbox ownership, passed via
`--driver-config-json`, and kept across worker restart and ordinary sandbox
start/Resume. Changes to deployment settings apply to new allocations. Profiles
with pinned limits that exceed a tightened policy or pool ceiling are refused
on use or policy revision; a network-policy update cannot resize an existing pod.
Create a new allocation with smaller limits after preserving its research state.
An entirely absent table retains gateway defaults for existing local runs; that
configuration is outside the Kubernetes placement/recovery guarantee.
The gateway must permit approved Kubernetes driver configuration
([gateway settings](#gateway-settings-for-profile-placement)). The emitted JSON
uses only keys OpenShell v0.1.5 accepts (checked against its driver source and
pinned by a test); it targets the workload pod and does not place the separately
managed supervisor pod. A runtime class name is configuration, not isolation proof.
The current kernel-identity probe also cannot distinguish an isolated guest from
an ordinary container on a different cluster node. Acceptance must check the
admitted runtime and actual ingress/egress NetworkPolicy enforcement independently.

The [offline deployment bundle](KUBERNETES-BUNDLE.md) renders singleton application
and worker-pool Pods with separate block-backed PVCs. Trusted control and worker
links use verified TLS with optional private CAs. Control liveness, admission
readiness and runtime connectivity are separate; shutdown fences admission and
drains owned work. The local host launcher is not placed unchanged in a Deployment.
OpenShell runtime ownership/bootstrap and cluster restart qualification remain
separate work; rendering does not create or replace an agent runtime.

Shared image tags contain both Linux architectures. Research image recipes now
include their target-native scheduler shim; the worker verifies source identity,
ELF architecture and loadability before use. A checked host-compiler fallback is
available only on a matching Linux architecture. These image changes still need
publication and native ARM64 qualification. Kubernetes pod replacement preserves
mounted data, not interpreter memory or changes to the image's writable layer.
The research image layout below keeps installed packages in that mounted data;
older images need migration or a verified rebuild before claiming the same revision.

### Sandbox isolation

Decided 2026-10-22 (D43, D92). OpenShell 0.1.x installs a mandatory Landlock
baseline (ABI 3) in every Docker, Podman, Kubernetes and VM sandbox and probes
seccomp user notification before admitting a workload; startup fails closed
without them ([OpenShell kernel requirements](https://docs.nvidia.com/openshell/about/support-matrix)).
gVisor implements neither: its syscall table has no Landlock calls, and runsc
`release-20261012.0` was measured returning ENOSYS for Landlock and EINVAL for
the seccomp listener with OpenShell 0.1.5 ([NVIDIA/OpenShell#3361](https://github.com/NVIDIA/OpenShell/issues/3361),
[#3362](https://github.com/NVIDIA/OpenShell/issues/3362)). NVIDIA's reduced-fidelity
gVisor adapter ([#3367](https://github.com/NVIDIA/OpenShell/pull/3367)) closed
unmerged; a gVisor isolation backend is proposed, not shipped. Older OpenShell
releases ran under gVisor only by skipping best-effort Landlock, which 0.1.x no
longer allows. gVisor is therefore not a tier-2 option here.

- **Tier 3, Kata (all sandboxes for now).** The cluster runs Talos 1.13 with
  the `kata-containers` system extension (3.32.0), which on Talos 1.13 provides
  only the Cloud Hypervisor handler `kata`; the `kata-qemu` handler arrives with
  Talos 1.14. Every research and agent-runtime sandbox runs under `kata` on the
  amd64 nodes, at tier 3. The RuntimeClass ([example](../examples/kubernetes/runtimeclass-kata.yaml))
  carries `openshell.ai/sandbox-attachable=true`, a `scheduling.nodeSelector`
  limited to Kata-capable amd64 nodes, and `overhead.podFixed`. In v0.1.5 a
  RuntimeClass or PriorityClass needs only that fixed label; the second label,
  `openshell.ai/sandbox-attachable-workspace=<workspace>`, is checked for PVCs
  (`resource_admission.rs`, `admit_shared`). External evidence reports the stock
  OpenShell 0.1.5 runtime qualifying (Landlock ABI 7) inside Kata 4.1.0 with a
  6.18 guest kernel, given `net.ipv4.ip_unprivileged_port_start=0` and an
  explicit capability set for a root-started container ([#3362](https://github.com/NVIDIA/OpenShell/issues/3362)).
  This project has not qualified it, nor Talos's Kata 3.32.0.
- **Raspberry Pi nodes.** Cloud Hypervisor needs a GICv3 interrupt controller;
  Raspberry Pi CM5 and CM4 have GICv2, so they cannot run `kata` now. For now
  arm64 nodes host trusted service pods only, never sandboxes. After a Talos 1.14
  upgrade, `kata-qemu` on CM5 would allow arm64 sandboxes (unqualified; needs KVM
  on the Pi kernel and the arm64 images); the arm64 profile pins stay as they are.
- **Tier 1, plain runc.** D43 still accepts it for nodes without KVM, running in
  OpenShell's combined topology without a sandboxed RuntimeClass (Landlock,
  seccomp and network controls apply; the workload shares the node kernel). It is
  unused for now.
- **Pools.** With every sandbox on Kata, one worker pool declaring tier 3
  suffices, and every profile's `runtime_class_name` is `kata` with an amd64
  node selector. Mixing tiers (separate pools, each declaring its own tier and
  matching its profiles' runtime class and node selector) is deferred; if added,
  work whose policy requires a higher tier is refused, never moved to a lower-tier
  pool. The offline renderer renders one pool Pod, which fits this plan.
- **Verification.** The boot-ID probe cannot lower the tier of a sandbox on another
  node; set the worker's `node_kernel_releases = ["*-talos"]` so a sandbox
  reporting a Talos node kernel (plain runc) is held to tier 1, while a Kata guest
  kernel (not `-talos`) keeps the declared tier. RuntimeClass admission labels keep sandboxes on the intended runtime;
  confirming the admitted runtime from trusted state or inside the guest remains
  open (backlog).
- **Version.** This plan assumes OpenShell 0.1.5, as `dev` runs on babbage.
- **Unverified on the owner's cluster:** KVM and nested virtualization on each
  amd64 node (VM nodes need nested virtualization), Kata under Talos 1.13, and
  Landlock enabled in the Talos kernel.

### Gateway settings for profile placement

Caller-supplied driver configuration has been off by default since OpenShell
0.1.0. Profile placement needs it enabled in the gateway's Kubernetes driver
section, with resource admission on (example: [gateway-driver.toml](../examples/kubernetes/gateway-driver.toml)):

```toml
[openshell.drivers.kubernetes]
allow_driver_config = true
default_runtime_class_name = "kata"

[openshell.drivers.kubernetes.resource_admission]
enabled = true
```

With the Helm chart these are `server.drivers.kubernetes.allowDriverConfig`,
`server.defaultRuntimeClassName` and `server.drivers.kubernetes.resourceAdmission`
(v0.1.5 chart; `gateway.allowDriverConfig` is not a v0.1.5 key). Label every
RuntimeClass and PriorityClass a profile names with
`openshell.ai/sandbox-attachable: "true"`, and every PVC also with
`openshell.ai/sandbox-attachable-workspace: "<OpenShell workspace name>"`; an
unlabelled resource is refused at sandbox creation. The worker sends the config
wrapped as `{"kubernetes": {...}}`; OpenShell v0.1.5 rejects unknown keys at every
level (`deny_unknown_fields`), which `test_kubernetes.py` checks for the emitted JSON.

### Persistent research prefix increment

The shared `minimal` and `crypto-base` image layout places real package bytes
under `/sandbox/.sandbox-science-prefix`, retaining `/opt/sandbox-science` as a
compatibility symlink. The same image is used locally, on Mac and in Kubernetes;
trusted service images keep their separate layout. Build cleanup must preserve
the real prefix. OpenShell's (v0.1.3; its mount path is unchanged in v0.1.5) init container copies the image's `/sandbox`
tree into its workspace PVC before the agent mounts it at `/sandbox`.

Trusted Kubernetes profiles require this layout through `require_workspace_prefix`
(default true). An explicit false retains old/custom image behavior outside the
persistent-prefix recovery guarantee. The selected requirement is stored with
initial sandbox ownership and is retained across start/reconnect, independently
of later deployment settings. No raw volume or claim authority is added to agents.

Executor hello reports a bounded prefix protocol, canonical path and nonsecret
identity stored within the real package prefix. Marker creation is required-mode
only, follows fixed-layout validation, and never touches a developer/local Python
prefix. Before accepting a new executor
as the same environment, the worker checks the original required layout, retained
prefix identity and package fingerprint against its known revision. Missing or
changed required identity/layout/packages become a durable recovery hold,
preserving the last known revision and interrupted work; they cannot silently
become a fresh unmanaged package revision or trigger automatic reinstallation.
Ordinary surviving-daemon reconnect and journal reconciliation retain their
existing semantics. This is continuity detection, not a snapshot of live kernels
or independent integrity verification of every installed file.

Restoring the original prefix and known package fingerprint can release that
specific hold. A durable pending acknowledgement survives a worker restart; the
executor revalidates the restored state before aligning its cached baseline, and
the worker records the acknowledged package epoch before admitting execution or
new package changes. Unfinished package journals and unrelated recovery holds
remain intact. Neither restoring a prefix nor its identity marker restores live
interpreter memory.

An active workspace PVC must fit its full native base prefix, installed packages
and research data. Upstream's 2 GiB workspace default is too small for `crypto-base`
(the earlier package measurement was about 5.2 GB before headroom). Storage size,
class and retention are private operator choices. Initial copy/fsync latency and
both native architectures need live qualification. Upstream interrupted seeding
does not currently resume a partial copy: preserve that PVC and report the exact
initialization failure, never delete/reseed it to manufacture successful recovery.
Complete archive/restore remains required before releasing a research PVC.

Configure default workspace claims in trusted gateway configuration, independently
of worker pod resources:

```toml
[openshell.drivers.kubernetes]
workspace_default_storage_size = "10Gi"  # illustrative; budget prefix + data + headroom
# workspace_storage_class = "example-storage"  # omit to use the cluster default
```

Pinned Helm values `server.workspaceDefaultStorageSize` and
`server.workspaceStorageClass` render those same runtime fields. This is a gateway
creation default, not a live resize of existing PVCs. The [v0.1.3 driver config (fields still present in v0.1.5)](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-driver-kubernetes/src/config.rs#L255)
and [chart mapping](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/deploy/helm/openshell/templates/_helpers.tpl#L543)
are the exact capacity/class seams; no agent volume or claim authority is added.

The seed init container mounts the claim at `/mnt/openshell-workspace`, leaving
image `/sandbox` visible as the source. The [pinned seed algorithm (v0.1.3 source; not re-read for v0.1.5)](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-sandbox/src/main.rs#L1669)
copies file bytes, preserves symlinks without following them, normalizes ownership
permissions and fsyncs, then writes its initialized sentinel. It does not resume
partial copies. The agent later mounts that claim over `/sandbox`; the real prefix
there survives pod replacement, while the image's `/opt` compatibility symlink
still resolves into it.

## Storage lifecycle

| Data | Initial placement | Retention |
| --- | --- | --- |
| Control metadata, plans, documents, ownership, recovery records | Small block-backed PVC with a single SQLite writer | Durable, backed up together with its blob configuration; no automatic expiry |
| Retained immutable artifact bytes and new uploaded originals | S3-compatible object store, hash-named and verified | Durable independently of agent and worker pods; remote deletion is a separate policy |
| Artifact local cache | Bounded local/PVC space | Explicit eviction only after a verified remote copy; normal reads can fetch archived content |
| Active research workspace and checkpoints | Per-environment PVC | Keep while running, paused or recoverable; checkpoint/export and verify before release |
| Worker capture blobs | Worker CAS with immutable snapshot metadata | Exact durable control ACK, then explicit offline release under the worker state lease; shared/installer references pin bytes |
| Legacy inline uploaded originals | Existing control SQLite BLOBs | Preserve unchanged; moving old rows is a separate migration |
| Build/download scratch | Bounded ephemeral storage where reconstructible | Remove when the owning operation is settled; do not treat it as a recovery checkpoint |

PVC capacity is a deployment cost choice, not scientific result retention. Moving
an artifact to object storage does not delete its record or reduce logical artifact
quota usage. Conversely, deleting a PVC must never be the mechanism for deciding
whether research is finished. Idle or paused environments remain recoverable.

The first storage increment implements optional [S3 backing](ARTIFACT-S3.md) for
the existing artifact store, new uploaded originals and explicit archive/evict
commands. It does not delete sandbox PVCs, worker copies, old inline SQLite content,
orphan transfers or unresolved reservations.
Object-store credentials stay with trusted storage services; agents receive scoped
artifact references and content, not bucket credentials. Endpoint and addressing
settings support S3-compatible services now and the AWS credential chain later.

New fully retained publications also commit a bounded, endpoint-bound acknowledgement
outbox. Delivery failure preserves the successful receipt and original snapshot;
run-start, completed-operation replay and a manual command retry the same identity.
The [offline worker release command](WORKER.md#offline-capture-replica-retention)
requires every shared-hash reference to be acknowledged and holds the worker's
exclusive state lease. A disposable local process test verifies lost-reply recovery,
dry-run/apply, restart and unchanged control content without recapture. Automatic
online worker cleanup remains separate work. The bounded control cache maintenance
command scans referenced content by indexed hash pages, defaults to dry-run and
can archive before verified eviction. Its private resumable cursor never grants
retention authority; unknown or corrupt copies remain local. See
[artifact maintenance](ARTIFACT-S3.md).
Complete offline workspace checkpoints now capture native packages, journals,
research files, initialization sentinel, modes and link topology with bounded
streaming. Trusted create/restore requires the original sandbox to be Stopped and
an operator-declared exclusive offline mount. Receipts bind the original image,
placement, prefix identity and package fingerprint. Restore publishes a verified
new directory without rebinding ownership or clearing recovery holds. The separate
S3 transport uploads 16 MiB immutable chunks and commits a fully verified manifest;
fetch requires its exact digest. See [workspace checkpoint operations](WORKER.md#offline-complete-workspace-checkpoints).

A disposable Garage test verified a 128 MiB workspace archive, repeat upload of the
same immutable identity, fetch, content hashes, modes, symlinks and hardlinks, then
verified teardown. This is transport compatibility evidence, not a cluster writer
fence or complete recovery set. Automatic PVC release still requires coordinated
control/worker/policy/event/binding backups and ownership restoration; an artifact
or workspace archive alone cannot authorize it.

The owner has deferred new image publication; no image changes were published.

For hosted clusters, prefer small metadata PVCs and short-lived reconstructible
scratch. Set cache and inactive-workspace retention policy explicitly before
production: size, age, minimum checkpoint coverage and a hold for active/paused
work. Object-store lifecycle rules must not expire referenced artifacts; bucket
versioning, encryption, backup and storage-class choices remain operator settings.
Agent Sandbox can own and garbage-collect workspace PVCs when its Sandbox is
deleted. Do not use sandbox deletion as idle cleanup: verified complete archival
and explicit lifecycle ownership must precede that action, regardless of the
StorageClass reclaim policy.

## Delivery sequence

The [offline deployment bundle](KUBERNETES-BUNDLE.md) now prepares trusted singleton
services, private credential projections, resource/PVC placement and scoped network
rules from private TOML. It never applies, publishes an image or creates a runtime;
operator bootstrap and live qualification remain explicit.

1. Trusted profile-level Kubernetes placement/resource configuration and native
   helper compatibility; deterministic tests before live workloads.
2. S3-backed immutable artifact content, bounded archived reads, verified upload
   and opt-in local eviction, with compatibility tests against an S3 endpoint.
3. Package-prefix persistence/rebuild and durable capture acknowledgement, then
   a policy-driven cache/workspace retention controller with dry-run reporting.
   Worker retention acknowledgement must be separate from capture-helper cleanup:
   commit an exact-snapshot ACK intent with the successful control receipt, retry
   through a bounded outbox, and preserve successful publication when delivery
   fails. Physical replica release starts offline under the worker's exclusive
   state lease and requires every reference sharing a hash to be acknowledged.
   Installer-log references retain their bytes. Online deletion additionally needs
   writer fences and pinned download descriptors; it is not a plain file unlink.
4. Unpublished trusted service image recipes and the offline deployment renderer:
   Service DNS, verified TLS/bearer scopes, private Secrets, readiness/draining,
   one-writer PVC placement, no service-account authority and NetworkPolicy. Keep
   release values separate from private site values. Do not grant agents cluster
   or storage administration rights.
5. Disposable acceptance in both architectures: Sage/PDF/MCP, large artifacts,
   worker/control/runtime loss, pod eviction, original-operation Resume, cold
   S3 retrieval and verified cleanup. Record actual isolation tier per pool.

OpenShell's [pinned runtime documentation](https://docs.nvidia.com/openshell/v0.1.5/how-it-works/sandboxes/runtimes/)
and [pinned chart](https://github.com/NVIDIA/OpenShell/tree/v0.1.5/deploy/helm/openshell)
are the runtime integration sources. Kubernetes documents
[persistent volume lifecycle](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
and [resource requests](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/).
