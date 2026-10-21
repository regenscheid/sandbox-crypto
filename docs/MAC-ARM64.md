# Apple Silicon qualification

For step-by-step installation and the first `crypto-base` Sage MicroVM test,
start with the [Mac setup guide](MAC-SETUP.md).

This is the path toward a local ARM64 Mac deployment. **The intended Mac test runs research in OpenShell MicroVMs, not in Docker containers.** The project has not yet passed a Mac MicroVM run. The launcher now has a Darwin process-identity implementation and a parent-pipe watcher for abrupt launcher death. A native `macos-15` ARM64 CI job runs the launcher tests; its result must be checked before treating even launcher behavior as qualified. Process discovery, `down`, replacement and Resume with a real OpenShell gateway still need acceptance on the owner's Mac. An ARM64 image build alone does not make the application Mac-ready.

## Initial runtime choice

Use OpenShell's local MicroVM driver for sandbox execution on the Mac, with the same policy, worker conformance and recovery probes used on babbage. This gives each research sandbox its own VM boundary if the pinned OpenShell release and Mac host pass those probes. The pinned VM driver can authenticate to GHCR and pull a private image directly into its own state directory, so Docker Desktop is not needed for the initial `crypto-base` test. Because that driver sends one registry credential to every pull, its bootstrap image is a digest-pinned GHCR mirror of NVIDIA's base image rather than nvcr.io itself (see [Images](IMAGES.md)). `docker run` only tests Docker and is not the Mac acceptance test. The OpenShell Docker driver is a possible fallback to investigate, but containers in Docker Desktop share its Linux VM kernel and have a weaker isolation tier than the intended MicroVM path. Do not silently switch drivers when a policy or isolation probe fails.

A single-node Kubernetes cluster adds scheduling, networking and storage setup without resolving the current host launcher's Linux assumptions. Apple `container` runs OCI images in lightweight VMs on macOS 26, but it is not one of OpenShell's documented compute drivers. Neither is the first integration target.

## Choose storage locations

Set `volume` in the [sample Mac config](../examples/mac-local.toml) before the first run. It must support Unix sockets (APFS is recommended), because the VM driver binds its control socket under `vm/run`; the helper probes this before starting the gateway. With the default `directory = "SandboxScience"`, the helper puts application records in `<volume>/SandboxScience/app`, prepared VM images and overlays in `vm`, and the dedicated gateway database, log and CLI state in `gateway`. It refuses an unmounted volume rather than silently writing to the system disk. Keep that volume mounted whenever the gateway or a sandbox is running; do not switch to an empty path to recover an interrupted run. The helper checks the VM socket path against macOS's limit.

OpenShell's [VM driver](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/crates/openshell-driver-vm/README.md) keeps its image cache and overlays under one `state_dir`. The helper generates the gateway config and sets its data and cache environment to paths on the selected volume. Docker Desktop's existing image store stays where it is. Homebrew program files and its normal cache still use their usual locations. Large research files that exist only inside a sandbox remain in its overlay until exported or archived.

## Mac MicroVM `crypto-base` test

The first Mac runtime test uses the published private ARM64 `crypto-base` image
and checks Sage arithmetic inside an OpenShell MicroVM. It does **not** yet
qualify the worker, UI or recovery on the Mac. The helper keeps the image cache,
VM overlays, gateway database and application state under one selected mounted
volume. It does not pull the image into Docker or move Docker Desktop's store.
OpenShell's pinned [VM driver](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/crates/openshell-driver-vm/README.md)
supports this layout; the owner's Mac has not yet run the test.

