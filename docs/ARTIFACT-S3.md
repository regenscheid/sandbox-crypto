# Optional S3 artifact backing

Install the SDK from the workspace root with `uv sync --extra s3`. An installed
orchestrator package uses `sandbox-science-orchestrator[s3]`. Every trusted process
that reads artifacts needs that extra and the same adjacent backing configuration.
Without configuration, existing local artifact behavior is unchanged.

Configure an existing control database once:

```bash
uv run --extra s3 sandbox-science-artifacts configure \
  --database /srv/sandbox-science/control.sqlite3 \
  --bucket research-artifacts --prefix instance-a \
  --endpoint https://objects.example.invalid --addressing-style path
```

Omit `--endpoint` for AWS S3, and use `--region` as appropriate (`us-east-1` by
default). The bucket must already exist and support conditional `PutObject` with
`If-None-Match: *`, full/ranged `GetObject`, and `HeadObject`. The SDK uses Signature
Version 4, finite connection/read timeouts and one HTTP attempt. Optional SDK
checksum extensions are disabled; the application verifies exact length and
SHA-256 independently after upload. See the official [SDK configuration](https://docs.aws.amazon.com/boto3/latest/guide/configuration.html)
and [conditional PUT](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html).

The default socket inactivity timeout is 15 seconds, with a 300-second elapsed
budget checked between full-read chunks and after prefix reads. These checks do
not preempt an SDK call while it is still receiving data; they are not a strict
wall-clock deadline against a trickling peer. Backing endpoints are trusted
operator configuration. Cancellation retains admitted I/O and transfer ownership
until that I/O drains.

The owner-only `control.sqlite3.artifacts/storage.toml` stores the destination.
Credentials use the SDK's standard chain, including workload roles, environment or
the operator's AWS configuration. Never add keys or tokens to this TOML, artifact
metadata, agent configuration or command arguments. New publications require a
verified remote copy before they succeed; existing local content stays readable.
Long-lived readers observe newly installed backing without reconstructing stores.
Repeating the same configuration is safe; a different destination is refused and
requires an explicit migration preserving archived content.

Archive the database's referenced external blobs, inspect eviction, then apply it:

```bash
uv run --extra s3 sandbox-science-artifacts archive --database /srv/sandbox-science/control.sqlite3
uv run --extra s3 sandbox-science-artifacts evict --database /srv/sandbox-science/control.sqlite3
uv run --extra s3 sandbox-science-artifacts evict --database /srv/sandbox-science/control.sqlite3 --apply
```

Each command reports JSON lines with the SHA-256, size and outcome. Eviction is a
dry-run unless `--apply` is present. It verifies both local and remote content first;
a failed copy or verification preserves the local file. It never deletes remote
objects, staging, orphans, reservations, worker copies, upload records, legacy
inline SQLite content or environment PVCs. Logical retention quota usage is unchanged.

New user-upload originals also use this store and its verified S3 backing. Their
immutable metadata and references live in schema 74; existing inline originals are
read without rewriting their rows. Archive/evict includes the new upload references,
so removing a verified local cache copy preserves the original in object storage.
Upload IDs, names, media types, USER attribution, hashes, sequence numbers and
the existing 2 GiB/project and 8 GiB/overall quotas span both representations.

Upload ingestion admits at most four transfers overall and two per project, each
up to 100 MiB. An unknown-length body reserves that maximum before reading.
Received bytes stream to private scratch, then copy into the hash store after the
digest is known; receive scratch closes before remote I/O. The transient copy can
use twice the admitted bytes (800 MiB overall). Configure `TMPDIR` accordingly.
These are upload-ingestion bounds; cold-download scratch has separate consumer
admission. Small document and existing worker-delivery consumers may still use a
bounded convenience read, up to the existing 100 MiB worker protocol limit.

An interrupted installed upload keeps its exact digest and reservation. Retrying
the same ID and bytes reuses that reservation after the old owner drains, then
reconciles its immutable receipt. Changed bytes or metadata conflict. Storage
failures report a retryable unavailable result; quota and identity errors retain
their own kinds. No cleanup drops an installed or prior unknown hold implicitly.
Process-safe ownership uses 4,096 stable advisory lock stripes under
`upload-locks/`; collisions with another live owner are retryable. Keep lock files
in place, on the same local/block-backed filesystem as the control database;
NFS-style shared filesystems are outside this ownership guarantee.

After eviction, full reads use a private temporary file, verify the remote bytes,
rewind it and remove it when the consumer closes. This supports existing pinned
downloads and byte ranges.
Temporary files honor `TMPDIR`; cluster manifests must provide bounded ephemeral
scratch or an explicitly selected volume for cold downloads. These files are
removed on close; they do not populate a persistent cache. Provenance prefixes
use bounded range requests with exact range/size checks, as
local prefixes rely on verification at ingestion. Back up the metadata database,
configuration, still-local blobs and object store together. Do not expire referenced
objects through a bucket lifecycle rule.

The deterministic suite uses a fake SDK plus botocore's request model. An optional
endpoint qualification uses a dedicated existing bucket and standard credentials:

```bash
SS_INTEGRATION=1 SS_ARTIFACT_S3_TEST_BUCKET=dedicated-test-bucket \
  SS_ARTIFACT_S3_TEST_ENDPOINT=https://objects.example.invalid \
  uv run --extra s3 pytest -q packages/orchestrator/tests/test_artifact_s3.py::test_opt_in_compatible_endpoint
```

Set `SS_ARTIFACT_S3_TEST_REGION` if needed. This creates one small object beneath a
fresh `qualification/` prefix, exercises verified upload, local eviction and cold
retrieval, and intentionally leaves the remote object for operator cleanup. It
does not establish Kubernetes deployment or environment restoration support.
