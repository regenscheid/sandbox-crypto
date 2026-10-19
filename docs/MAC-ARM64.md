# Apple Silicon qualification

This is the path toward a local ARM64 Mac deployment. The project has not yet passed a Mac run. Its current `sandbox-science up` launcher uses Linux `/proc` for process identity and cleanup and a Linux `prctl` death signal. Running that launcher directly on macOS would fail; an ARM64 image build alone does not make the application Mac-ready.

## Initial runtime choice

Use Docker Desktop to build and store the Linux ARM64 profile images. For sandbox execution, first qualify OpenShell's local MicroVM driver on the Mac, with the same policy, worker conformance and recovery probes used on babbage. This gives each research sandbox its own VM boundary if the pinned OpenShell release and Mac host pass those probes. The Docker driver is a possible fallback to investigate, but containers in Docker Desktop share its Linux VM kernel and have a weaker isolation tier than the intended MicroVM path. Do not silently switch drivers when a policy or isolation probe fails.

A single-node Kubernetes cluster adds scheduling, networking and storage setup without resolving the current host launcher's Linux assumptions. Apple `container` runs OCI images in lightweight VMs on macOS 26, but it is not one of OpenShell's documented compute drivers. Neither is the first integration target.

## Sequence and evidence

1. **Native ARM64 image gate.** Run the manually dispatched `arm64 image qualification` workflow on `dev`. It builds and checks `minimal`, `orchestrator` and `mcp-proxy` on a Linux ARM64 runner. `images/build.sh` now selects the host architecture by default; `PLATFORM=linux/arm64` states it explicitly. This gate does not test OpenShell or macOS. The `crypto-base` image still needs a larger ARM64 builder and confirmation that Ubuntu's pinned ports snapshot resolves.
2. **Portable host supervision.** Replace the launcher's Linux `/proc` process identity, session enumeration and `prctl` trampoline with a Darwin implementation that retains its PID reuse and orphan cleanup guarantees. Exercise start, crash, `down`, replacement and resume on the Mac before claiming local support. The trusted worker, control store and GUI can then run as host processes; research code remains in OpenShell sandboxes.
3. **Mac gateway and policy gate.** Install a pinned OpenShell release, choose the MicroVM driver explicitly, and validate gateway, middleware registration, policy compilation, provider binding, file confinement, outbound network decisions, SSH worker transport and worker lifecycle against actual ARM64 sandboxes. Build or transfer the ARM64 profile images without publishing `crypto-base` publicly. Confirm the browser login and PDF viewer's loopback origins.
4. **Recovery gate.** Interrupt long Sage work, the agent runtime and the worker separately. Confirm that one orchestrator Resume reconciles children and original effects without duplication and that durable artifacts survive sandbox replacement.

Keep the Mac's state directory and image tags separate from babbage. Record the host OS, OpenShell and Docker versions, image digests, actual isolation tier and each probe's result. A recipe test or Linux ARM64 job is preparation, not Mac acceptance.

## Kubernetes follow-on

The existing architecture keeps the worker pool distinct from the GUI and control store. For the mixed ARM64/AMD64 cluster, qualify native images on each architecture, then use a private registry for `crypto-base` and a multi-platform manifest for shared tags. Deploy an OpenShell Kubernetes gateway through its supported chart and controller, with the policy middleware and cache reachable from sandbox pods. Put trusted services and durable stores on protected nodes/storage; run research pods in separate node pools under a sandboxed RuntimeClass where available. Check node affinity and resource requests so Sage and large artifacts avoid small Raspberry Pi nodes. Validate PVC or object-store artifact retention, gateway restart, pod eviction, policy and credential boundaries, and tree Resume before calling it supported. The host launcher cannot simply be copied into a pod: its process/session supervision and loopback addresses assume one machine.

See [architecture](ARCHITECTURE.md#local-remote-and-kubernetes-placement), [images](IMAGES.md#arm64), [OpenShell's installation guide](https://docs.nvidia.com/openshell/v0.1.3/about/installation/), and [the OpenShell sandbox runtimes](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/runtimes).
