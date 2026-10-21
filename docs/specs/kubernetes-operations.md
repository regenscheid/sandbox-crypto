# Kubernetes operations increment

Status: implementation contract, 2026-10-20. Continues [Kubernetes work](../KUBERNETES.md)
and D91. The owner authorizes implementation and disposable local qualification,
but defers all image publication. Private site values and credentials stay out of
Git. Nothing here authorizes deletion of a research PVC or mutation of a preserved
research instance.

## Offline workspace checkpoints

The first production caller is the trusted operator command
`sandbox-science-worker workspace-checkpoint create|verify|restore`. It operates on
an explicitly mounted complete research workspace, never an agent-selected host
path. It holds the worker's exclusive StateLock and binds its immutable receipt to
the retained project, environment, original sandbox ID, original image/placement
and required native-prefix identity. Create and restore check the exact original
OpenShell sandbox is Stopped before and after their filesystem work. They do not
stop, delete or recreate it themselves.

Stopping the worker is insufficient: its jobs and daemons can outlive it. The
operator must provide an exclusive offline mount and explicitly declare that
condition. A stopped-sandbox check cannot fence another administrative writer to
the volume. RWO also is not a cluster writer fence. Receipts record this declared
condition; they do not qualify automatic PVC release. Creation from a running,
unknown or mismatched sandbox is refused. No active/paused operation identity or
package recovery hold is removed by archival or restoration.

The complete tree includes installed native packages, research data, journals,
prefix identity and the `.openshell-initialized` sentinel. Preserve file and
directory modes, symlink text without following it, and existing hardlink topology.
Do not follow filesystem links out of the tree. Reject unsupported special files
explicitly instead of claiming they were captured. Detect changed source files and
fail the capture. Preserve completed immutable outputs after an interrupted attempt;
partial archives are not committed receipts.

Use a separate versioned checkpoint format and limits, not the scientific artifact
contract's 1 GiB individual-blob limit. Encoding/validation streams file contents
with bounded buffers; metadata, member counts and total bytes have explicit bounds.
Checksums cover full content and provenance. Verification requires the exact archive
and receipt, and detects truncation, extra entries, duplicate paths, changed bytes,
unsupported metadata and incompatible pinned identity. No executable metadata or
archive-provided extraction path is trusted.

Restore only into a new empty offline directory. Reject path traversal, duplicate
members, unsafe hardlink targets and writes through symlink parents. Symlink targets
may be absolute as preserved data, but are never followed during extraction. Verify
the complete archive before exposing a completed destination; a failed restore
remains explicitly incomplete and cannot be mistaken for a successful checkpoint.
Do not merge with existing files, rebind an environment, align package revisions,
clear operation holds or manufacture restored interpreter memory. Ownership is
normalized to the invoking trusted operator; modes/topology are retained. Cross-
architecture restoration is refused when the original launch pins architecture.

A workspace checkpoint is only part of a recovery set. Control/worker/policy/event
databases, bindings, plans, conversations, retained artifacts and backing settings
must be preserved separately. Automatic PVC deletion remains deferred until a
coordinated recovery set and cluster-backed writer fence are implemented and tested.

### Durable checkpoint transport

Separate bounded immutable chunks and a final manifest permit multi-GiB checkpoints
without enlarging artifact limits or keeping large caches indefinitely. Local
verification precedes upload. Use the standard S3 credential chain and generic
private destination configuration; never put keys into receipts, arguments or Git.
Verify chunk hash and size after conditional creation or an uncertain existing
write. Publish the final immutable manifest only after all chunks verify. Restore
verifies the final manifest and every chunk before accepting the reconstructed local
archive. Interrupted uploads leave no false completion; repeating the same identity
can reconcile original immutable chunks. Different bytes conflict. No command
deletes remote objects, a source workspace, PVC, worker metadata or unresolved hold.
If durable transport is not yet integrated, local checkpoint completion must be
reported distinctly from remote retention and recovery-set completeness.

