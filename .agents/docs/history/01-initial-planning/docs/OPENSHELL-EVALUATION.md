# OpenShell evaluation

Assessed: 2026-10-10. Recommendation: evaluate OpenShell as the first execution backend behind a replaceable workbench adapter. Adoption remains conditional on a measured feasibility spike.

## Verified source baseline

GitHub's release API reported **v0.1.3**, published **2026-10-09T15:22:35Z**. The documentation read during this assessment labels its current version v0.1.3. Pin SDK, CLI, gateway, trusted runtime/supervisor, and image references to one tested release set; record resolved digests before installation. No OpenShell runtime was installed or exercised in this planning pass. [Release](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.3)

| Documented feature | Consequence for this project |
| --- | --- |
| Docker, Podman, Kubernetes, and MicroVM compute drivers; one active compute driver per gateway | Evaluate a local Docker or Podman pool and a remote container pool first. Use separate pool/gateway registrations for heterogeneous backends. [Runtimes](https://docs.nvidia.com/openshell/latest/how-it-works/sandboxes/runtimes) |
| Trusted supervisor separated from the workload; mediated network access and kernel confinement | Keep the workbench controller and credentials outside generated-code workloads. Verify enforcement on the target, including failure cases. [Architecture](https://docs.nvidia.com/openshell/latest/about/architecture) |
| Gateway registration, authentication, sandbox lifecycle and policy/provider management | Use a worker-side gateway client. Do not expose gateway administration to the browser or model. [Gateways](https://docs.nvidia.com/openshell/latest/how-it-works/gateways/overview) |
| Python SDK lifecycle/exec APIs, explicit workspace selection, and service authentication guidance | Candidate worker implementation: Python plus the matching SDK. Verify streaming, cancellation, file transfer, and reconciliation instead of assuming `exec()` covers them. [Python SDK](https://docs.nvidia.com/openshell/latest/sdk/python) |
| mTLS, OIDC, and trusted edge authentication options | Choose a tested machine-auth mode for worker controllers; separate it from browser sign-in. [Authentication](https://docs.nvidia.com/openshell/latest/how-it-works/gateways/authentication) |
| Containerized gateway deployment with privileged infrastructure dependencies | Package the gateway separately from the application. Review host paths, runtime socket permissions, callbacks, certificates, and volumes against the pinned release. [Container deployment](https://docs.nvidia.com/openshell/latest/how-it-works/gateways/container-deployment) |
| Breaking deployment/policy/API/SDK changes between 0.0.x and 0.1.0 | Avoid old examples and mixed-release peers. Make upgrade compatibility an explicit gate. [Upgrade guide](https://docs.nvidia.com/openshell/latest/upgrade/0-1-0) |

Older search results describe a K3s cluster inside Docker. The current runtime documentation describes several compute drivers; the workbench must not assume that local execution always requires a nested Kubernetes cluster.

There is also apparent documentation drift: the container-deployment page describes mounting an `openshell-sandbox` supervisor binary from a host path, while the architecture/runtime pages describe separate supervisor containers and trusted runtime/supervisor image settings. Treat this as an unresolved deployment detail. Inspect the v0.1.3 deployment files and verify the actual running boundary before writing project Compose manifests. Do not combine incompatible snippets.

## What the workbench must supply

Research sessions, model-loop/harness integration, project storage, the worker enrollment/lease transport, scientific profile locks, capability probes, scheduling, input/output transfer, authoritative provenance, artifact viewers, experiment review, and export/import remain project responsibilities. A policy-enforcing sandbox does not establish scientific correctness or reproduce an experiment by itself.

## Feasibility spike

The spike is a deterministic execution service, initially driven from a CLI rather than an LLM. Use a disposable project namespace and dedicated test data. Start with small CPU jobs and prebuilt images; add Sage/fpylll after lifecycle and isolation work.

| Check | Experiment | Required evidence |
| --- | --- | --- |
| Pinned deployment | Launch matching gateway/controller/SDK components using release-specific instructions | Resolved versions and digests, process/container inventory, documented trusted sockets/mounts and workload boundary |
| Functional execution | Run `2+2`, then a finite-field identity with a deterministic expected result | Actual sandbox command, exit code, output, image/platform/policy identity |
| Streaming | Emit timestamped stdout/stderr over several seconds | Incremental events, preserved ordering per stream, output bounds and disconnect behavior |
| Input/output transfer | Stage a script and input; export JSON plus a plot | Digests before/after transfer, no reliance on a common host path, explicit transfer mechanism |
| Filesystem policy | Try reading a host-only sentinel and writing outside allowed workspace; test symlink traversal | Denied access and useful diagnostics; successful permitted operations |
| Network policy | Contact an allowed fixture endpoint, then a denied destination and metadata/private-network fixtures | Allowed/denied results and policy events; DNS/redirect behavior; no automatic grant |
| Credentials | Use a mock credentialed endpoint and inspect job-visible state | Endpoint scoping, no real secret printed or written into artifacts; evaluate whether permitted endpoints could reflect credentials |
| Resource controls | Exceed CPU/memory/PID/output/wall-time bounds in controlled jobs | Driver-specific limit behavior; termination and cleanup records; no silent unsupported limit |
| Cancellation | Spawn a child process and cancel during execution | Termination observed for process tree; retained partial logs; bounded cleanup time |
| Disconnection | Interrupt controller↔API and controller↔gateway paths separately | Actual runtime behavior, local watchdog, reconciled status, no indefinite orphan job |
| Recovery/idempotency | Restart controller or orchestrator during creation/execution/collection; replay a request | One logical attempt correlation, stale-lease rejection, no duplicate committed output |
| Remote placement | Run the same manifest through a second host, then hosted orchestration with local worker | Authenticated transport, no exposed local engine API, hashes and lineage preserved |
| Capabilities | Import and execute fpylll; run a Sage calculation inside the selected policy | Working library/tool proof in the actual sandbox, not only installed-package metadata |
| Baseline cost | Measure cold and warm image/sandbox startup, execution overhead, transfer rate, idle footprint | Raw measurements, hardware/configuration, repeat count; no invented latency target |

Run negative checks in the intended host kernel/container/CNI environment. Kubernetes NetworkPolicy enforcement depends on the cluster's network implementation; do not equate an accepted policy object with an enforced boundary. A GPU path requires its own target-specific validation.

Pass when deterministic execution, isolation, artifact capture, cancellation, recovery, and local/remote transport work on the selected release. If a required SDK operation is missing, a narrowly scoped CLI bridge is acceptable during the spike with structured arguments and explicit error handling. Keep unsupported behavior visible. If the backend cannot meet the boundary, stop adoption and assess a separately documented alternative; never silently fall back to host execution.

## Open questions

- Which local backend works best on the target Ubuntu host: Docker or rootless Podman? Do not change existing services to answer this without need.
- What exact v0.1.3 deployment recipe resolves the supervisor binary/image discrepancy?
- Which SDK operations provide reliable streams, file transfer, cancellation, policy diagnostics, and cleanup acknowledgement?
- Which controls and resource measurements are actually supported per selected driver?
- How much application-level reconciliation is required after gateway restarts or worker leases expire?
- Which endpoint-bound credential behavior is safe for the intended model gateways and MCP services?
- Does the environment need a VM isolation boundary, or is a dedicated container host sufficient for the initial trusted-user deployment?

