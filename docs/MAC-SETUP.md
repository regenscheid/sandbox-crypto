# Set up Sandbox Science on an Apple Silicon Mac

This guide starts with **Homebrew already installed**. The first milestone is a real
`crypto-base` Sage computation inside an OpenShell MicroVM. The later full
research-workspace launch is described separately because the MicroVM test alone
does not configure the worker, policy middleware, package cache, model gateway or
UI. Mac launch, restart and Resume have not yet passed native acceptance; see
[Mac qualification](MAC-ARM64.md) for those remaining checks.

## What you need before starting

- An Apple Silicon Mac and a native ARM64 terminal. OpenShell v0.1.3's MicroVM
  runtime uses macOS Hypervisor.framework; this path does not use Docker Desktop
  for research sandboxes. `uname -m` must print `arm64`, not `x86_64` under Rosetta.
- Homebrew, Git and internet access to GitHub Releases, GHCR and the registry
  used for OpenShell's bootstrap image. The helper uses `curl`; macOS normally
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
`e2fsprogs` and `uv`, installs **OpenShell v0.1.3** if absent, verifies the
downloaded installer SHA-256, checks the generated VM gateway config, and
verifies the separate `openshell-driver-vm` executable in the same Homebrew
installation as the gateway. It records that executable's directory in the
generated gateway config, and prompts once for the GHCR token if its Keychain
item is absent. The entrypoint
also installs Homebrew Python 3.13 if your available `python3` is older than
3.11. The script never puts the token in the TOML or a shell argument.

Homebrew program files and its normal caches still occupy their usual disk.
The large files are organized as follows, with the default `directory`:

| Path under `<volume>/SandboxScience/` | Contents |
| --- | --- |
| `vm/` | OpenShell's prepared images, sandbox overlays, VM logs and driver socket |
| `gateway/` | The dedicated gateway's database, config, cache, TLS state and `gateway.log` |
| `app/` | The full application's state, once `launch` is configured |
| `cache/` | Helper download cache and, later, the application's uv cache and Python environment |

Do not move or delete these directories while a sandbox is in use. Re-run
`install` with the same settings if it was interrupted; it does not erase the
existing state. If you used an earlier version of this helper, `install` or
`smoke` adds the VM driver directory to an unchanged helper-generated gateway
config. It refuses to overwrite a config you edited yourself.

## 4. Run the `crypto-base` MicroVM test

```sh
./scripts/mac-local smoke
```

The helper starts its isolated loopback gateway, pulls the published ARM64
`crypto-base` image into `vm/`, creates a uniquely named sandbox, checks that
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
4. Set `[openshell.environments]` policy store, targets and ceiling paths to
   actual Mac-accessible files and set its profiles to images available to
   this gateway. The example's `sandbox-science/...` tags are not the published
   GHCR tags. Provide a matching orchestrator image and its **full embedded
   source revision** in `[orchestrator]`; the launcher rejects a mismatch.
5. Build the web application and math checker in this checkout. They need
   Node.js 22.12 or newer; use 22.13 or newer for figure checks:

   ```sh
   cd web/workspace
   npm ci --ignore-scripts
   npm run build
   npm run build:math
   cd ../..
   ```

6. Make the trusted policy middleware and package cache available to the
   gateway and set any optional MCP provider and skill-pack settings you
   intend to use. The Mac helper **does not install or configure those
   services**, publish a matching orchestrator image, or generate targets and
   policy files. The full Mac lifecycle and recovery path still needs native
   qualification; see [Mac qualification](MAC-ARM64.md#sequence-and-evidence).

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

`launch` starts the dedicated gateway and runs `sandbox-science up` in the
foreground. Ctrl-C stops this launch and its dedicated gateway. A successful
`smoke` does **not** imply that `launch` will work with an unfinished
application config; the launcher reports missing files, images and services.
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
| Installed OpenShell version differs | The helper requires v0.1.3. Inspect the existing installation before changing it; it will not replace another version automatically. |
| `vm compute driver binary not found` | Update the checkout with `git pull --ff-only`, then run `./scripts/mac-local install` and `./scripts/mac-local smoke` again. The helper now locates the driver in the gateway's Homebrew installation and pins its directory. If `install` says the executable is missing, inspect `brew list nvidia/openshell/openshell`; the pinned installation is incomplete and needs repair before a smoke retry. Preserve the selected-volume state. |
| GHCR pull fails or returns access denied | Confirm the Keychain token is a classic PAT with `read:packages` and access to the private package; inspect `gateway.log` without sharing its credentials. |
| Gateway or Sage test fails | Read the printed gateway log path, keep the volume mounted and retry only after identifying the failure. The test cleans up its named sandbox when it can. |
| Full `launch` reports missing files | Recheck the private application TOML's paths, current image/revision, policy and targets; the babbage example is not a Mac-ready config. |

The pinned [OpenShell v0.1.3 installation guide](https://docs.nvidia.com/openshell/v0.1.3/about/installation/)
and [VM driver notes](https://github.com/NVIDIA/OpenShell/blob/v0.1.3/crates/openshell-driver-vm/README.md)
describe the external runtime used by these steps.
