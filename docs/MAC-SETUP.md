# Set up Sandbox Science on an Apple Silicon Mac

This guide starts with **Homebrew already installed**. The first milestone is a real
`crypto-base` Sage computation inside an OpenShell MicroVM. The later full
research-workspace launch is described separately because the MicroVM test alone
does not configure the worker, policy middleware, package cache, model gateway or
UI. Mac launch, restart and Resume have not yet passed native acceptance; see
[Mac qualification](MAC-ARM64.md) for those remaining checks.

## What you need before starting

- An Apple Silicon Mac and a native ARM64 terminal. OpenShell v0.1.5's MicroVM
  runtime uses macOS Hypervisor.framework; this path does not use Docker Desktop
  for research sandboxes. `uname -m` must print `arm64`, not `x86_64` under Rosetta.
- Homebrew, Git and internet access to GitHub Releases and GHCR. OpenShell's
  VM bootstrap image is a digest-pinned GHCR mirror of NVIDIA's
  `nvcr.io/nvidia/base/ubuntu:24.04`, because the VM driver sends its single
  registry credential to every pull and nvcr.io rejects a GitHub token. The
  same Keychain token pulls both images. The helper uses `curl`; macOS normally
  supplies it. You do **not** need to install Python, `uv`, `e2fsprogs` or
  OpenShell by hand: `install` installs missing prerequisites through Homebrew
  and OpenShell's pinned installer. If another OpenShell version is already on
  `PATH`, the helper stops rather than replacing it.
- A **mounted, writable local volume that supports Unix sockets** for the large
  files, such as `/Volumes/Research`. Set `volume` to the mount point itself,
  not a folder inside it. APFS is recommended; ExFAT and network shares may
  reject the VM driver's control socket. About 40 GiB free is recommended for
  the first image preparation; the helper warns below that amount. Its test
  gateway config assigns four
  vCPUs, 8 GiB of VM memory and a 16 GiB sparse writable overlay.
- Access to this repository's private `crypto-base` GHCR package. Have a GitHub
  **classic personal access token** with `read:packages` and access to the
  package ready. `install` asks for it without echoing it and stores it in
  macOS Keychain. This token is only for the image pull. The LiteLLM key is a
  different credential and is not needed for the first test.

Check the host and your chosen volume before cloning:

```sh
uname -m
brew --version
git --version
command -v curl
df -h /Volumes/Research       # replace with your actual mounted volume
```

If `git --version` starts the Apple Command Line Tools installer, complete it
before cloning. Homebrew is already installed in your case, so there is no
Homebrew setup step here.

The helper will refuse an unmounted volume, a volume that cannot host Unix
sockets, the system root, a path that makes the VM's Unix socket too long, or
an occupied dedicated gateway port. It keeps the existing Docker Desktop image
store and any existing OpenShell gateway configuration alone. OpenShell's
installer may start its normal Homebrew gateway; this guide's test uses a
**separate** gateway on port 18081.

## 1. Clone the development branch

In Terminal:

```sh
git clone --branch dev https://github.com/regenscheid/sandbox-crypto.git
cd sandbox-crypto
```

If you already have a clean clone, update it with `git switch dev` and
`git pull --ff-only`. Keep Terminal in the checkout for the helper commands.
The checkout itself stays wherever you cloned it; the large VM and application
state will use the volume you select below.

## 2. Configure the storage volume

Copy the small, non-secret [Mac helper config](../examples/mac-local.toml) to
your home directory:

```sh
cp examples/mac-local.toml "$HOME/sandbox-science-mac.toml"
${EDITOR:-vi} "$HOME/sandbox-science-mac.toml"
```

Change `volume` to your **actual mount point**, for example:

```toml
volume = "/Volumes/Research"
directory = "SandboxScience"
```

`directory` is a new, short folder name on that volume. The image tag, GitHub
user and dedicated port already have defaults. If port 18081 is in use, choose
another `gateway_port` **before** `install`; the helper deliberately refuses to
overwrite a generated gateway config later. Leave `application_config`
commented out for now. The helper config contains no model key or LiteLLM URL.

If your external SSD is ExFAT, keep its existing files and create an APFS
**sparse bundle disk image** on it with Disk Utility (File > New Image > Blank
Image). Give the mounted image a short name such as `ScienceVM`, choose APFS as
the format and Sparse Bundle Image as the image format, and set a maximum size
large enough for your research. It grows as data is stored. Mount that image,
then set `volume = "/Volumes/ScienceVM"` and run the helper. Keep both the SSD
and image mounted for every run. Leave the old `SandboxScience` directory on
ExFAT untouched. Do not copy its generated gateway config to the new path;
it contains absolute paths tied to the old mount point.

