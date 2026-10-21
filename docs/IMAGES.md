# Base profile images

Research sandboxes start from one of two standard base profiles (D46, D22). The orchestrator's own sandbox starts from a third image, `orchestrator`, which has the harness installed, and the MCP proxy's from a fourth, `mcp-proxy`, which has the proxy installed and a command that keeps it running. Their recipes are in `images/`: one `Containerfile` with a target per profile, a Conda environment and lockfile per profile, and scripts to lock, build and test them. The worker maps each research profile to an image (`[openshell.environments] profiles`, [worker](WORKER.md#environments)). This page is the contract the images follow, and how to build and check them.

| Profile | Contents | Licensing |
| --- | --- | --- |
| `minimal` | Ubuntu 24.04; Python 3.14 with ipykernel and jupyter_client; micromamba for runtime installs | Permissive, apart from the base distribution's runtime libraries; its AMD64 variant is currently in a private GHCR package (tier 2 in [licensing](LICENSING.md)) |
| `crypto-base` | The same, plus SageMath 10.9 with its `sagemath` kernel, fpylll, fplll, PARI, FLINT, G6K, lattice-estimator, conda-forge's C and C++ compilers, TeX Live 2023 (`latexmk`, `pdflatex`), poppler-utils, qpdf, pdf_oxide and git | Contains GPL tools. The recipe is MIT; the image is built privately and may be copied only to a verified private registry (D46) |
| `orchestrator` | Ubuntu 24.04; Python 3.14 with aiohttp, pydantic, PyYAML and the Markdown parser of living documents (markdown-it-py, mdit-py-plugins); the harness (`sandbox-science-harness`) and its contracts, built from this repository. No kernels ([below](#the-orchestrator-image)) | Permissive like `minimal`, which the build checks the same way; the harness is MIT |
| `mcp-proxy` | Ubuntu 24.04; Python 3.14 with aiohttp, pydantic and PyYAML; the MCP proxy (`sandbox-science-mcp-proxy`) with the contracts, the policy library and the event store, built from this repository; a launcher, `mcp-proxy-run`, as the sandbox's command. No kernels ([below](#the-mcp-proxy-image)) | Permissive like `minimal`, which the build checks the same way; the proxy is MIT |

## Contract

Every profile follows it; the orchestrator and MCP proxy images differ where [the orchestrator image](#the-orchestrator-image) and [the MCP proxy image](#the-mcp-proxy-image) say.

- **Base.** Ubuntu 24.04, from Docker Hub's `ubuntu:24.04`, pinned by digest.
- **Account.** The image's `sandbox` account has uid and gid 1000 and home `/sandbox`. Ubuntu's own uid 1000 account (`ubuntu`) is removed, since it would shadow it. The image runs as uid 1000 with `HOME=/sandbox`, as OpenShell's VM driver runs workloads: as the image's `sandbox` account, or uid 1000 when there is none. Nothing needs root at runtime.
- **`/sandbox`** is the home directory and the workdir, and holds the project (`/sandbox/project`). It is empty in the image, so a runtime may mount a volume over it; the build empties it and checks.
- **Interpreter.** `/opt/sandbox-science/bin/python` runs the executor, so the worker's `python` setting is the same for every profile. It has ipykernel and jupyter_client.
- **Kernels.** Every kernelspec the worker offers is in the prefix's `share/jupyter/kernels`, where that interpreter's Jupyter installation finds it: `python3`, and `sagemath` in crypto-base. The build fails if one is missing.
- **Conda prefix.** `/opt/sandbox-science`, owned by uid 1000, is micromamba's root prefix and its base environment:
  - `bin/` holds the interpreter and every program of the environment, so the executor's `PATH` finds them;
  - `share/jupyter/kernels/` the kernelspecs;
  - `pkgs/` micromamba's package cache, empty in the image. After a lockfile install micromamba keeps the extracted packages there, partly as hard links into the environment, and `micromamba clean -a` leaves them. The build removes them, which saves 96 MB in minimal and 1.1 GB in crypto-base;
  - `envs/` environments created at runtime.
- **Runtime installs** go into the prefix: `micromamba install …` and `pip install …` install into the base environment, so kernels see them at once. The prefix is therefore a declared writable volume (D48).
  - `/usr/local/bin/micromamba` sets `MAMBA_ROOT_PREFIX` and `CONDA_PREFIX` to the prefix when they are unset. OpenShell's VM driver does not apply an image's environment variables, and without them micromamba would cache packages under `/sandbox`.
  - The worker's package-client configuration points micromamba, pip and the other clients at the package cache ([package cache](PACKAGE-CACHE.md)). The image sets none of the variables it uses (`CONDARC`, `PIP_CONFIG_FILE`, `CARGO_HOME`, …) and ships no Conda configuration.
- **Environment variables**, for runtimes that apply them: `PATH` starting with `/opt/sandbox-science/bin`, `HOME=/sandbox`, `MAMBA_ROOT_PREFIX` and `CONDA_PREFIX` naming the prefix, `LANG=C.UTF-8`. Under OpenShell the executor sets `PATH` and `CONDA_PREFIX` itself.
- **Writable paths** (D48): `/sandbox`, `/opt/sandbox-science`, `/tmp` and `/dev/shm`. Everything else may be read-only.
- **No image policy.** The images carry no `/etc/openshell/policy.yaml`; the worker always supplies the policy.
- **Record.** `/usr/share/sandbox-science/image.json` names the profile, the recipe's revision, the base image, the Ubuntu snapshot, the micromamba release, the lockfile's SHA-256 and the trees built from source. It lists every Conda, pip and Ubuntu package with its version and license. The Conda lockfile is beside it. Restoring and rebuilding environments (D22, D48) can start from these.
- **Labels.**
- `org.opencontainers.image.source`: the repository, except on `crypto-base` to prevent automatic access inheritance for its private GHCR package;
  - `org.opencontainers.image.revision`: the recipe's commit, the last to change what the build reads, with `-dirty` for uncommitted changes there;
  - `org.opencontainers.image.version`: its short form, also the tag;
  - `org.opencontainers.image.licenses`: the licenses in `images/<profile>/licenses.txt`, joined into one SPDX expression;
  - `org.opencontainers.image.base.name`: the Ubuntu image;
  - `io.github.regenscheid.sandbox-science.profile`: the profile's name.

### OpenShell filesystem paths

OpenShell's Landlock baseline lets sandbox processes open only `/usr`, `/lib`, `/etc`, `/app`, `/var/log`, `/proc` and `/dev/urandom` read-only, and `/tmp`, `/dev/null` and the workdir `/sandbox` read-write. The policy compiler adds the devices research code needs ([policy](POLICY.md), "Mapping to enforcement"). Both research images need two more paths, so the OpenShell targets file must list:

```yaml
sandbox:
  filesystem:
    read_only: [/var/lib/texmf]          # TeX's formats and file databases (crypto-base)
    read_write: [/opt/sandbox-science]   # the Conda prefix
```

- `images/common/sandbox-filesystem.yml` holds this section, and the tests apply it.
- One section suits both images: OpenShell skips a path an image lacks.
- OpenShell fixes the filesystem section when it creates a sandbox, so the targets file must list these before the worker creates sandboxes from the images.
- Everything else the images use is under the baseline: micromamba in `/usr/local`, the record in `/usr/share/sandbox-science`, apt's programs in `/usr`. `/bin`, `/lib64` and `/sbin` are links into `/usr`.
- Without `/var/lib/texmf`, `pdflatex` cannot read its format file. Without `/opt/sandbox-science`, nothing in the prefix runs.

The orchestrator's sandbox gets no devices from the compiler (`compile_orchestrator()`), and needs only the prefix, read-only. The targets file's `orchestrator` entry must list it, beside the interpreter that may reach the services:

```yaml
orchestrator:
  binaries: [/opt/sandbox-science/bin/python*]
  filesystem:
    read_only: [/opt/sandbox-science]    # the harness's interpreter and packages
```

- `images/orchestrator/filesystem.yml` holds this section, and the tests apply it; `examples/openshell/targets.yaml` lists the same.
- Without it, Landlock refuses to start the harness's interpreter (`EACCES` from `execve`), as the tests show when given an empty section.

The MCP proxy's sandbox gets no devices either (`compile_mcp_proxy()`) and needs the same path. Its binary is the interpreter too: OpenShell sees the process that connects, and for a Python entry point that is the interpreter, `/opt/sandbox-science/bin/python3.14`, not the script ([lab 13](../.agents/docs/agent-reports/babbage-openshell-lab-13-2026-10-11.md)). The targets file's `mcp_proxy` entry must therefore list:

```yaml
mcp_proxy:
  binaries: [/opt/sandbox-science/bin/python*]
  filesystem:
    read_only: [/opt/sandbox-science]    # the proxy's interpreter, packages and launcher
```

- `images/mcp-proxy/filesystem.yml` holds this section, and the tests apply it; `examples/openshell/targets.yaml` lists the same, and `compile_mcp_proxy()` puts it into the proxy sandbox's policy.
- Without it, Landlock refuses to start the proxy's interpreter, as for the orchestrator.

### The orchestrator image

The `orchestrator` image runs the normal launcher's agent runtime in a dedicated OpenShell sandbox (Z2 in the [threat model](THREAT-MODEL.md)). It owns root and child model/tool loops, model adapters and queue selection. A trusted host control service keeps durable repositories and validates scoped tool/state capabilities; the GUI remains independent. The compiled policy permits only the configured worker, model, control and optional MCP service endpoints. The normal launcher has no host agent fallback. Historical [lab 12](../.agents/docs/agent-reports/babbage-openshell-lab-12-2026-10-11.md) predates this transport and does not qualify its replacement behavior; the [confinement contract](specs/orchestrator-confinement.md) defines the required disposable acceptance. The image differs from the contract above in these points:

- **Harness.** `sandbox-science-harness` and its contracts are in the prefix, installed from wheels of `packages/contracts` and `packages/orchestrator` that `build.sh` builds from the checkout (`uv build`) and passes to the build as the named context `wheels`.
  - pip installs only those two, with `--no-deps --no-index`. aiohttp, pydantic and their dependencies come from the lockfile, in the minor versions `uv.lock` pins (aiohttp 3.14, pydantic 2.14); a test keeps them in step.
  - The build fails unless `pip check` passes and `sandbox-science-harness --help` runs. The image checks also require the control and agent runtime entrypoints to accept `--settings`.
  - Building the `orchestrator` target without the `wheels` context stops with a message, rather than looking for an image named `wheels`.
- **No kernels.** It has no ipykernel, jupyter_client or kernelspecs, and nothing else from the research profiles: the orchestrator runs no research code (D11). micromamba is there, from the shared base stage.
- **Read-only prefix.** The harness installs nothing at runtime, so `/opt/sandbox-science` is read-only, and the writable paths are `/sandbox` and `/tmp`. The runtime stores replaceable scratch/configuration under `/sandbox/runtime`; the durable control store and worker credential stay on the host.
- **Running it.** OpenShell's VM driver does not apply the image's `PATH`, so run the harness by its full path, `/opt/sandbox-science/bin/sandbox-science-harness`. Its first line names the prefix's `python`, which is what the targets file's `orchestrator.binaries` admits to the worker API and the model gateway.

### The MCP proxy image

The MCP proxy runs in its own OpenShell sandbox, under the policy `compile_mcp_proxy()` gives for the project's servers ([MCP connectors](MCP-CONNECTORS.md#running-it)). The `mcp-proxy` image has it installed, and the image's command keeps it running, also across `sandbox stop` and `start`. Like the orchestrator image, it has no kernels and a read-only prefix, so the writable paths are `/sandbox` and `/tmp`. Besides:

- **Proxy.** `sandbox-science-mcp-proxy` and the repository packages it imports (contracts, policy library, event store) are in the prefix, installed from wheels of their four packages that `build.sh` builds from the checkout, as for the orchestrator image. The event store's command, `sandbox-science-events`, comes with them.
  - pip installs only those four, with `--no-deps --no-index`. aiohttp, pydantic, PyYAML and their dependencies come from the lockfile, in the minor versions `uv.lock` pins (aiohttp 3.14, pydantic 2.14, PyYAML 6.0); tests keep them in step and check that the lockfile has every requirement of the four packages.
  - The build fails unless `pip check` passes and `sandbox-science-mcp-proxy --help` runs.
- **Launcher.** `/opt/sandbox-science/bin/mcp-proxy-run`, a POSIX shell script, is the sandbox's command, and the image's `CMD` for runtimes that use it:
  - it waits until the settings file exists, `/sandbox/mcp-proxy/proxy.toml` or the path given as its argument, looking every second;
  - it then runs `sandbox-science-mcp-proxy serve --settings` with that file, by its full path;
  - whenever the proxy exits, it starts it again after a pause: 1 s, doubling to at most 10 s while the proxy keeps exiting within a minute of its start;
  - the proxy gets the launcher's environment, with the credential placeholders OpenShell sets;
  - both log to standard error, the launcher with a timestamp on each line;
  - SIGTERM, SIGINT or SIGHUP stops the proxy, then the launcher.
- **Its directory.** `/sandbox/mcp-proxy/` holds the settings, with the pool ceiling and the project policies beside them, and `state/` (`state_dir = "state"` in the settings): the policy store and the event store. The VM driver keeps a sandbox's disk across `sandbox stop` and `start` ([lab 11](../.agents/docs/agent-reports/babbage-openshell-lab-11-2026-10-11.md)), so they survive a restart.
- **Binary.** The process that connects to the servers is the prefix's interpreter, `/opt/sandbox-science/bin/python3.14`, which the targets file's `mcp_proxy.binaries` names ([above](#openshell-filesystem-paths)). The launcher opens no connections.
- **Running it.** The operator creates the sandbox from the image with the launcher as its command (`openshell sandbox create … -- /opt/sandbox-science/bin/mcp-proxy-run`), uploads the pool ceiling, the project policies and then the settings into `/sandbox/mcp-proxy/`, binds each project to its policy with `sandbox-science-mcp-proxy bind` through `openshell sandbox exec`, and exposes the proxy on the pool host with `openshell forward service`. [MCP connectors](MCP-CONNECTORS.md#running-it) has the commands. As for the harness, run every command in the sandbox by its full path.

## Pinned sources

Every download is pinned and checked:

| Source | Pin | Check | License |
| --- | --- | --- | --- |
| Ubuntu 24.04 | `docker.io/library/ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55` (24.04.5, 2026-09-17) | Image digest | Ubuntu's packages, each under its own license |
| Ubuntu packages | The archive as it was at snapshot `20261010T000000Z` ([snapshot.ubuntu.com](https://snapshot.ubuntu.com)); the record lists each version | apt's signed indexes | Each package's own (`/usr/share/doc/*/copyright`) |
| micromamba | Release `2.9.0-0` from `mamba-org/micromamba-releases`, a static binary | Its published SHA-256 | BSD-3-Clause, with bundled libraries under MIT, OpenSSL and similar terms |
| Conda packages | `images/<profile>/conda-lock.yml`: every package from conda-forge for linux-64 and linux-aarch64 | Each package's SHA-256, which micromamba checks | Each package's own; listed in the record |
| G6K | `fplll/g6k` at `c71e084dd003e1ffbfe8c91a7211b0ce9da22245` (2025-11-09, version 0.1.2) | Git commit hash | GPL-2.0-or-later |
| lattice-estimator | `malb/lattice-estimator` at `53da5982597709ba0fdf94ea37a84d822310fd84` (2026-08-19) | Git commit hash | LGPL-3.0-or-later |
| pdf_oxide | 0.3.78 wheels from PyPI (`images/crypto-base/requirements.txt`) | pip `--require-hashes` | MIT OR Apache-2.0 |
| The harness (orchestrator) | `packages/contracts` and `packages/orchestrator` from this checkout, built into wheels by `build.sh` | Not downloaded: the image's tag and record name the commit, `-dirty` when the sources have uncommitted changes | MIT |
| The MCP proxy (mcp-proxy) | `packages/contracts`, `packages/policy`, `packages/event-store` and `packages/mcp-proxy` from this checkout, built into wheels by `build.sh` | As for the harness | MIT |

`ca-certificates` for reaching the snapshot service over TLS comes from Ubuntu's current archive in a throwaway build stage; the images get it from the snapshot like every other Ubuntu package.

The services' wheels are built by `uv build` with the packages' build backend, hatchling, which uv takes from PyPI (or its cache) as for any build of the repository's packages. It is not pinned, but it only assembles the packages' own files and metadata into the wheels.

Versions in the orchestrator image: Python 3.14.8, aiohttp 3.14.5, pydantic 2.14.0 with pydantic-core 2.50.0, PyYAML 6.0.3 with LibYAML (`yaml` 0.2.5), and the Markdown parser, markdown-it-py 4.2.0 with mdurl 0.1.2 and mdit-py-plugins 0.6.1. These are the versions in `uv.lock`, but for aiohttp, which `uv.lock` has at 3.14.4 (only the minor version must match). Of their dependencies only multidict differs: conda-forge's 6.7.1, against 6.9.1 in `uv.lock`. conda-forge builds multidict and frozenlist as pure Python packages, without the C extensions their PyPI wheels have.

The mcp-proxy image's lockfile has 41 packages: the orchestrator image's 44 without the Markdown parser's three, with aiohttp at 3.14.4, as in `uv.lock`.

Notable versions in crypto-base: Python 3.14.8, SageMath 10.9, PARI 2.17.3, fpylll 0.6.4, fplll 5.5.0, FLINT 3.3.1, Singular 4.4.1, GAP 4.15.1, Maxima 5.49.0, GCC 15.3, Cython 3.3.0, TeX Live 2023 (Ubuntu `2023.20240207-1`), poppler-utils 24.02.0, qpdf 11.9.0. The `sage` package brings R and OpenJDK too.

TeX comes from Ubuntu (`images/crypto-base/apt-packages.txt`): the LaTeX base, recommended and extra collections, the science, pictures and publishers collections (so llncs, cryptocode, algorithm2e, algorithmicx, siunitx, thmtools, tikz-cd), the recommended fonts and cm-super, so that T1-encoded documents get Type 1 fonts rather than bitmaps. Ubuntu's TeX Live 2023 lacks the IACR classes (iacrtrans, iacrcc); a project can keep them in its own directory.

G6K has no conda-forge package. The build compiles it with the environment's compilers. Its configure script defaults to `-march=native`, which would tie the image to the build host's CPU, so the build disables that and targets a named CPU level instead: `x86-64-v3` (AVX2) on amd64 and `armv8.2-a` on arm64, set by the build arguments `G6K_MARCH_AMD64` and `G6K_MARCH_ARM64`. An amd64 host without AVX2 needs an image built with `x86-64-v2`. G6K's own optimisation flags are kept, the maximum sieving dimension is 128, and the record notes the level. The compilers stay in the image, since research code builds C and C++ too.

## Licensing

- **minimal.** The build checks every Conda package's license. Anything that is not permissive fails the build, except the runtime libraries that conda-forge's Python and ZeroMQ need, and the Ubuntu packages:
  - the GCC runtime (`libgcc`, `libgomp`, `libstdcxx`): GPL-3.0 with the GCC Runtime Library Exception, which leaves linked programs under any license;
  - `readline` (GPL-3.0-only), which only Python's optional `readline` module links;
  - `ld_impl` (GNU ld, GPL-3.0-only), a program that nothing links;
  - `keyutils` (LGPL-2.1-or-later), a library of Kerberos, which ZeroMQ links.

  Tier 2 admits the system libraries the base distribution requires ([licensing](LICENSING.md)); these are them.
- **crypto-base** contains GPL tools (SageMath, PARI, fpylll, G6K, GCC, …). The recipe and lockfiles are MIT; the image stays on the machines that build it and in a private registry. `build.sh` refuses to push.
  Its OCI metadata deliberately omits `org.opencontainers.image.source` so a new private GHCR
  package does not automatically inherit read permissions from this public source repository.
  The image's provenance report still records the source URL and revision. Verify package
  visibility and an unauthenticated pull denial after its first architecture upload and
  before adding further manifests or the shared index.
- **orchestrator** is checked like minimal, including the harness's two packages, which pip installs. Its only packages that are not permissive are the same runtime libraries of conda-forge's Python: the GCC runtime, `readline` and `ld_impl` (no `keyutils`, since it has no ZeroMQ). The harness's contracts are MIT and its orchestrator package `MIT AND Apache-2.0`, as their `pyproject.toml` declare them and the `record` check expects, and aiohttp, pydantic, the Markdown parser (markdown-it-py, mdit-py-plugins, mdurl), PyYAML and their dependencies are under Apache-2.0, MIT, BSD-3-Clause and PSF-2.0 (tier 1 in [licensing](LICENSING.md)).
- **mcp-proxy** is checked the same way, including its four packages from this repository, all MIT. Its packages are the orchestrator image's but for the Markdown parser, all MIT, so its licenses are the orchestrator image's: the same runtime libraries are its only packages that are not permissive.
- **The license label.** `images/<profile>/licenses.txt` lists the license expressions in the image: its Conda packages, the Python packages pip installed, the trees built from source, and `LicenseRef-Ubuntu-archive` for the Ubuntu packages. `build.sh` joins them into the `org.opencontainers.image.licenses` label. When a new lockfile changes them, the build fails and prints the new lines for the file. License names that are not SPDX identifiers become `LicenseRef-…` values.

## Building

```bash
images/build.sh minimal
images/build.sh crypto-base
images/build.sh orchestrator
images/build.sh mcp-proxy
```

- **Result.** The image is loaded into the local image store as `sandbox-science/<profile>:<short SHA>`, naming the last commit that changed what the profile's build reads, with `-dirty` for uncommitted changes there (`images/revision.sh`):
  - every profile: `Containerfile`, `.dockerignore` and `common/`, and the profile's directory, such as `minimal/`;
  - orchestrator: also `pyproject.toml` and `src/` of `packages/contracts` and `packages/orchestrator`, which its wheels are built from;
  - mcp-proxy: the same of `packages/contracts`, `packages/policy`, `packages/event-store` and `packages/mcp-proxy`.

  `recipe_packages` in `revision.sh` lists each profile's packages, for the tag and for `build.sh`. Changes to the scripts, the tests, another profile or anything else leave the tag alone. The script prints the tag, the image digest, its size and the build time. It never pushes.
- **orchestrator and mcp-proxy** need `uv`: `build.sh` first builds their wheels into a temporary directory, which it removes afterwards.
- **Options.** Further arguments go to `docker buildx build`, such as `--no-cache` or `--build-arg G6K_MARCH_AMD64=x86-64-v2`. `PLATFORM` selects `linux/amd64` or `linux/arm64` (by default the host architecture), and `BUILDER` selects a buildx builder. The Mac builds Linux ARM64 images, not native macOS images.
- **Rebuilds** install the same packages; they are not bit-for-bit identical.

Sizes, as built on babbage (Ryzen AI Max+ 395, 32 threads, Docker 29.1 with BuildKit's `docker` driver):

| Image | Compressed | Unpacked |
| --- | --- | --- |
| `minimal` | 135 MB | 374 MB |
| `crypto-base` | 2.34 GB | 6.2 GB |
| `orchestrator` | 127 MB | 364 MB |
| `mcp-proxy` | 128 MB | 366 MB |

Build times from empty caches depend mostly on snapshot.ubuntu.com, which served several MB/s for the first builds and 0.25 MB/s half an hour later:

| Step | Time |
| --- | --- |
| Ubuntu's package indexes and `ca-certificates` (every image) | 14 s to 200 s |
| minimal's 62 Conda packages (71 MB to download) | 3 s |
| crypto-base's 125 Ubuntu packages (264 MB, mostly TeX) | 2 min to 19 min |
| crypto-base's 393 Conda packages (1.44 GB) | 23 s |
| orchestrator's or mcp-proxy's Conda packages (about 63 MB to download) | 2 s |
| G6K | 30 s |
| Writing crypto-base into the local image store | 2.5 min |
| **minimal in all** | 2.4 min measured at 0.25 MB/s; about 30 s at the faster rate |
| **crypto-base in all** | 25 min at 0.25 MB/s (22.6 min measured with the shared steps cached); about 6 min at the faster rate |
| **orchestrator in all** | 71 s with `--no-cache`, 54 s of it for Ubuntu's indexes and `ca-certificates`; 8 s with the shared base stage cached, including the two wheels |
| **mcp-proxy in all** | 11 s with the shared base stage cached, including the four wheels; its Ubuntu steps are the orchestrator's |

In the orchestrator image the Conda prefix takes 239 MB, mostly Python itself; the harness and its contracts take 0.3 MB. In the mcp-proxy image it takes 241 MB; the proxy and the three packages it imports take 1.2 MB, PyYAML and LibYAML 1.1 MB. In crypto-base the Conda prefix takes 5.2 GB, of which the compilers take 0.76 GB, GAP's data 0.39 GB and OpenJDK 0.3 GB (for Jmol, which the `sage` package brings); TeX takes 0.57 GB.

## Lockfiles

`images/lock.sh [profile…]` solves `images/<profile>/environment.yml` again into `conda-lock.yml`, for linux-64 and linux-aarch64; without arguments, for every profile.

- It runs conda-lock 4.0.2 from a throwaway environment (`uvx`), or the executable `CONDA_LOCK` names.
- It solves with the micromamba release the Containerfile pins, checked against the same SHA-256.
- The solve assumes Ubuntu 24.04's glibc (2.39) and generic CPUs (`images/common/virtual-packages.yml`).

After locking, review the diff, build, and update `licenses.txt` if the build asks for it.

## Tests

`images/test.sh <profile> [image]` runs `images/checks/check_image.py` in the image twice. Each time it runs as uid 1000, with no capabilities, `no-new-privileges`, a read-only root filesystem, tmpfs at `/sandbox` and `/tmp`, and for the research profiles a volume for the Conda prefix:

- **container**: as a container driver would run the image;
- **landlock**: the same, under a Landlock ruleset like OpenShell's: its baseline, the devices the compiler adds (none for `orchestrator`), and the paths above. A check confirms that the ruleset is in effect.

In the research profiles the checks start the worker's executor as the OpenShell backend does (`python -I /sandbox/.sandbox-science/executor.py --stdio --root /sandbox/project`), so kernels start through jupyter_client with the executor's environment. As the worker's first sync of an environment does, the check opens the executor's package admission after `hello` (`package_gate`, generation 0, `ready`): the executor holds it from its start and admits no shell or cell until then. Each shell and cell carries the check's own time limit:

| Check | What it shows |
| --- | --- |
| `identity`, `filesystem`, `record` | uid 1000 is `sandbox` with home `/sandbox`; only the declared paths are writable; the image record names the profile, and there is no OpenShell image policy |
| `executor` | The executor's `hello` lists `python` and the `jobs` feature, and in crypto-base `sage` and the `pdf` feature too |
| `python-kernel`, `tools-on-path`, `multiprocessing` | A Python cell runs with the prefix's interpreter; the environment's programs are on `PATH`, and its `python`, the package-command wrappers' stand-in first on `PATH`, runs the prefix's; `multiprocessing` locks, pools and process pools work |
| `sage-kernel`, `sage-interfaces` | A Sage cell computes in GF(17) and preparses `2^10`; gp and Singular start through pseudo-terminals, and libgap works |
| `fpylll`, `g6k`, `lattice-estimator` | LLL on a 60-dimensional q-ary lattice; a G6K sieve in dimension 50; the primal uSVP estimate for Kyber512 |
| `latexmk`, `pdftotext`, `pdf-oxide` | `latexmk -pdf` builds an llncs document with T1 fonts, amsmath, cryptocode, algorithmicx, TikZ and cleveref, with no bitmap fonts in the PDF; `pdftotext`, `qpdf --check` and pdf_oxide read the result |
| `pdf-tools` | The worker's PDF operations ([PDF sources](specs/pdf-sources.md)) through the executor and its helper, copied beside it: page facts, line anchors, search and a page render, with the image's pdf_oxide |
| `runtime-install` | micromamba and pip install a package into the prefix (needs conda-forge and PyPI; `NETWORK=0` skips it) |

In the orchestrator image they run the harness by its full path, with only `HOME` and a system `PATH` set, as an OpenShell sandbox runs it:

| Check | What it shows |
| --- | --- |
| `identity`, `filesystem`, `record` | As above, except that the prefix is readable but not writable, and the record lists the harness's two packages as MIT |
| `harness-cli` | `sandbox-science-harness` is in the prefix's `bin/`, starts the prefix's `python`, and prints its help and that of `run`, `project` and `session` |
| `harness-imports` | The harness, its contracts, aiohttp, pydantic and pydantic-core import from the prefix; the versions |
| `no-kernels` | No kernelspecs, and no ipykernel, jupyter_client or pyzmq |
| `harness-state` | `project create` and `project show` keep a project record under `/sandbox` (`--state-dir`) |
| `harness-run` | `run` with a scripted model, against a stub of the worker API on loopback: the stub speaks the worker protocol of the image's contracts package (its `X-Worker-Protocol` header), the harness sends its token, reads the capabilities and prints the model's answer |

In the mcp-proxy image they start the launcher by its full path, with only `HOME`, a system `PATH` and a credential placeholder set, as the proxy's sandbox runs its command. The proxy's calls never leave the container: no call gets past the check for a session.

| Check | What it shows |
| --- | --- |
| `identity`, `filesystem`, `record`, `no-kernels` | As for the orchestrator image; the record lists the proxy's four packages as MIT |
| `proxy-cli` | `sandbox-science-mcp-proxy` is in the prefix's `bin/`, starts the prefix's `python`, and prints its help and that of `serve`, `bind`, `pending`, `approve` and `deny`; `sandbox-science-events` prints its help |
| `proxy-imports` | The proxy, the policy library, the event store, the contracts, aiohttp, pydantic, pydantic-core and PyYAML import from the prefix; the versions. The default TLS context trusts the prefix's CA bundle (in a sandbox OpenShell sets its own) |
| `launcher` | `mcp-proxy-run` is executable, waits for `/sandbox/mcp-proxy/proxy.toml`, and starts nothing while it is missing |
| `proxy-serve` | Once the ceiling and the settings are written, the launcher starts the proxy. Its process is `/opt/sandbox-science/bin/python3.14`, which `python*` matches, with the placeholder in its environment. On loopback it answers 401 without a token, and with one `project_unbound`, then `session_required` after `bind`: a binding applies without a restart. `pending` reads its event store |
| `proxy-restart` | After the proxy is killed (SIGKILL), the launcher logs the exit and starts it again, with the binding intact. The event store holds one record for each authenticated call, and its chain verifies after the crash. SIGTERM stops the proxy, then the launcher, which exits with 0 |

`images/tests/test_images.py` runs the same checks from pytest, one test per check and mode. It runs only when `SS_IMAGE_TESTS` names images Docker can see, such as `SS_IMAGE_TESTS="sandbox-science/minimal:abc1234 sandbox-science/crypto-base:abc1234" uv run pytest images`; CI skips it. `images/tests/test_recipes.py` runs in CI. It checks:

- the pins and the lockfiles, and that the services' lockfiles have every requirement of the packages they install, in the minor versions `uv.lock` pins, and the same Python;
- that each profile's tag covers every file its build reads, and that the services' images install every repository package theirs depend on;
- the declared filesystem sections against the compiler's model and the example targets file;
- that the launcher is the mcp-proxy image's command, with the paths the checks use;
- the license helpers.

What the checks show about the paths:

- Under the research ruleset without `/var/lib/texmf`, pdflatex cannot find its format file, so that path stays in the section. Nothing beyond OpenShell's baseline, the compiler's devices and the two declared paths is needed: Sage's pseudo-terminal interfaces, `multiprocessing`, runtime installs and the PDF tools all work.
- Under the orchestrator's or the proxy's ruleset without `/opt/sandbox-science`, Landlock refuses to start the prefix's interpreter. With it, the harness, the launcher and the proxy need nothing else beyond OpenShell's baseline: no devices, and no `/dev/shm`.
- The proxy answers within about 2 s of the settings being written (up to 1 s of it the launcher's wait between looks), and about 1.5 s after it is killed (1 s of it the first pause). As a container's own command, the launcher runs as PID 1, waits for the settings, and `docker stop` ends it at once.

## arm64

Native Linux ARM64 [qualification runs](https://github.com/regenscheid/sandbox-crypto/actions/runs/39331529252) built and tested all four profiles, including `crypto-base` with Sage and G6K, in container and Landlock modes with network disabled. This validates those images on that runner, not the Mac OpenShell runtime. The recipe has:

- the micromamba release and its SHA-256 for linux-aarch64;
- lockfiles that cover linux-aarch64: the same packages and versions as linux-64, apart from the compilers' architecture-named packages and two minor version differences (charset-normalizer, libglib) where a build was missing;
- pdf_oxide's aarch64 wheel hash;
- a G6K build for the target, compiled natively per architecture (D63) and aimed at `armv8.2-a`, the Raspberry Pi 5's Cortex-A76. Pinned G6K includes the x86-only `immintrin.h` unconditionally even though it has a generic SIMD path, and gives its generic vectors explicit alignment that GCC 15's AArch64 argument ABI rejects. The ARM64 build applies the exact-match `patch-g6k-arm64.py` adjustment before configure; the AMD64 source is unchanged.

All four profiles now have published two-platform tags. `build.sh` loads one
platform into the local store for development and never pushes it.

The selected destination is GHCR under this repository's namespace:
`ghcr.io/regenscheid/sandbox-crypto/<profile>`. The manual image workflow's
`publish` mode uses native Linux AMD64 and ARM64 runners for every profile. Each
runner creates a `docker-container` Buildx builder and builds directly to a
run-specific GHCR architecture tag using BuildKit's registry exporter. It pulls
the resulting immutable digest and checks that exact image in container and
Landlock modes. The merge job starts only after all eight
build-and-check jobs pass. It resolves the run-specific tags to immutable digests,
checks platform, profile and recipe revision, and creates one two-platform tag per
profile in GitHub Actions. The manual `publish_scope=minimal` mode runs only the
two native minimal builds and their merge as a registry smoke test. Selecting
all profiles without confirming `crypto_base=true` fails input validation before
any publication job starts. Pull-request jobs remain read-only and cannot publish.

New GHCR packages default to private. Before pushing or merging `crypto-base`,
the workflow checks that the package's GitHub visibility is private. An architecture
digest may name an OCI index containing one image plus a provenance attestation.
`publish_index.py` resolves only its image leaf, accepts attestations bound to that
leaf, and verifies that the shared tag contains exactly the AMD64 and ARM64 images.
For manual recovery, with registry access configured, inspect a proposed index from
already-pushed digests:

```bash
python3 images/publish_index.py crypto-base ghcr.io/regenscheid/sandbox-crypto/crypto-base:REVISION \
  ghcr.io/regenscheid/sandbox-crypto/crypto-base@sha256:AMD64_DIGEST \
  ghcr.io/regenscheid/sandbox-crypto/crypto-base@sha256:ARM64_DIGEST \
  --private-registry-confirmed --dry-run
```

Run the same command without `--dry-run` to create the shared tag. The script
checks both source architectures, the profile and matching recipe revision labels,
then verifies the published index contains exactly those two digests. It refuses
`crypto-base` without an explicit confirmation that the destination package is
private. The operator must verify registry access before using this manual command;
Docker cannot infer privacy from a name. The prior `--load` then `docker push`
ARM64 path uploaded its layers but received `unknown blob` or HTTP 500 while
finalizing the manifest, even for an isolated minimal-image retry. The first
Actions publication attempt also returned HTTP 500 on both native architectures:
the default Buildx `docker` driver still used Docker's daemon push path. The
workflow now requires the `docker-container` driver for publication. The
[minimal smoke run](https://github.com/regenscheid/sandbox-crypto/actions/runs/39348114450)
verified its direct registry export; the [full publication run](https://github.com/regenscheid/sandbox-crypto/actions/runs/39348347538)
passed all eight pulled-digest container/Landlock checks and all four merges.
Each shared OCI index was independently inspected and contains exactly
`linux/amd64` and `linux/arm64`. All four GHCR packages are currently private;
`crypto-base` remains private as required by D46.

The `VM bootstrap mirror` workflow copies NVIDIA's
`nvcr.io/nvidia/base/ubuntu:24.04` index, pinned by digest, to
`ghcr.io/regenscheid/sandbox-crypto/vm-bootstrap:ubuntu-24.04` with
`skopeo copy --all --preserve-digests`, then checks that GHCR serves the same
digest and that the package is private. It runs when its workflow file changes on
`dev` or `main`, or by manual dispatch. OpenShell v0.1.3's VM driver sends one
registry credential to both the bootstrap and sandbox pulls, and nvcr.io rejects
the GitHub token needed for `crypto-base`, so the Mac helper boots from this
mirror. To move to a newer NVIDIA image, change the digest in the workflow and in
`DEFAULT_BOOTSTRAP_IMAGE` in `scripts/mac_local.py`; a test keeps them equal.

## In OpenShell

Every image has run in OpenShell VM sandboxes on babbage, under the compiled policies:
- crypto-base for the M1 exit run ([lab 9](../.agents/docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)), so the VM driver handles its root filesystem;
- minimal, where runtime installs into the prefix work ([lab 11](../.agents/docs/agent-reports/babbage-openshell-lab-11-2026-10-11.md));
- both research images under the worker's external conformance suite, with Python kernels and the worker's file and publication semantics ([lab 23](../.agents/docs/agent-reports/babbage-openshell-lab-23-2026-10-12.md));
- the orchestrator image, running the harness ([lab 12](../.agents/docs/agent-reports/babbage-openshell-lab-12-2026-10-11.md));
- the mcp-proxy image, with its launcher as the sandbox's command ([lab 15](../.agents/docs/agent-reports/babbage-openshell-lab-15-2026-10-11.md)).

## Not yet done

- **Mac OpenShell acceptance.** The shared tags are published and verified on native Linux runners; a real Mac MicroVM lifecycle and Resume test remains open.
- **An SBOM** in a standard format (SPDX or CycloneDX) before a general release. The published `minimal` tag does not yet include one; the image record lists every package and license, but in its own format.
- **Restore and rebuild.** The record and lockfile are there; capturing an environment's changes and rebuilding it from the lockfile belong to the environment manager (W3, D48).

Research images also contain `/usr/share/sandbox-science/binding-inventory.json`, a
canonical static inventory generated after the final package installation by the
same scanner shipped to executors. Its version, extractor hash, package ownership,
source-built revisions, restriction provenance and coverage notes are recorded in
its content digest. No installed tool or skill helper is executed to discover
variable names. Runtime provisioning requires a new observation; the base image's
inventory is not treated as current after package changes. See the
[binding inventory contract](specs/skill-binding-inventory.md).
