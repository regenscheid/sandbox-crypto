# Kubernetes offline operations, 2026-10-20

Continues the [retention increment](k8s-retention-2026-10-20.md) on `k8s`.
No image was published, cluster context accessed, real credential read, model
called or preserved service/research modified. Site configuration remains private.

## Implemented and independently reviewed

- Indexed bounded control-cache maintenance with age/byte/count policy, resumable
  private cursor and default dry-run. Archive/readback precedes optional eviction;
  corrupt, unconfirmed and unrelated bytes remain. An index benchmark with 100,000
  duplicate/unrelated rows used 113 versus 109 SQLite VM instructions for its
  bounded lookup, without a temporary sort or history scan. Migration 76 adds
  covering indexes; it does not rewrite scientific records.
- Complete offline workspace create/verify/restore with original image, placement,
  prefix and package provenance. Capture and restore require the original sandbox
  Stopped plus an operator-declared exclusive mount. Bounded framed streaming
  preserves packages, journals, initialization sentinel, modes and link topology.
  Restore publishes a new verified tree without rebinding or clearing holds.
- S3 checkpoint transport uses immutable 16 MiB chunks and a final pinned manifest,
  full readback, original-identity uncertain-write reconciliation and bounded
  streaming. SDK socket timeouts and cooperative deadline checks are finite;
  continuously trickling blocking SDK reads do not have hard elapsed preemption.
  No remote deletion, research PVC release or complete recovery-set claim.
- Trusted control/worker TLS and custom verified CA clients, independent liveness /
  admission readiness / runtime connectivity, and owned admission/drain. Independent
  review found and corrected a socket-preparation/shutdown race.
- Unpublished trusted app/worker recipes and a deterministic private-TOML deployment
  renderer. Singleton block-backed metadata Pods, strict image pins, resource
  bounds, private role credentials/SDK discovery, no service-account tokens and
  explicit NetworkPolicies. Offline rendering neither applies nor creates agents.

## Disposable S3 compatibility

Garage 2.4.1 used synthetic credentials, a temporary bucket and only loopback S3
exposure, from cached image
`dxflrs/garage@sha256:9c96caa2612d3411acc5b0e6701fb238dbfba33e533a6d7d3d811a4b12d0d020`.
A 134,217,728-byte research file produced a 134,218,753-byte six-member archive.
Upload, same-manifest repeat upload, fetch and decode passed in 1.990 seconds,
with 27,551,649 bytes peak traced Python allocation. File SHA-256:
`09adf361eeef3e3ed95a1f758db4924c8539ebe4eec9058d3e0e29322f317846`.
Archive SHA-256:
`aadee918ff895705675d53594330cc8235773772a1e7a6daaa9a20fce52854eb`.
Manifest SHA-256:
`7116a1771e020724455de72ee116fda7a37c0400497e4c2b17d53b1512fab03a`.

Modes, hardlink topology, symlink text and sentinel matched after reconstruction.
The owned server was removed and inspection confirmed absence. Synthetic server
state/keys were deleted. An initial pilot reached successful transfer/restoration
but teardown hit root-owned LMDB files; the owned cleanup container corrected
ownership on only that disposable directory. A fresh full repeat passed teardown.
This is transport compatibility evidence, not Garage durability/replication,
OpenShell stopped-sandbox, Kubernetes mount/fence or ARM64 acceptance.

## Validation and remaining gates

Final integrated source, local-image and evidence results are recorded below when
qualification finishes. No inference of deployment acceptance is made from unit
tests or Kubernetes schema validation.

Still open: coordinated metadata/workspace recovery sets, cluster writer fencing
before PVC release, recorded agent-runtime bootstrap/restart, real CNI/supervisor
isolation, native ARM64/crypto-base and Sage/MCP/PDF interruption/Resume acceptance.
The original host launcher cannot be placed unchanged in a Deployment to provide
those guarantees. Image publication remains explicitly deferred.