The default commands below read `$HOME/sandbox-science-mac.toml`. For a
different location, use `./scripts/mac-local --config /absolute/path/to/mac.toml install`
and the same `--config` option with `smoke` or `launch`.

## 3. Install the local test runtime

```sh
./scripts/mac-local install
```

This prepares owner-only directories on the chosen volume, installs missing
`e2fsprogs` and `uv`, installs **OpenShell v0.1.5** if absent, verifies the
downloaded installer SHA-256, checks the generated VM gateway config, and
verifies the separate `openshell-driver-vm` executable in the same Homebrew
installation as the gateway. It records that executable's directory in the
generated gateway config. It also creates the gateway's sandbox launch-signing
bundle under `gateway/tls/` with OpenShell's `generate-certs` command. That
command receives isolated XDG paths, so its CLI certificate copy stays on the
selected volume. The same bundle gives the gateway's listener its certificate:
the gateway listens on loopback over TLS and admits only clients with a
certificate from its CA, which `gateway add --local` copies into each run's
temporary CLI profile. Generation happens in a
temporary directory on that volume so interruption cannot leave a partial live
bundle. Existing complete signing files are reused, and previously partial
files are left for inspection rather than overwritten. A direct `smoke` run
also creates a missing bundle after checking
the VM driver, so an earlier `install` need not be repeated. The helper prompts
once for the GHCR token if its Keychain
item is absent. The entrypoint
also installs Homebrew Python 3.13 if your available `python3` is older than
3.11. The script never puts the token in the TOML or a shell argument.

Homebrew program files and its normal caches still occupy their usual disk.
The large files are organized as follows, with the default `directory`:

| Path under `<volume>/SandboxScience/` | Contents |
| --- | --- |
| `vm/` | OpenShell's prepared images, sandbox overlays, VM logs and driver socket |
| `gateway/` | The dedicated gateway's database, config, cache, TLS state and `gateway.log` |
| `services/` | The policy middleware's certificates, policy store, pool ceiling and settings, the package cache, the targets file and the services' logs, once `launch` runs |
| `app/` | The full application's state, once `launch` is configured |
| `cache/` | Helper download cache and, later, the application's uv cache and Python environment |

Do not move or delete these directories while a sandbox is in use. Re-run
`install` with the same settings if it was interrupted; it does not erase the
existing state. If you used an earlier version of this helper, `install` or
`smoke` adds the VM driver directory and the GHCR bootstrap image to an
unchanged helper-generated gateway config. It refuses to overwrite a config you
edited yourself.

## 4. Run the `crypto-base` MicroVM test

```sh
./scripts/mac-local smoke
```

The helper starts its isolated loopback gateway, pulls the published ARM64
`crypto-base` image into `vm/`, creates a uniquely named sandbox whose filesystem policy is OpenShell's default
plus read-only `/opt/sandbox-science` and which has no network access, checks that
the guest reports `aarch64`, and runs `print(GF(17)(3)^16)` with Sage. It then
deletes that sandbox and stops the dedicated gateway. The image cache remains
for a later run. Success includes a line beginning `PASS:`. The first image
preparation can be slow; it does not pull into Docker Desktop or move your
existing Docker data.

If it fails, read `<volume>/SandboxScience/gateway/gateway.log` and keep the
volume mounted. The helper prints that log path on exit. Do not delete the VM
cache to retry blindly: an image or sandbox may have been prepared before the
failure. A repeat `smoke` uses a new sandbox name and the same cache.

At this point you have tested image preparation and Sage inside the MicroVM.
You have **not** yet tested a research session, the browser workspace,
delegated agents, persistent documents or Resume.

## 5. Prepare the full application configuration later

`launch` needs a **second, private application TOML**. The Mac helper TOML
controls storage, GHCR access and the dedicated OpenShell gateway; the
application TOML controls projects, models, the worker, images, policy and UI.
Use [the application example](../examples/sandbox-science.toml) and
[the single-machine quickstart](QUICKSTART.md) as references, but do not launch
the example unchanged: it contains babbage-specific paths, image tags and a
placeholder model URL.

To start a private copy on the selected volume, substitute your actual mount
point in these commands:

```sh
cp examples/sandbox-science.toml "/Volumes/Research/SandboxScience/sandbox-science.toml"
chmod 600 "/Volumes/Research/SandboxScience/sandbox-science.toml"
${EDITOR:-vi} "/Volumes/Research/SandboxScience/sandbox-science.toml"
```

