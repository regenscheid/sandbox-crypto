# Apple Silicon qualification

This is the path toward a local ARM64 Mac deployment. **The intended Mac test runs research in OpenShell MicroVMs, not in Docker containers.** The project has not yet passed a Mac MicroVM run. The launcher now has a Darwin process-identity implementation and a parent-pipe watcher for abrupt launcher death. A native `macos-15` ARM64 CI job runs the launcher tests; its result must be checked before treating even launcher behavior as qualified. Process discovery, `down`, replacement and Resume with a real OpenShell gateway still need acceptance on the owner's Mac. An ARM64 image build alone does not make the application Mac-ready.

## Initial runtime choice

Use OpenShell's local MicroVM driver for sandbox execution on the Mac, with the same policy, worker conformance and recovery probes used on babbage. This gives each research sandbox its own VM boundary if the pinned OpenShell release and Mac host pass those probes. The pinned VM driver can authenticate to GHCR and pull a private image directly into its own state directory, so Docker Desktop is not needed for the initial `crypto-base` test. `docker run` only tests Docker and is not the Mac acceptance test. The OpenShell Docker driver is a possible fallback to investigate, but containers in Docker Desktop share its Linux VM kernel and have a weaker isolation tier than the intended MicroVM path. Do not silently switch drivers when a policy or isolation probe fails.

A single-node Kubernetes cluster adds scheduling, networking and storage setup without resolving the current host launcher's Linux assumptions. Apple `container` runs OCI images in lightweight VMs on macOS 26, but it is not one of OpenShell's documented compute drivers. Neither is the first integration target.

## Choose storage locations

The Mac setup must let the owner choose paths **before the first gateway or research run**. There are separate stores; setting only the application's `state_dir` does not move OpenShell VM disks or Docker's build cache. Use an absolute path on a local filesystem for each store. A short path for VM state, such as `/Volumes/Research/ss-vm`, also avoids macOS's Unix-socket path limit. Keep the selected volume mounted whenever the gateway or a sandbox is running; never switch a configured path to a fresh empty directory to recover an interrupted run.

| Store | Set it with | What it contains |
| --- | --- | --- |
| Sandbox Science | `state_dir = "/Volumes/Research/ss-app"` in `sandbox-science.toml` | Project/control records, managed documents and artifact records, worker records, tokens and logs. The launcher already accepts this path and requires its directory to be owned by the user with mode 700. Large files still present only in a sandbox live in its VM overlay until explicitly exported or archived. |
| OpenShell MicroVMs | `state_dir = "/Volumes/Research/ss-vm"` under `[openshell.drivers.vm]` in the gateway TOML | Prepared image cache, each sandbox's writable overlay disk, VM logs and driver socket. This is one OpenShell setting; image cache and overlays cannot currently be placed separately. |
| OpenShell gateway | Set `XDG_STATE_HOME` and `OPENSHELL_LOCAL_TLS_DIR` in the **gateway service's** environment, or set `OPENSHELL_DB_URL` for its SQLite database | Gateway database and local TLS credentials. Changing the invoking shell's environment alone does not reconfigure an already running Homebrew service. Keep the database and its WAL files together on local storage. |
| Existing Docker Desktop image store | Leave its current location unchanged for this test | Docker's other images, builds and containers stay where they are. OpenShell downloads and prepares `crypto-base` in its own VM state directory. |

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

## Mac MicroVM `crypto-base` test

This is the first Mac runtime test; the smaller `minimal` image is optional.
It tests a private ARM64 `crypto-base` image with actual Sage arithmetic in a
MicroVM. It does **not** yet qualify the worker, application UI or automatic
recovery on the Mac. No Docker pull or change to Docker Desktop's shared disk
image is required. OpenShell v0.1.3's VM driver reads
`OPENSHELL_REGISTRY_TOKEN` (and optional `OPENSHELL_REGISTRY_USERNAME`) from its
own process environment and downloads registry layers under its `state_dir`.
The [pinned driver source](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/crates/openshell-driver-vm/src/driver.rs)
implements this path; it has not yet been exercised on the owner's Mac.

