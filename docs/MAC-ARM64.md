# Apple Silicon qualification

This is the path toward a local ARM64 Mac deployment. The project has not yet passed a Mac run. The launcher now has a Darwin process-identity implementation and a parent-pipe watcher for abrupt launcher death. A native `macos-15` ARM64 CI job runs the launcher tests; its result must be checked before treating even launcher behavior as qualified. Process discovery, `down`, replacement and Resume with a real OpenShell gateway still need acceptance on the owner's Mac. An ARM64 image build alone does not make the application Mac-ready.

## Initial runtime choice

Use Docker Desktop to build and store the Linux ARM64 profile images. For sandbox execution, first qualify OpenShell's local MicroVM driver on the Mac, with the same policy, worker conformance and recovery probes used on babbage. This gives each research sandbox its own VM boundary if the pinned OpenShell release and Mac host pass those probes. The Docker driver is a possible fallback to investigate, but containers in Docker Desktop share its Linux VM kernel and have a weaker isolation tier than the intended MicroVM path. Do not silently switch drivers when a policy or isolation probe fails.

A single-node Kubernetes cluster adds scheduling, networking and storage setup without resolving the current host launcher's Linux assumptions. Apple `container` runs OCI images in lightweight VMs on macOS 26, but it is not one of OpenShell's documented compute drivers. Neither is the first integration target.

## Choose storage locations

The Mac setup must let the owner choose paths **before the first gateway or research run**. There are separate stores; setting only the application's `state_dir` does not move OpenShell VM disks or Docker's build cache. Use an absolute path on a local filesystem for each store. A short path for VM state, such as `/Volumes/Research/ss-vm`, also avoids macOS's Unix-socket path limit. Keep the selected volume mounted whenever the gateway or a sandbox is running; never switch a configured path to a fresh empty directory to recover an interrupted run.

| Store | Set it with | What it contains |
| --- | --- | --- |
| Sandbox Science | `state_dir = "/Volumes/Research/ss-app"` in `sandbox-science.toml` | Project/control records, managed documents and artifact records, worker records, tokens and logs. The launcher already accepts this path and requires its directory to be owned by the user with mode 700. Large files still present only in a sandbox live in its VM overlay until explicitly exported or archived. |
| OpenShell MicroVMs | `state_dir = "/Volumes/Research/ss-vm"` under `[openshell.drivers.vm]` in the gateway TOML | Prepared image cache, each sandbox's writable overlay disk, VM logs and driver socket. This is one OpenShell setting; image cache and overlays cannot currently be placed separately. |
| OpenShell gateway | Set `XDG_STATE_HOME` and `OPENSHELL_LOCAL_TLS_DIR` in the **gateway service's** environment, or set `OPENSHELL_DB_URL` for its SQLite database | Gateway database and local TLS credentials. Changing the invoking shell's environment alone does not reconfigure an already running Homebrew service. Keep the database and its WAL files together on local storage. |
| Docker Desktop build store | Docker Desktop **Settings → Resources → Advanced → Disk image location** | Locally built OCI images and Docker's build/container data. This does not move OpenShell's prepared-image cache. Registry-sourced images still occupy the OpenShell VM state directory after preparation. |

For the pinned OpenShell release, the gateway TOML should select the VM driver and set its state directory explicitly:

```toml
[openshell]
version = 2

[openshell.gateway]
compute_driver = "vm"

[openshell.drivers.vm]
state_dir = "/Volumes/Research/ss-vm"
```

This is a path example, not a ready-to-run gateway configuration: retain the release's existing TLS, endpoint and image settings. The OpenShell `driver_dir` setting selects the driver **executable**, not image storage. For a private Mac installation, do not edit the owner's existing gateway or move its live data in place; use a dedicated gateway configuration and verify its effective paths before starting research. The Mac acceptance gate must confirm that gateway restart reuses the chosen database, image cache and original sandbox overlays, and that the application's managed documents and artifact records remain under its selected `state_dir`. Back up or migrate existing state while the service is stopped, preserving ownership and permissions.