The copy now sits in a different directory from the example. Replace its
relative policy and web-build paths with valid absolute Mac paths, as well as
the babbage-specific values called out below.

For the model, the private application TOML needs at least:

```toml
state_dir = "/Volumes/Research/SandboxScience/app"

[model]
url = "https://your-litellm-host/v1"
name = "your-litellm-model-alias"
api_key_file = "~/.config/sandbox-science/secrets/litellm-harness.key"
```

Use the model alias configured at **your** LiteLLM gateway. Optional
`reviewer`, `delegated` and `watcher` aliases use this gateway too. The URL and
alias belong in the private application TOML; the **key contents belong only
in the referenced file**. Create and edit that file without placing the token
on a command line or in shell history:

```sh
install -d -m 700 "$HOME/.config/sandbox-science/secrets"
umask 077
${EDITOR:-vi} "$HOME/.config/sandbox-science/secrets/litellm-harness.key"
chmod 600 "$HOME/.config/sandbox-science/secrets/litellm-harness.key"
```

Enter the raw LiteLLM key in the editor. The launcher refuses a key file
readable by other users. Do not commit the private TOML or key file.

The rest of the application config also needs these Mac-specific corrections:

1. Set `state_dir` to `<volume>/<directory>/app`, exactly; the helper checks it.
2. Set `[worker] pool_id` for this Mac and `policy` to a real policy file.
3. Set `[openshell] cli` to the installed CLI (`openshell` is suitable when it
   is on `PATH`), `python` to the path inside the chosen worker image, and the
   isolation tier to the qualified MicroVM tier. Remove the example's
   babbage-only `[openshell.env]` paths: the helper writes the isolated CLI
   settings into a temporary private copy of the config for each launch.
4. Point the OpenShell service settings at the ones `launch` runs, under
   `<volume>/<directory>/services`; the helper checks them and prints the exact
   values when one differs. `cc` is the compiler the worker builds its
   scheduler shim with: the shim is preloaded in the Linux guest, so on a Mac
   it is cross-compiled with `zig cc` (which `install` installs through
   Homebrew), through a wrapper `launch` writes; the host's own `cc` cannot
   build it, and Jupyter kernels would not start without it:

   ```toml
   [openshell]
   package_cache = "http://host.openshell.internal:18083"
   cc = "/Volumes/Research/SandboxScience/cache/bin/cc-linux-aarch64"

   [openshell.environments]
   policy_store = "/Volumes/Research/SandboxScience/services/middleware/state"
   targets = "/Volumes/Research/SandboxScience/services/targets.yaml"
   ceiling = "/Volumes/Research/SandboxScience/services/middleware/pool-ceiling.yaml"
   ```

   Set its profiles to images this gateway can pull: the published two-platform
   tags, such as `ghcr.io/regenscheid/sandbox-crypto/crypto-base:6bb73871`, not
   the example's local `sandbox-science/...` tags. Set `[orchestrator]` to the
   matching orchestrator image and its **full embedded source revision**
   (`ghcr.io/regenscheid/sandbox-crypto/orchestrator:6bb73871` and
   `6bb738718cdde53199aacfd580eae005e983e3f9`); the launcher rejects a mismatch.
5. Build the web application and math checker in this checkout. They need
   Node.js 22.12 or newer; use 22.13 or newer for figure checks:

   ```sh
   cd web/workspace
   npm ci --ignore-scripts
   npm run build
   npm run build:math
   cd ../..
   ```