On the Mac, install [Homebrew](https://brew.sh/) if needed and clone the `dev`
branch (or update an existing clone). Copy the sample config to a private file
and change its **one required value**, `volume`, to an existing mounted local
volume. About 40 GiB free is recommended for the first image preparation. The
`directory` setting names a new folder on that volume; the helper refuses the
system root or an unmounted volume and checks that OpenShell's VM socket path
fits macOS's limit. The image tag, port, GitHub user and
Keychain service already have test defaults.

```sh
git clone --branch dev https://github.com/regenscheid/sandbox-crypto.git
cd sandbox-crypto
cp examples/mac-local.toml "$HOME/sandbox-science-mac.toml"
${EDITOR:-vi} "$HOME/sandbox-science-mac.toml"
./scripts/mac-local install
./scripts/mac-local smoke
```

For an existing clone, use `git switch dev && git pull` in place of the clone
command. Keep the terminal in the repository directory for all three helper
actions.

`install` installs the pinned OpenShell release through its
[official installer](https://docs.nvidia.com/openshell/v0.1.3/about/installation/)
after checking the installer SHA-256, plus Homebrew's `e2fsprogs` and `uv`
when needed. It writes a dedicated gateway
configuration on the selected volume, and prompts privately in the terminal
for a GitHub **classic** token with `read:packages` if none is stored under
the configured Keychain service name. The
installer may start OpenShell's normal Homebrew gateway; the helper does not
reconfigure it. Program files and ordinary Homebrew caches still use their
normal locations. The entrypoint installs Homebrew Python 3.13 only if the
available `python3` cannot run the helper. The token never goes into the TOML
or repository.

`smoke` starts a separate loopback gateway in the foreground, registers it in
a temporary CLI profile, pulls the private image directly into the VM cache,
checks `aarch64` and a Sage computation, deletes its uniquely named test
sandbox, and stops its gateway. It leaves the prepared image cache in place for
later use. Its gateway log is under the chosen volume at
`<volume>/<directory>/gateway/gateway.log`. The VM driver uses its own cache
even if Docker Desktop already has the image.
The first image preparation can take a while. If it fails, the log and VM cache
remain for diagnosis; the helper does not remove existing OpenShell state.

Later, after a private application config has been prepared and the full Mac
gates below are ready, set `application_config` in your private
`sandbox-science-mac.toml` and run:

```sh
./scripts/mac-local launch
```

`launch` starts that same dedicated gateway and runs `sandbox-science up`
in the foreground. It requires the application's `state_dir` to be
`<volume>/<directory>/app`, writes a temporary private copy of the application
config beside the original with its isolated OpenShell CLI paths for the
launcher and workers, and puts
uv's cache, managed Python installs and project environment on the selected
volume. Remove
`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` and
`OPENSHELL_LOCAL_TLS_DIR` and `OPENSHELL_GATEWAY` overrides from the
application's `[openshell.env]`;
the helper owns those for this launch and removes the temporary copy when it
stops. The application still needs its normal
private model, policy, image and build settings: the
[single-machine quickstart](QUICKSTART.md) describes those, but its example is
for babbage. Ctrl-C stops the foreground application and this helper's gateway.
Do not treat a passing image-only `smoke` as full application acceptance.

## Sequence and evidence

1. **Native ARM64 image gate.** The `arm64 image qualification` workflow has built and checked all four profiles, including the large Sage/G6K `crypto-base` image, on a Linux ARM64 runner. The [full publication run](https://github.com/regenscheid/sandbox-crypto/actions/runs/39348347538) passed both architectures' pulled-digest tests and merged every profile into a two-platform GHCR tag. `images/build.sh` selects the host architecture by default; `PLATFORM=linux/arm64` states it explicitly. This gate does not test OpenShell or macOS.
2. **Portable host supervision.** The Darwin path uses the process creation time and boot time from the kernel (through psutil), enumerates session members, and runs each part under a wrapper watching a pipe held only by the launcher. The wrapper ends its session when the pipe closes after a launcher crash or when it receives SIGTERM, including children that ignore SIGTERM. If the wrapper itself dies, the supervisor drains its session before waiting for output or restarting the part; `down` rechecks boot, creation time, session and command identity before cleanup. Exercise start, crash, `down`, replacement and Resume on the Mac before claiming local support. The trusted worker, control store and GUI then run as host processes; research code remains in OpenShell sandboxes.
3. **Mac gateway and policy gate.** Install a pinned OpenShell release, choose the MicroVM driver explicitly, and validate gateway, middleware registration, policy compilation, provider binding, file confinement, outbound network decisions, SSH worker transport and worker lifecycle against actual ARM64 sandboxes. Pull the private ARM64 `crypto-base` image directly through the VM driver with authorized GHCR access; then confirm the browser login and PDF viewer's loopback origins during the full-application test.
4. **Recovery gate.** Interrupt long Sage work, the agent runtime and the worker separately. Confirm that one orchestrator Resume reconciles children and original effects without duplication and that durable artifacts survive sandbox replacement.

Keep the Mac's state directories separate from babbage. Use the same immutable `linux/arm64` image variant as the ARM64 Kubernetes nodes when qualified; no Mac-specific tag is needed. Record the host OS, OpenShell version, image digests, actual isolation tier and each probe's result. A recipe test or Linux ARM64 job is preparation, not Mac acceptance.

## Kubernetes follow-on

The existing architecture keeps the worker pool distinct from the GUI and control store. For the mixed ARM64/AMD64 cluster, the four published tags provide native images for both architectures; `crypto-base` remains private. Deploy an OpenShell Kubernetes gateway through its supported chart and controller, with the policy middleware and cache reachable from sandbox pods. Put trusted services and durable stores on protected nodes/storage; run research pods in separate node pools under a sandboxed RuntimeClass where available. Check node affinity and resource requests so Sage and large artifacts avoid small Raspberry Pi nodes. Validate PVC or object-store artifact retention, gateway restart, pod eviction, policy and credential boundaries, and tree Resume before calling it supported. The host launcher cannot simply be copied into a pod: its process/session supervision and loopback addresses assume one machine.

See [architecture](ARCHITECTURE.md#local-remote-and-kubernetes-placement), [images](IMAGES.md#arm64), [OpenShell's installation guide](https://docs.nvidia.com/openshell/v0.1.3/about/installation/), and [the OpenShell sandbox runtimes](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/runtimes).
