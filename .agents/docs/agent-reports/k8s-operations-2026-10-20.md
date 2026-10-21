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

Frozen source `912633b58d2ba1e031b48f24bf64d0afdf821902` passed **14,985
Python tests, 51 skipped**, in 688.23 seconds, with at most four workers. Five
warnings remain: two deliberate malformed-artifact serializer warnings and three
IPython temporary-directory fallback warnings. The initial evolving-source sweep
passed 14,932 cases but loaded an old image assertion while its recipe changed;
its single failure is preserved, and the frozen complete repeat supersedes it.
No assertion was relaxed to hide that mismatch.

Web validation passed **2,241 tests**, strict Svelte/TypeScript, Prettier, npm
license checks and both production builds. Post-build math/figure tests passed
**55 cases**. Ruff format/lint, strict Pyright, test tiers, all 86 installed Python
distribution licenses and generated protocol checks passed. Strict Kubeconform
v0.8.0 validated all **10 resources** against both Kubernetes 1.34.0 and 1.35.0
schemas. This is offline validation, not cluster admission or CNI acceptance.

Independent reviews are clear of critical/high/medium findings. In addition to the
index and socket fixes, the integrated audit corrected a cold-PVC startup cycle,
missing workspace model configuration, CLI alias delimiters and packaging inputs.
The real cold workspace test reaches login/projects/settings before any runtime;
research efforts default to max and reviewer effort to medium, with configuration
for each role. Runtime bootstrap is explicitly identified as unimplemented.

## Local images and transport

All three images were built and retained **locally only**, for Linux AMD64 from
source `912633b5`. The worker build eventually fetched the same fixed Ubuntu
snapshot; no fallback to current packages was introduced. Earlier snapshot 502/503
failures are preserved. Bounded transport retries and strict index failure avoid
misreporting missing packages. Worker-only Git/OpenSSH no longer inflate app
requirements. The web build's missing repository LICENSE input was repaired while
preserving the exact notice aggregator and digest checks.

| Local image | Docker image ID | Bytes |
| --- | --- | ---: |
| `sandbox-science/trusted-app:912633b5` | `sha256:6c844dc30ca9c051f88beb7fc4f519750b4e1e63a0aba9aa9069c830d0478480` | 202,226,015 |
| `sandbox-science/trusted-worker:912633b5` | `sha256:7b500c7567576e420e36e5d385133f2cdbd6aba2ae7ad2ed37d7ed1b18ba7411` | 199,146,015 |
| `sandbox-science/orchestrator:912633b5` | `sha256:84ecb4b9ded67c62f8ba1dda4bd3b52cacd26c52e6b971497cf1820a520944fd` | 131,400,353 |

Trusted image checks passed installed CLIs, helper availability, no research kernels,
packaged assets/skills/SDK, native OpenShell version and real positive/negative
production math/figure checking. The separate orchestrator image passed **17
container/Landlock checks** without external network access.

A disposable app-image process test passed verified TLS control readiness before
runtime connection, cold login, production JavaScript and model-settings HTTP 200.
A three-image test on an internal Docker network then passed native worker passive
readiness, verified TLS control/runtime handshake, drain after interruption and
reconnection of the same runtime container. The refusing fake model gateway recorded
**zero requests**. All owned containers and the network were removed; synthetic
keys and payload trees were deleted. This checks packaged service compatibility;
it does not qualify OpenShell VM ownership, Kubernetes interruption or scientific
Resume. An earlier channel pilot omitted a worker and correctly failed its readiness
prerequisite; those diagnostic failures remain in the evidence.

## Evidence

Sanitized logs, lab scripts, source input hashes and local image identities are at
`/var/tmp/ss-k8s-operations-evidence-20261020`. Its **56-file** `SHA256.json` manifest
has SHA-256 `85b5f622dfebf001edf6a1ef72967c3d4478e1a2d2177fa790c157e771f7496d`.
Original-log hashes distinguish normalization from the original receipts. Selected
archived file hashes:

| File | SHA-256 |
| --- | --- |
| `ss-k8s-operations-frozen-20261020.log` | `921c1e542b5111a0b331b6597bd9646b4781e040a610b7c45f9464127b665848` |
| `ss-k8s-checkpoint-garage-final-20261020.log` | `067f2f25b6111fc845839521b849104b0479bdc5daa77368c94ebb366bb207ab` |
| `ss-k8s-container-channel-native-20261020.log` | `17b07d08e1dd65000db432123bcd2b4b6ef547aa6ae56b02eca4495779e0981a` |
| `local-images.json` | `f0e45fcd6b9aade16135ad52cc1c65aa8b7869f347e2ac277a1a8f35233d6a3a` |
| `source-inputs-sha256.json` | `fc468e8c2894bc220bfdac243b74848c69139efb7826cddb42fa4598368143c9` |


Still open: coordinated metadata/workspace recovery sets, cluster writer fencing
before PVC release, recorded agent-runtime bootstrap/restart, real CNI/supervisor
isolation, native ARM64/crypto-base and Sage/MCP/PDF interruption/Resume acceptance.
The original host launcher cannot be placed unchanged in a Deployment to provide
those guarantees. Image publication remains explicitly deferred.
