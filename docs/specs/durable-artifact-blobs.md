# Durable large artifact content

Owner-approved 2026-10-17; implemented locally. Artifacts belong
to the durable control application, independently of agent containers. New retained
content uses a local content-addressed filesystem store adjacent to the control
database; the same directory can live on a PVC. Optional S3-compatible backing and
explicit verified local eviction are implemented on `k8s` (see below).
Existing SQLite content remains readable without copying or rewriting old revisions.

Limits are 1 GiB per artifact, 2 GiB retained per recording call, 16 GiB per project
and 64 GiB overall. Concurrent staging reserves bytes durably before transfer, with
at most four transfers and 4 GiB staged overall, two transfers/2 GiB per project.
Reservations count against retention headroom. Quota failures retain a visible
metadata/held outcome rather than deleting existing content.

Publication retains its existing durable intent and pinned worker snapshot. HTTP
content streams into a private temporary file in bounded chunks, verifying scope,
length, encoding and SHA-256. A verified blob is fsynced and installed by hash before
one SQLite transaction commits its reference, revision, head, event, quota and receipt.
Retries ingest the pinned snapshot, never republish an uncertain mutable path.
Cancellation does not release transfer admission until actual owned I/O has drained.

A crash before metadata commit can leave unreferenced blobs, temporary files and
held staging reservations. There is no automatic garbage collection. Recovery or
operator cleanup must prove that a transfer is inactive and content unreferenced;
neither ordinary container teardown nor quota exhaustion removes retained artifacts.
Backups include the database and its adjacent blob directory.

`ArtifactInventory.open_content(project, pinned_reference)` yields a verified binary
stream positioned at zero, owned until its context exits. `content()` remains a
bounded convenience read, defaulting to 8 MiB. Downloads stream outside model/RPC
payloads. Existing PDF, figure, image, source-copy and model-context bounds remain;
the run-evidence export still explicitly excludes external files. Transfer and capture
deadlines allow bulk I/O while retaining finite bounds. Live 1 GiB qualification is
separate from deterministic validation.

The authenticated workspace route is
`GET /api/v0/projects/{project}/artifacts/{artifact}/revisions/{revision}/content?sha256=HEX`.
It streams 512 KiB blocks, supports one byte range and hash-bound If-Range, and
limits concurrent downloads and total time. No worker or agent container is needed.
Logical quotas charge each retained revision, including legacy SQLite content; a
reproduction adds no retained revision. Physical storage can additionally include
legacy mirror bytes and bounded charged orphan/staging transfers. An installed
blob whose metadata commit fails retains its reservation for explicit recovery.
Worker capture allows 240 seconds and HTTP publication/content transfer 300 seconds.

## Kubernetes object-store increment (2026-10-20)

Implemented on `k8s`. An optional private
`<database-name>.artifacts/storage.toml` selects S3 backing for the existing
hash-named store. It holds endpoint, bucket, prefix, region and addressing style;
credentials use the SDK's standard credential chain, never artifact metadata or
agent-visible config. No config keeps the existing local-only behavior. All
constructors, including provenance prefix reads, use this same configuration.

New content is installed locally first, then uploaded and independently read back
to verify exact SHA-256 and length before publication succeeds. Mark installation
before remote I/O: a failed/unknown PUT retains its local file and transfer
reservation, so uncertainty cannot silently release capacity. Content keys derive
from the exact hash. Repeating that transfer reconciles identical immutable bytes;
ETags and user-supplied object metadata are not integrity evidence.

Archived full reads stream into a private temporary file, verify size/hash, rewind
and yield a seekable stream; exit/failure removes that temporary file. Prefix reads
remain bounded, with finite socket inactivity timeouts and elapsed checks between
chunks. Blocking SDK calls are not strictly preempted at a wall-clock deadline;
the configured object-store endpoint is trusted. Remote outage or corruption
reports unavailable content, never empty or substituted bytes. S3-compatible
endpoints may need path addressing and no optional checksum-extension headers.

An operator CLI installs backing configuration and archives only database-referenced
external blobs. Dry-run is the default for local eviction; an explicit eviction
option requires successful remote verification first. It never deletes remote
objects, old inline SQLite content, staging, orphan content, worker replicas or
live environment PVCs. The metadata database and backing configuration remain
durable. Prefix/bucket changes after eviction require an explicit migration;
reconfiguration cannot silently point existing references at an empty bucket.

This first increment has no automatic age-based deletion. Later retention work
must bound local cache/PVC usage while preserving active/paused research,
incomplete transfers and original-operation recovery. See
[Kubernetes delivery work](../KUBERNETES.md).

Install the optional SDK and configure/archive/evict content with the
[S3 operator commands](../ARTIFACT-S3.md). Cold-read scratch uses Python's
temporary directory (`TMPDIR`); cluster manifests must put it on bounded ephemeral
storage or an explicitly selected volume. Successful reads remove that scratch
on close; a killed process can leave filesystem-level cleanup to its owning pod.

## Uploaded originals (2026-10-20)

New immutable user uploads use the same verified control-owned blob store and
optional S3 backing. Existing inline uploads remain unchanged and readable; an
append-only schema migration adds external upload metadata/references and durable
transfer reservations without rewriting the legacy BLOB table. Session/project
ownership, upload IDs, names, media types, hashes, sequence numbers and quotas
remain shared across both representations. A retry of an ID with different
metadata or bytes is a conflict, including an interrupted attempt whose bytes
have already been installed but whose metadata commit is incomplete.

Upload transfer admission allows four overall and two per project, each reserving
at most 100 MiB before reading its body. These reservations count against the
existing 2 GiB/project and 8 GiB/overall original-upload retention quotas, with
1,024 and 4,096 retained-upload count limits. Replays of committed IDs do not
charge retained bytes/count twice, but still require transfer admission. Unknown
or installed-but-uncommitted transfers retain their charge; retries must reconcile
the original identity and exact bytes rather than clear a hold or replay mutable
research operations. A live transfer owner cannot be replaced by another request.

Binary HTTP bodies are read in bounded chunks into admitted private scratch, then
copied into the hash-named blob store after the digest is known. Scratch can peak
at twice admitted bytes during that copy (800 MiB overall); close the initial
scratch before remote upload/readback. Only a verified blob followed by a committed
immutable reference completes an upload. Cancellation retains admission until the
actual I/O drains. Quota checks/metadata changes are short transactions, never held
across object-store or body I/O. An exact-ID retry reconciles a retained outcome.

Full downloads use a verified seekable stream and fixed-size network writes,
outside SQL transactions. Legacy inline originals are copied/verified into private
scratch before yielding. Small JSON uploads and bounded document/worker-delivery
consumers may retain the existing convenience byte API. Archive/evict enumeration
includes external upload references, never rewrites or deletes legacy rows, and
never deletes the originals from object storage.
