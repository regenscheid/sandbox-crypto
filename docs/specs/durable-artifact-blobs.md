# Durable large artifact content

Owner-approved 2026-10-17; implemented locally. Artifacts belong
to the durable control application, independently of agent containers. New retained
content uses a local content-addressed filesystem store adjacent to the control
database; the same directory can live on a PVC. S3 is a future backend, not implemented.
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