## Cluster control callback

Keep local behavior unchanged. The trusted control server gains explicit TLS
certificate/key settings; a non-loopback bind requires TLS. The agent runtime can
connect to a strictly validated HTTPS `/control` endpoint with an optional custom
CA file. Certificate and hostname verification are always enabled. Existing local
HTTP configuration remains available for the existing local OpenShell path. No
agent tool can supply these settings or weaken TLS. Runtime bearer scope, protocol,
image revision, generation and one-connection admission checks remain in force.

Liveness is distinct from service readiness and runtime connectivity. The control
endpoint must be reachable before the runtime can connect, so Kubernetes Service
readiness cannot require an already-connected runtime. Health reports process and
drain state separately. Shutdown stops new admission, closes the active socket,
drains admitted effects and leaves interrupted work recoverable. It does not stop
research children or replay tool/model operations.

TLS, certificate rejection and socket shutdown receive real local HTTPS tests with
synthetic credentials. No certificate verification bypass or untrusted forwarding
header mode is added. Private projected Kubernetes Secret files are copied by a
narrow init step into private regular mode-600 files in memory-backed volumes;
existing private-credential checks remain strict.

Worker API listeners also accept an explicit TLS identity. Control and workspace
clients can add a custom worker CA to system trust, requiring HTTPS when supplied;
they never disable hostname verification. Supplying a custom CA with a borrowed
HTTP session is refused instead of pretending to change that session's connector.
Existing local HTTP configurations remain unchanged. Cluster rendering requires
encrypted worker/control links and keeps worker bearer scope independent of TLS.

## First deployment bundle

The initial topology uses a singleton application Pod: control and workspace share
one block-backed metadata PVC. A singleton worker pool Pod co-locates worker and
policy middleware over their policy/event/state files on another block-backed PVC.
These SQLite processes cannot be independently scheduled across RWX/NFS volumes.
Use Recreate strategy, one replica and the existing process/state locks. Application
and worker Pods have no Kubernetes service-account token or administrative RBAC.
Only the separately installed OpenShell gateway has its required cluster authority.

The orchestrator runtime, research workloads and MCP proxy remain separate OpenShell
sandboxes. Do not run an agent loop or research code inside a trusted service Pod.
Runtime creation requires recorded original ownership and trusted placement; never
use a failed readiness probe or process restart as permission for a replacement.

Begin with direct Pod port forwarding to the workspace's two loopback listeners.
This preserves current login, preview-origin and CSRF behavior. Public ingress,
public/preview origins, Secure cookies and proxy authorization are a later explicit
increment. Do not expose loopback-derived HTTP URLs as a supported public ingress.

Provide unpublished build recipes and deterministic rendered manifests/settings.
Pin operator-selected images by digest and verify that placeholders are resolved
before apply. Generic configuration selects capacity, architecture, storage class,
S3 endpoint and approved egress without recording site values in Git. Default-deny
NetworkPolicy plus narrowly declared service/DNS/gateway/model/S3 flows is required;
document CNI enforcement and separately scheduled OpenShell supervisor limitations.
Preflight and render commands never apply or read cluster credentials implicitly.
Actual deployment, both native architectures, runtime isolation and loss/Resume
acceptance remain separate evidence gates.

## Bounded artifact cache maintenance

An operator policy may schedule bounded passes over database-referenced control
blobs. Each pass has explicit reference-count and byte bounds and a resumable hash
cursor. It uses existing full local/remote verification before optional eviction;
dry-run is default. Policy may archive before eviction, retain a minimum local age
and limit work per pass. Do not reinterpret logical research retention quotas as
physical cache limits. Skip absent local content without downloading it only to
evict it. Never remove metadata, legacy inline content, unknown transfers, worker
replicas, remote objects or PVCs. Every skipped/failing entry is visible; advancing a
cursor never declares a failed object retained. Scheduled execution remains opt-in.