Install [Homebrew](https://brew.sh/) if it is not installed, then install the pinned OpenShell
release and the VM driver's filesystem tools. The installer starts a normal
Homebrew gateway; leave that gateway and its state alone. The commands below
start a **separate, temporary gateway** on port 18081 for this test.

```sh
brew install e2fsprogs
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | OPENSHELL_VERSION=v0.1.3 sh
openshell --version
```

Choose an **existing, mounted local volume** and replace the example path in
the next command. The mount check prevents a missing external volume from
silently creating a directory on the Mac's normal disk. Keep at least 40 GiB
free there for the first preparation, cached image and experiment: the previous AMD64 build measured 2.34 GB
compressed and 6.2 GB unpacked, and the ARM64 size may differ. The 16 GiB
overlay below is sparse, but the VM driver says it must accommodate compressed
payload, unpacked rootfs and writes during preparation; its 4 GiB default is
insufficient for this image. These directories remain separate from Docker's
existing store.

```sh
SS_VOLUME="/Volumes/Research"
mount | grep -Fq " on $SS_VOLUME (" || { echo "Volume is not mounted: $SS_VOLUME"; exit 1; }
SS_ROOT="$SS_VOLUME/SandboxScience"
mkdir -p "$SS_ROOT/gateway/state" "$SS_ROOT/gateway/data" \
  "$SS_ROOT/gateway/cache" "$SS_ROOT/gateway/config" "$SS_ROOT/gateway/tls" \
  "$SS_ROOT/vm" "$SS_ROOT/app" "$SS_ROOT/cli-config"
chmod 700 "$SS_ROOT" "$SS_ROOT/gateway" "$SS_ROOT/gateway/state" \
  "$SS_ROOT/gateway/data" "$SS_ROOT/gateway/cache" \
  "$SS_ROOT/gateway/config" "$SS_ROOT/gateway/tls" \
  "$SS_ROOT/vm" "$SS_ROOT/app" "$SS_ROOT/cli-config"
df -h "$SS_ROOT"
cat > "$SS_ROOT/gateway/gateway.toml" <<EOF
[openshell]
version = 2

[openshell.gateway]
compute_driver = "vm"
disable_tls = true
bind_address = "127.0.0.1:18081"
image_preparation_timeout_seconds = 3600

[openshell.gateway.auth]
allow_unauthenticated_users = true

[openshell.drivers.vm]
state_dir = "$SS_ROOT/vm"
grpc_endpoint = "http://127.0.0.1:18081"
default_image = "nvcr.io/nvidia/base/ubuntu:24.04"
vcpus = 4
mem_mib = 8192
overlay_disk_mib = 16384
EOF
chmod 600 "$SS_ROOT/gateway/gateway.toml"
```

The package is private. Store a GitHub classic personal access token with
`read:packages` in the macOS Keychain; the following `security` command prompts
for its value without putting it in shell history. Use the existing item if one
is already stored under this service name. Do not put the token in the TOML,
the repository, or the command line as a literal.

```sh
security add-generic-password -U -a regenscheid -s sandbox-science-ghcr -w
```

In **terminal 1**, start the dedicated gateway in the foreground. The
Keychain lookup may ask for permission. The credential is passed only in the
gateway process environment; its VM driver inherits it. The gateway database,
SQLite WAL files and image preparation all stay under `SS_ROOT`. Leave this
terminal open during the test.

```sh
PATH="$(brew --prefix e2fsprogs)/sbin:$PATH" \
XDG_CONFIG_HOME="$SS_ROOT/gateway/config" \
XDG_STATE_HOME="$SS_ROOT/gateway/state" \
XDG_DATA_HOME="$SS_ROOT/gateway/data" \
XDG_CACHE_HOME="$SS_ROOT/gateway/cache" \
OPENSHELL_LOCAL_TLS_DIR="$SS_ROOT/gateway/tls" \
OPENSHELL_DB_URL="sqlite:$SS_ROOT/gateway/openshell.db?mode=rwc" \
OPENSHELL_REGISTRY_USERNAME=regenscheid \
OPENSHELL_REGISTRY_TOKEN="$(security find-generic-password -a regenscheid -s sandbox-science-ghcr -w)" \
openshell-gateway --config "$SS_ROOT/gateway/gateway.toml"
```

In **terminal 2**, set the same `SS_ROOT`. A private CLI config directory keeps
this test's gateway registration from changing any existing OpenShell CLI
default. The first create can take a while to pull and prepare the large image.
Use the published multiarchitecture tag; the VM driver selects its Linux ARM64
variant. Successful `uname` plus Sage arithmetic establishes that the image
runs, and the gateway's VM driver events establish that it ran in a MicroVM.

```sh
SS_ROOT="/Volumes/Research/SandboxScience"
export XDG_CONFIG_HOME="$SS_ROOT/cli-config"
openshell gateway add http://127.0.0.1:18081 --local --name ss-mac-crypto-test
openshell status
if command -v docker >/dev/null && docker image inspect ghcr.io/regenscheid/sandbox-crypto/crypto-base:6bb73871 >/dev/null 2>&1; then
  echo 'This exact image is already in Docker; the VM driver would use that copy.'
  exit 1
fi
openshell sandbox create --name ss-mac-crypto \
  --from ghcr.io/regenscheid/sandbox-crypto/crypto-base:6bb73871 \
  --detach -- /usr/bin/sleep infinity
openshell sandbox exec -n ss-mac-crypto --no-tty --no-login-shell -- /usr/bin/uname -m
openshell sandbox exec -n ss-mac-crypto --no-tty --no-login-shell -- \
  /opt/sandbox-science/bin/sage -c 'print(GF(17)(3)^16)'
openshell sandbox delete ss-mac-crypto
du -sh "$SS_ROOT/vm" "$SS_ROOT/gateway" "$SS_ROOT/app"
```

If the preflight finds this exact image in Docker, decide separately whether
it is still needed there before removing *that image alone*; changing Docker
Desktop's shared disk location is unnecessary. Expect `aarch64` and `1`.
Delete only the sandbox this test created, and stop
terminal 1 with Ctrl-C afterward. Preserve `SS_ROOT/vm` if you want the
prepared image cache for later tests; preserve `SS_ROOT/gateway` if a sandbox
or gateway record might need recovery. The Homebrew gateway remains separate.
For the later full application test, set `state_dir` in its private
`sandbox-science.toml` to `"/Volumes/Research/SandboxScience/app"` (or the
equivalent path you chose); the MicroVM image test itself does not launch the
application. Homebrew's program files and normal package-manager caches still
use their ordinary locations, but the large prepared research image and VM
disks use `SS_ROOT/vm`.
If image authentication fails, verify the token's package read permission and
that terminal 1 launched the *same* v0.1.3 driver; do not pull `crypto-base`
through Docker as an automatic workaround. If preparation reports insufficient
space, inspect free bytes on `SS_ROOT` and the VM preparation log before
changing paths or deleting state. The
[single-machine quickstart](QUICKSTART.md) currently describes the babbage lab;
copy its example config into a private Mac config and replace the babbage-only
paths and services before attempting `sandbox-science up`.

## Sequence and evidence

1. **Native ARM64 image gate.** The `arm64 image qualification` workflow has built and checked all four profiles, including the large Sage/G6K `crypto-base` image, on a Linux ARM64 runner. The [full publication run](https://github.com/regenscheid/sandbox-crypto/actions/runs/39348347538) passed both architectures' pulled-digest tests and merged every profile into a two-platform GHCR tag. `images/build.sh` selects the host architecture by default; `PLATFORM=linux/arm64` states it explicitly. This gate does not test OpenShell or macOS.
2. **Portable host supervision.** The Darwin path uses the process creation time and boot time from the kernel (through psutil), enumerates session members, and runs each part under a wrapper watching a pipe held only by the launcher. The wrapper ends its session when the pipe closes after a launcher crash or when it receives SIGTERM, including children that ignore SIGTERM. If the wrapper itself dies, the supervisor drains its session before waiting for output or restarting the part; `down` rechecks boot, creation time, session and command identity before cleanup. Exercise start, crash, `down`, replacement and Resume on the Mac before claiming local support. The trusted worker, control store and GUI then run as host processes; research code remains in OpenShell sandboxes.
3. **Mac gateway and policy gate.** Install a pinned OpenShell release, choose the MicroVM driver explicitly, and validate gateway, middleware registration, policy compilation, provider binding, file confinement, outbound network decisions, SSH worker transport and worker lifecycle against actual ARM64 sandboxes. Pull the private ARM64 `crypto-base` image directly through the VM driver with authorized GHCR access; then confirm the browser login and PDF viewer's loopback origins during the full-application test.
4. **Recovery gate.** Interrupt long Sage work, the agent runtime and the worker separately. Confirm that one orchestrator Resume reconciles children and original effects without duplication and that durable artifacts survive sandbox replacement.

Keep the Mac's state directories separate from babbage. Use the same immutable `linux/arm64` image variant as the ARM64 Kubernetes nodes when qualified; no Mac-specific tag is needed. Record the host OS, OpenShell version, image digests, actual isolation tier and each probe's result. A recipe test or Linux ARM64 job is preparation, not Mac acceptance.

## Kubernetes follow-on

The existing architecture keeps the worker pool distinct from the GUI and control store. For the mixed ARM64/AMD64 cluster, the four published tags provide native images for both architectures; `crypto-base` remains private. Deploy an OpenShell Kubernetes gateway through its supported chart and controller, with the policy middleware and cache reachable from sandbox pods. Put trusted services and durable stores on protected nodes/storage; run research pods in separate node pools under a sandboxed RuntimeClass where available. Check node affinity and resource requests so Sage and large artifacts avoid small Raspberry Pi nodes. Validate PVC or object-store artifact retention, gateway restart, pod eviction, policy and credential boundaries, and tree Resume before calling it supported. The host launcher cannot simply be copied into a pod: its process/session supervision and loopback addresses assume one machine.

See [architecture](ARCHITECTURE.md#local-remote-and-kubernetes-placement), [images](IMAGES.md#arm64), [OpenShell's installation guide](https://docs.nvidia.com/openshell/v0.1.3/about/installation/), and [the OpenShell sandbox runtimes](https://docs.nvidia.com/openshell/v0.1.3/how-it-works/sandboxes/runtimes).