For example, set `XDG_STATE_HOME=/Volumes/Research/ss-gateway` and `OPENSHELL_LOCAL_TLS_DIR=/Volumes/Research/ss-gateway/tls` in that gateway service's environment, then verify its database appears under the chosen root. Alternatively, set `OPENSHELL_DB_URL=sqlite:/Volumes/Research/ss-gateway/openshell.db?mode=rwc` for the database alone. These are gateway-service settings, not `[openshell.env]` entries in the Sandbox Science config; the latter affect CLI calls from the launcher and worker. Docker Desktop's disk-image location is selected in Docker Desktop itself. [OpenShell's VM driver](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/crates/openshell-driver-vm/README.md#gateway-and-driver-configuration) and [Docker Desktop settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/#advanced) document these separate stores.

## Sequence and evidence

1. **Native ARM64 image gate.** The `arm64 image qualification` workflow has built and checked all four profiles, including the large Sage/G6K `crypto-base` image, on a Linux ARM64 runner. `images/build.sh` selects the host architecture by default; `PLATFORM=linux/arm64` states it explicitly. This gate does not test OpenShell or macOS. Shared GHCR tags remain pending registry publication.
2. **Portable host supervision.** The Darwin path uses the process creation time and boot time from the kernel (through psutil), enumerates session members, and runs each part under a wrapper watching a pipe held only by the launcher. The wrapper ends its session when the pipe closes after a launcher crash or when it receives SIGTERM, including children that ignore SIGTERM; `down` rechecks boot, creation time, session and command identity before cleanup. Exercise start, crash, `down`, replacement and Resume on the Mac before claiming local support. The trusted worker, control store and GUI then run as host processes; research code remains in OpenShell sandboxes.
3. **Mac gateway and policy gate.** Install a pinned OpenShell release, choose the MicroVM driver explicitly, and validate gateway, middleware registration, policy compilation, provider binding, file confinement, outbound network decisions, SSH worker transport and worker lifecycle against actual ARM64 sandboxes. Build or transfer the ARM64 profile images without publishing `crypto-base` publicly. Confirm the browser login and PDF viewer's loopback origins.
4. **Recovery gate.** Interrupt long Sage work, the agent runtime and the worker separately. Confirm that one orchestrator Resume reconciles children and original effects without duplication and that durable artifacts survive sandbox replacement.

Keep the Mac's state directories separate from babbage. Use the same immutable `linux/arm64` image variant as the ARM64 Kubernetes nodes when qualified; no Mac-specific tag is needed. Record the host OS, OpenShell and Docker versions, image digests, actual isolation tier and each probe's result. A recipe test or Linux ARM64 job is preparation, not Mac acceptance.

## Kubernetes follow-on

The existing architecture keeps the worker pool distinct from the GUI and control store. For the mixed ARM64/AMD64 cluster, qualify native images on each architecture, then use a private registry for `crypto-base` and a multi-platform manifest for shared tags. Deploy an OpenShell Kubernetes gateway through its supported chart and controller, with the policy middleware and cache reachable from sandbox pods. Put trusted services and durable stores on protected nodes/storage; run research pods in separate node pools under a sandboxed RuntimeClass where available. Check node affinity and resource requests so Sage and large artifacts avoid small Raspberry Pi nodes. Validate PVC or object-store artifact retention, gateway restart, pod eviction, policy and credential boundaries, and tree Resume before calling it supported. The host launcher cannot simply be copied into a pod: its process/session supervision and loopback addresses assume one machine.

See [architecture](ARCHITECTURE.md#local-remote-and-kubernetes-placement), [images](IMAGES.md#arm64), [OpenShell's installation guide](https://docs.nvidia.com/openshell/v0.1.3/about/installation/), and [the OpenShell sandbox runtimes](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/runtimes).
