# Kubernetes foundations, 2026-10-20

Branch `k8s`, based on `dev` at `af0fa0d5`. No Kubernetes context, cluster
inventory, private endpoints or real credentials were accessed. Site values are
not included. The Mac qualification remains open.

## Implemented scope

- Trusted per-profile Kubernetes node selectors, runtime class and CPU/memory/
  ephemeral-storage requests and limits, pinned atomically with sandbox ownership.
  Project/pool CPU and memory caps narrow the deployment limits. Existing
  allocations refuse incompatible tighter policies rather than claiming that a
  network-policy change resized the pod. Every offered Kubernetes profile pins
  AMD64 or ARM64 to preserve retained native packages across pod replacement;
  capacity selection remains independent of architecture.
- Target-native scheduler shim image recipes, source/ELF/loadability checks and a
  checked same-Linux-architecture fallback. Versioned no-replace installation
  protects an already loaded library. Images need rebuild/publication before
  cross-platform use of the shipped artifact.
- Optional S3-compatible immutable artifact storage, exact readback verification,
  cold seekable reads, bounded prefixes and operator archive/eviction commands.
  Local eviction is explicit and follows verified remote retention. No remote
  deletion, orphan/reservation sweeping, worker-replica or environment-PVC removal.

Independent reviews found and corrected tighter-policy/ceiling reuse bypasses.
Final reviews found no critical/high/medium issue in placement, shim or S3 scope.
The S3 SDK uses finite socket inactivity timeouts and cooperative elapsed checks;
blocking trickling calls do not have strict wall-clock preemption. Configured
storage endpoints are trusted operator configuration.

## Runtime storage evidence

A disposable local Garage 2.4.1 container used synthetic credentials, a dedicated
temporary bucket and loopback-only S3 exposure. Image:
`dxflrs/garage@sha256:9c96caa2612d3411acc5b0e6701fb238dbfba33e533a6d7d3d811a4b12d0d020`.
This is compatibility evidence for that temporary server, not acceptance of any
deployment's identity, storage durability, replication or network boundary.

- The opt-in endpoint test passed in 0.44 seconds.
- A 134,217,728-byte artifact was streamed, uploaded, fully verified, evicted and
  retrieved cold. Seek and full reads succeeded; the 4 MiB provenance prefix
  matched. Peak traced Python allocation was 21,175,523 bytes. SHA-256:
  `a626d17da2e502f5b4b8e3ebd23f0bf9daef6255688d8e0bb482b3ae3794a682`.
- Two identical 832-byte commits exercised conditional-write retry, then local
  eviction and a successful cold read.
- The owned container was stopped; Docker inspection confirmed it absent. No
  object service or test container remains running.

Evidence remains outside Git at `/var/tmp/ss-k8s-s3-ec5lftnt`; the five-file
`SHA256.json` manifest names:

| File | SHA-256 |
| --- | --- |
| qualification.log | ac03dc55bdc1ce87ab5a711fdf5a7d72d345e62c471c4cb4e9de283e03475f14 |
| large.log | 5409235266269a46ba49e42fb52f67286711525465671a18001afc01d4ec0738 |
| large-result.json | dc6ba00c769f7b216cb9ba86afca3d6adb3f6efa16a8a99c910880bcd7765f7f |
| retry.log | 9be33107d4bf5cd0612ff7a8d958b1916a00bc0ddaea75eb7a49bcb2c2c91a02 |
| retry-result.json | e45c7d7201d69ea20511d193f29473fddc60622da759ef4b8594def7755f6b59 |

## Qualification still open

Integrated Python checks passed **14,608 tests, 51 skipped** in 699.75 seconds,
with the two known malformed-fixture serializer warnings. The final architecture
pin and complete-profile-map guard then passed **177 affected configuration and
lifecycle tests**. The full web suite passed **2,239 tests** with Svelte/TypeScript,
Prettier and both builds clean. Ruff, strict Pyright, licensing, generated proto,
test-tier and repository-hygiene checks passed. All tests used synthetic data;
no model calls or existing research services were exercised.

The first local native image build hit HTTP 500/502 from the pinned Ubuntu
snapshot while fetching the compiler stage. A single retry succeeded against the
same pinned archive. AMD64 minimal image `sandbox-science/minimal:38247c47`, index
digest `sha256:d6e98268d0a2e03a601be28d86fa84f9ee75b474415e60e7386ebf6141337b7a`,
passed all eight container checks and nine Landlock checks, including native shim
identity/loadability and real Python kernel/executor use. Networking was disabled
for these checks. No image was published; ARM64/OpenShell/Kubernetes execution
of this change remains untested.

Whole-workspace archival and restoration, package-prefix persistence, durable
worker capture release, upload originals outside SQLite, reachable authenticated
control callbacks and trusted service packaging/manifests remain in the
[Kubernetes plan](../../../docs/KUBERNETES.md). Recorded artifacts are incomplete
workspace coverage and cannot authorize deleting an active or paused research PVC.
