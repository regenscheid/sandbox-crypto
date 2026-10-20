# Kubernetes deployment work

The `k8s` branch starts Kubernetes support from `dev`. No cluster deployment has
been qualified. Examples describe mixed Linux AMD64/ARM64 nodes with different
CPU, memory and storage capacity; endpoints, credentials, node names, storage
classes, ingress and bucket names belong in private configuration.

## Placement

Keep the existing worker API and OpenShell lifecycle. OpenShell owns sandbox pods;
the platform must not add a competing Job controller or replay an interrupted
research operation merely because a pod restarted. Pin CLI, gateway, drivers and
runtime images to the same OpenShell release (currently v0.1.3).

Use resource requests and trusted pool configuration to select capacity. Each
Kubernetes profile must pin `kubernetes.io/arch` to `amd64` or `arm64`: OpenShell
v0.1.3 retains the multiarchitecture image reference, without recording the selected
platform. The pin keeps retained native packages compatible across pod replacement.
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
The gateway must permit approved Kubernetes driver configuration. These settings
target OpenShell v0.1.3's workload pod; they do not place its separately managed
supervisor pod. A runtime class name is configuration, not isolation proof.
The current kernel-identity probe also cannot distinguish an isolated guest from
an ordinary container on a different cluster node. Acceptance must check the
admitted runtime and actual ingress/egress NetworkPolicy enforcement independently.

The first deployment should use one replica of the trusted control/store services
with a block-backed PVC, distinct trusted workers and separate OpenShell sandbox
pools. The current host launcher supervises local processes and assumes loopback
addresses; placing it unchanged in a Deployment does not establish cluster support.
Service addresses, authenticated runtime callbacks, probe endpoints, signal/drain
behavior and owned sandbox replacement need explicit cluster wiring first.

Shared image tags contain both Linux architectures. Research image recipes now
include their target-native scheduler shim; the worker verifies source identity,
ELF architecture and loadability before use. A checked host-compiler fallback is
available only on a matching Linux architecture. These image changes still need
publication and native ARM64 qualification. Kubernetes pod replacement preserves
mounted data, not interpreter memory or changes to the image's writable layer.
The research image layout below keeps installed packages in that mounted data;
older images need migration or a verified rebuild before claiming the same revision.

### Persistent research prefix increment

The shared `minimal` and `crypto-base` image layout places real package bytes
under `/sandbox/.sandbox-science-prefix`, retaining `/opt/sandbox-science` as a
compatibility symlink. The same image is used locally, on Mac and in Kubernetes;
trusted service images keep their separate layout. Build cleanup must preserve
the real prefix. OpenShell v0.1.3's init container copies the image's `/sandbox`
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
creation default, not a live resize of existing PVCs. The [v0.1.3 driver config](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-driver-kubernetes/src/config.rs#L255)
and [chart mapping](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/deploy/helm/openshell/templates/_helpers.tpl#L543)
are the exact capacity/class seams; no agent volume or claim authority is added.

The seed init container mounts the claim at `/mnt/openshell-workspace`, leaving
image `/sandbox` visible as the source. The [pinned seed algorithm](https://github.com/NVIDIA/OpenShell/blob/e1f3c82caa3ed3b65de22889ae7ef32a774878ef/crates/openshell-sandbox/src/main.rs#L1669)
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
online cleanup and whole-workspace/PVC archive/release remain separate work.
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
4. Trusted service images and manifests/chart: Service DNS, mTLS/Secrets,
   readiness/draining, one-writer PVC placement, RBAC and NetworkPolicy. Keep
   release values separate from private site values. Do not grant agents cluster
   or storage administration rights.
5. Disposable acceptance in both architectures: Sage/PDF/MCP, large artifacts,
   worker/control/runtime loss, pod eviction, original-operation Resume, cold
   S3 retrieval and verified cleanup. Record actual isolation tier per pool.

OpenShell's [pinned runtime documentation](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/runtimes/)
and [pinned chart](https://github.com/NVIDIA/OpenShell/tree/v0.1.3/deploy/helm/openshell)
are the runtime integration sources. Kubernetes documents
[persistent volume lifecycle](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
and [resource requests](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/).