6. Set any optional `[mcp_direct]` and skill-pack settings you intend to use. An
   MCP server's provider must be registered with this gateway first
   ([MCP connectors](MCP-CONNECTORS.md)); `launch` registers only a placeholder
   for a policy provider with the `github-token` profile, as babbage does
   ([babbage](PERSONAL-BABBAGE.md)). The full Mac lifecycle and recovery path
   still needs native qualification; see
   [Mac qualification](MAC-ARM64.md#sequence-and-evidence).

When all those prerequisites exist, set `application_config` in
`$HOME/sandbox-science-mac.toml` to the **absolute** path of your private
application TOML, for example:

```toml
application_config = "/Volumes/Research/SandboxScience/sandbox-science.toml"
```

Then, from the repository checkout:

```sh
./scripts/mac-local launch
```

`launch` runs, in the foreground and in this order:

1. It adds `[worker] policy` to the policy store and writes the services'
   settings under `<volume>/<directory>/services`: the policy middleware's
   own CA and loopback certificate (made once and kept), its settings and pool
   ceiling, the package cache's settings, and the OpenShell targets file.
2. It starts the package cache on `127.0.0.1:18083` and the policy middleware
   on `127.0.0.1:18082` (TLS), with their logs in the `services` directory.
3. It starts the dedicated gateway with the middleware registered
   (`gateway/gateway-launch.toml`); the gateway refuses to start if it cannot
   reach the middleware.
4. It registers the policy's placeholder providers, then runs
   `sandbox-science up`.

Ctrl-C stops the launch, the gateway and then the services. The ports are
`gateway_port`, `middleware_port` and `package_cache_port` in the Mac TOML
(18081, 18082 and 18083 by default). The helper runs uv with the macOS system
trust store (`UV_SYSTEM_CERTS=1`) unless you set uv's TLS options yourself,
since a managed Mac that inspects TLS adds its CA there. A successful `smoke`
does **not** imply that `launch` will work with an unfinished application
config; the launcher reports missing files, images and services.
Preserve the selected volume and existing state when troubleshooting or
testing restart and Resume.

## Common problems

| Symptom | Check |
| --- | --- |
| `requires an Apple Silicon Mac` | Run in a native Mac terminal; `uname -m` must be `arm64`. |
| `Volume is not mounted` | Mount the chosen local volume and set `volume` to its root under `/Volumes`, not a subdirectory. |
| VM socket path is too long | Shorten `directory` or use a shorter volume name before `install`. |
| `Operation not supported (os error 45)` as the VM driver starts | The chosen volume may not support Unix sockets. Run `diskutil info /Volumes/ExternalSSD` (substitute your mount point) to inspect its filesystem, then run the updated helper's `install` to probe socket support. For ExFAT, use the APFS sparse bundle above or another APFS volume. Leave the existing `SandboxScience` directory in place; do not erase it. |
| Port 18081 is already in use | Choose a free `gateway_port` before `install`; do not overwrite a live gateway config or attach to another gateway. |
| Installed OpenShell version differs | The helper requires v0.1.5 and will not replace another version itself. Its error prints the upgrade command, `curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.5/install.sh \| OPENSHELL_VERSION=v0.1.5 sh`, which reinstalls the Homebrew formula and restarts its gateway service; the VM state on the volume is kept. The first `smoke` after upgrading from v0.1.3 prepares the VM images again once, since v0.1.4 changed the cache layout; the old cache files stay on the volume unused and can be removed by hand. |
| `vm compute driver binary not found` | Update the checkout with `git pull --ff-only`, then run `./scripts/mac-local install` and `./scripts/mac-local smoke` again. The helper now locates the driver in the gateway's Homebrew installation and pins its directory. If `install` says the executable is missing, inspect `brew list nvidia/openshell/openshell`; the pinned installation is incomplete and needs repair before a smoke retry. Preserve the selected-volume state. |
| `name exceeds maximum length (22 > 19)` during `smoke` | Update `dev` with `git pull --ff-only` and rerun `./scripts/mac-local smoke`. Older helper versions generated a test sandbox name longer than OpenShell v0.1.3 and v0.1.5 accept. The corrected name stays below 19 characters. |
| Sage exits with status 126 or `Permission denied` for `/opt/sandbox-science/bin/sage` | The helper predates its smoke sandbox policy; OpenShell's default policy does not include the image's Conda prefix. Update with `git pull --ff-only` and rerun `smoke`. |
| `failed to authenticate registry access for vm sandbox image 'nvcr.io/nvidia/base/ubuntu:24.04'` | The gateway config predates the GHCR bootstrap mirror. Run `git pull --ff-only`, then `./scripts/mac-local smoke`; the helper rewrites an unchanged generated config to the mirrored digest. |
| GHCR pull fails or returns access denied | Confirm the Keychain token is a classic PAT with `read:packages` and access to the private package; inspect `gateway.log` without sharing its credentials. |
| Gateway or Sage test fails | Read the printed gateway log path, keep the volume mounted and retry only after identifying the failure. The test cleans up its named sandbox when it can. |
| Full `launch` reports missing files | Recheck the private application TOML's paths, current image/revision, policy and targets; the babbage example is not a Mac-ready config. |

The pinned [OpenShell v0.1.5 installation guide](https://docs.nvidia.com/openshell/v0.1.5/about/installation/)
and [VM driver notes](https://github.com/NVIDIA/OpenShell/blob/v0.1.5/crates/openshell-driver-vm/README.md)
describe the external runtime used by these steps.
