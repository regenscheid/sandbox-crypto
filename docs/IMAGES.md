# Base profile images

Research sandboxes start from one of two standard base profiles (D46, D22). The orchestrator's own sandbox starts from a third image, `orchestrator`, which has the harness installed. Their recipes are in `images/`: one `Containerfile` with a target per profile, a Conda environment and lockfile per profile, and scripts to lock, build and test them. The worker maps each research profile to an image (`[openshell.environments] profiles`, [worker](WORKER.md#environments)). This page is the contract the images follow, and how to build and check them.

| Profile | Contents | Licensing |
| --- | --- | --- |
| `minimal` | Ubuntu 24.04; Python 3.14 with ipykernel and jupyter_client; micromamba for runtime installs | Permissive, apart from the base distribution's runtime libraries; the project could publish it (tier 2 in [licensing](LICENSING.md)). It is not published yet |
| `crypto-base` | The same, plus SageMath 10.9 with its `sagemath` kernel, fpylll, fplll, PARI, FLINT, G6K, lattice-estimator, conda-forge's C and C++ compilers, TeX Live 2023 (`latexmk`, `pdflatex`), poppler-utils, qpdf, pdf_oxide and git | Contains GPL tools. The recipe is MIT; the image is built privately and never published (D46) |
| `orchestrator` | Ubuntu 24.04; Python 3.14 with aiohttp and pydantic; the harness (`sandbox-science-harness`) and its contracts, built from this repository. No kernels ([below](#the-orchestrator-image)) | Permissive like `minimal`, which the build checks the same way; the harness is MIT |

## Contract

Every profile follows it; the orchestrator image differs where [the orchestrator image](#the-orchestrator-image) says.

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
  - `org.opencontainers.image.source`: the repository;
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

### The orchestrator image

The harness runs in its own OpenShell sandbox (Z2 in the [threat model](THREAT-MODEL.md)), under the compiled orchestrator policy, which admits only the worker API and the model gateway ([policy](POLICY.md), "Mapping to enforcement"). In labs [7](../.agents/docs/agent-reports/babbage-openshell-lab-7-2026-10-11.md) and [9](../.agents/docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md) it was installed there from a wheelhouse uploaded by hand; the `orchestrator` image has it installed. In [lab 12](../.agents/docs/agent-reports/babbage-openshell-lab-12-2026-10-11.md) the harness ran from the image in its own sandbox, reaching only the worker API, and had the worker create a project's environments. The image differs from the contract above in these points:

- **Harness.** `sandbox-science-harness` and its contracts are in the prefix, installed from wheels of `packages/contracts` and `packages/orchestrator` that `build.sh` builds from the checkout (`uv build`) and passes to the build as the named context `wheels`.
  - pip installs only those two, with `--no-deps --no-index`. aiohttp, pydantic and their dependencies come from the lockfile, in the minor versions `uv.lock` pins (aiohttp 3.14, pydantic 2.14); a test keeps them in step.
  - The build fails unless `pip check` passes and `sandbox-science-harness --help` runs.
  - Building the `orchestrator` target without the `wheels` context stops with a message, rather than looking for an image named `wheels`.
- **No kernels.** It has no ipykernel, jupyter_client or kernelspecs, and nothing else from the research profiles: the orchestrator runs no research code (D11). micromamba is there, from the shared base stage.
- **Read-only prefix.** The harness installs nothing at runtime, so `/opt/sandbox-science` is read-only, and the writable paths are `/sandbox` and `/tmp`. The harness's state (`--state-dir`) and the worker's token file belong under `/sandbox`.
- **Running it.** OpenShell's VM driver does not apply the image's `PATH`, so run the harness by its full path, `/opt/sandbox-science/bin/sandbox-science-harness`. Its first line names the prefix's `python`, which is what the targets file's `orchestrator.binaries` admits to the worker API and the model gateway.

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

`ca-certificates` for reaching the snapshot service over TLS comes from Ubuntu's current archive in a throwaway build stage; the images get it from the snapshot like every other Ubuntu package.

The orchestrator's wheels are built by `uv build` with the packages' build backend, hatchling, which uv takes from PyPI (or its cache) as for any build of the repository's packages. It is not pinned, but it only assembles the packages' own files and metadata into the wheels.

Versions in the orchestrator image: Python 3.14.8, aiohttp 3.14.4, pydantic 2.14.0 with pydantic-core 2.50.0, as in `uv.lock`. Of their dependencies only multidict differs: the solve chose conda-forge's 6.7.1, against 6.9.1 in `uv.lock`. conda-forge builds multidict and frozenlist as pure Python packages, without the C extensions their PyPI wheels have.

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
- **orchestrator** is checked like minimal, including the harness's two packages, which pip installs. Its only packages that are not permissive are the same runtime libraries of conda-forge's Python: the GCC runtime, `readline` and `ld_impl` (no `keyutils`, since it has no ZeroMQ). The harness is MIT, and aiohttp, pydantic and their dependencies are under Apache-2.0, MIT, BSD-3-Clause and PSF-2.0 (tier 1 in [licensing](LICENSING.md)).
- **The license label.** `images/<profile>/licenses.txt` lists the license expressions in the image: its Conda packages, the Python packages pip installed, the trees built from source, and `LicenseRef-Ubuntu-archive` for the Ubuntu packages. `build.sh` joins them into the `org.opencontainers.image.licenses` label. When a new lockfile changes them, the build fails and prints the new lines for the file. License names that are not SPDX identifiers become `LicenseRef-…` values.

## Building

```bash
images/build.sh minimal
images/build.sh crypto-base
images/build.sh orchestrator
```

- **Result.** The image is loaded into the local image store as `sandbox-science/<profile>:<short SHA>`, naming the last commit that changed what the profile's build reads, with `-dirty` for uncommitted changes there (`images/revision.sh`):
  - every profile: `Containerfile`, `.dockerignore` and `common/`, and the profile's directory, such as `minimal/`;
  - orchestrator: also `pyproject.toml` and `src/` of `packages/contracts` and `packages/orchestrator`, which its wheels are built from.

  Changes to the scripts, the tests, another profile or anything else leave the tag alone. The script prints the tag, the image digest, its size and the build time. It never pushes.
- **orchestrator** needs `uv`: `build.sh` first builds the two wheels into a temporary directory, which it removes afterwards.
- **Options.** Further arguments go to `docker buildx build`, such as `--no-cache` or `--build-arg G6K_MARCH_AMD64=x86-64-v2`. `PLATFORM` sets the platform (default `linux/amd64`) and `BUILDER` a buildx builder.
- **Rebuilds** install the same packages; they are not bit-for-bit identical.

Built on babbage (Ryzen AI Max+ 395, 32 threads, Docker 29.1 with BuildKit's `docker` driver) on 2026-10-11, each from the recipe revision its tag names; all three are in babbage's local image store:

| Image | Digest | Compressed | Unpacked |
| --- | --- | --- | --- |
| `sandbox-science/minimal:02f85b3` | `sha256:88fab78766c9828abed9ea0198edae6513955fd22287f95819685ad5291eb9c6` | 135 MB | 374 MB |
| `sandbox-science/crypto-base:02f85b3` | `sha256:cb12bbbe588f20e555d39385ffd56f7b45312fdd69b3bd8030278db555d0df4b` | 2.34 GB | 6.2 GB |
| `sandbox-science/orchestrator:174e72e` | `sha256:f82026af2e22e20b6245475ee72e04cda134bccc7bebdf96e1d083987c8342e9` | 127 MB | 364 MB |

Build times from empty caches depend mostly on snapshot.ubuntu.com, which served several MB/s for the first builds and 0.25 MB/s half an hour later:

| Step | Time |
| --- | --- |
| Ubuntu's package indexes and `ca-certificates` (every image) | 14 s to 200 s |
| minimal's 62 Conda packages (71 MB to download) | 3 s |
| crypto-base's 125 Ubuntu packages (264 MB, mostly TeX) | 2 min to 19 min |
| crypto-base's 393 Conda packages (1.44 GB) | 23 s |
| orchestrator's 39 Conda packages (about 63 MB to download) | 2 s |
| G6K | 30 s |
| Writing crypto-base into the local image store | 2.5 min |
| **minimal in all** | 2.4 min measured at 0.25 MB/s; about 30 s at the faster rate |
| **crypto-base in all** | 25 min at 0.25 MB/s (22.6 min measured with the shared steps cached); about 6 min at the faster rate |
| **orchestrator in all** | 71 s with `--no-cache`, 54 s of it for Ubuntu's indexes and `ca-certificates`; 8 s with the shared base stage cached, including the two wheels |

In the orchestrator image the Conda prefix takes 239 MB, mostly Python itself; the harness and its contracts take 0.3 MB. In crypto-base the Conda prefix takes 5.2 GB, of which the compilers take 0.76 GB, GAP's data 0.39 GB and OpenJDK 0.3 GB (for Jmol, which the `sage` package brings); TeX takes 0.57 GB.

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

In the research profiles the checks start the worker's executor as the OpenShell backend does (`python -I /sandbox/.sandbox-science/executor.py --stdio --root /sandbox/project`), so kernels start through jupyter_client with the executor's environment:

| Check | What it shows |
| --- | --- |
| `identity`, `filesystem`, `record` | uid 1000 is `sandbox` with home `/sandbox`; only the declared paths are writable; the image record names the profile, and there is no OpenShell image policy |
| `executor` | The executor's `hello` lists `python`, and in crypto-base `sage` and the `pdf` feature |
| `python-kernel`, `tools-on-path`, `multiprocessing` | A Python cell runs with the prefix's interpreter; the environment's programs are on `PATH`; `multiprocessing` locks, pools and process pools work |
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
| `harness-run` | `run` with a scripted model, against a stub of the worker API on loopback: the harness sends its token, reads the capabilities and prints the model's answer |

`images/tests/test_images.py` runs the same checks from pytest, one test per check and mode. It runs only when `SS_IMAGE_TESTS` names images Docker can see, such as `SS_IMAGE_TESTS="sandbox-science/minimal:abc1234 sandbox-science/crypto-base:abc1234" uv run pytest images`; CI skips it. `images/tests/test_recipes.py` runs in CI. It checks:

- the pins and the lockfiles, and that the orchestrator's has the minor versions of aiohttp and pydantic that `uv.lock` pins;
- that each profile's tag covers every file its build reads;
- the declared filesystem sections against the compiler's model and the example targets file;
- the license helpers.

Results on babbage, 2026-10-11, for the images above:

- **minimal** passed all 17 checks (8 as a container, 9 under the Landlock ruleset) in 12 s.
- **crypto-base** passed all 35 (17 and 18) in 45 s. The first Sage cell, with the kernel's start, took 2.3 s; the primal uSVP estimate for Kyber512 came out at 2^143.8, as lattice-estimator's documentation gives.
- **pytest** ran the same as 52 tests in 56 s.
- **orchestrator** (`174e72e`) passed all 17 checks (8 as a container, 9 under the Landlock ruleset) in 5 s, with and without a network (`NETWORK=0`). `run` against the stub worker took 0.6 s, most of it Python's start and the imports. pytest ran them as 17 tests, beside the 46 recipe tests, in 6 s.
- **The research images with the profile-aware scripts.** `test.sh` and the checks of `174e72e` passed all 17 checks on `minimal:02f85b3` and all 35 on `crypto-base:02f85b3`.
- **Under the ruleset without `/var/lib/texmf`**, pdflatex could not find its format file, so that path stays in the section. Nothing beyond OpenShell's baseline, the compiler's devices and the two declared paths was needed: Sage's pseudo-terminal interfaces, `multiprocessing`, runtime installs and the PDF tools all worked.
- **Under the orchestrator's ruleset without `/opt/sandbox-science`** (an empty section in `FILESYSTEM`), Landlock refused to start the prefix's interpreter. With it, the harness needed nothing else beyond OpenShell's baseline: no devices, and no `/dev/shm`.

## arm64

The recipe builds for `linux/arm64` too, but no arm64 image has been built or tested. What it already has:

- the micromamba release and its SHA-256 for linux-aarch64;
- lockfiles that cover linux-aarch64: the same packages and versions as linux-64, apart from the compilers' architecture-named packages and two minor version differences (charset-normalizer, libglib) where a build was missing;
- pdf_oxide's aarch64 wheel hash;
- a G6K build for the target, compiled natively per architecture (D63) and aimed at `armv8.2-a`, the Raspberry Pi 5's Cortex-A76.

What arm64 still needs:

- **A build.** On an arm64 host, such as a Raspberry Pi 5 node or the Mac, or under QEMU emulation, where compiling G6K and unpacking about 400 packages is slow.
- **Ubuntu's ports archive.** Ubuntu serves arm64 packages from `ports.ubuntu.com`. On 2026-10-11 the snapshot service answered `https://snapshot.ubuntu.com/ubuntu-ports/…` with 401, so arm64 may need another way to pin Ubuntu packages, such as exact versions in `apt-packages.txt`.
- **A multi-platform image.** It needs a registry or an OCI layout to hold both architectures under one tag; `build.sh` loads one platform into the local store.
- **The tests**, run on an arm64 host.

## Not yet done

- **arm64 images** (above).
- **A private registry** for crypto-base, and for multi-platform images. OpenShell's VM driver can create sandboxes from images in the local Docker store, so babbage needs none yet.
- **An SBOM** in a standard format (SPDX or CycloneDX) before `minimal` is published. The image record lists every package and license, but in its own format.
- **The images in an OpenShell sandbox.** The tests emulate OpenShell's Landlock ruleset and a read-only root. A lab should run the worker with these images (`python = "/opt/sandbox-science/bin/python"`, the profiles in `[openshell.environments]`), the targets file above and the conformance suite, and check:
  - that the prefix is still owned by uid 1000 once the VM driver has unpacked the image, so runtime installs work;
  - the VM driver's limits for images from the local Docker store: it exports the root filesystem as a tar archive, by default of at most 10 GiB (`rootfs_tar_max_bytes`), and crypto-base's is about 6.2 GB. The first sandbox's overlay disk holds the archive and the unpacked tree while the image is prepared.
- **The orchestrator image in an OpenShell sandbox.** The tests run the harness under the emulated ruleset and a read-only root, against a stub of the worker API. A lab should create the orchestrator's sandbox from the image, under the policy `compile_orchestrator()` gives for the targets file above, and run the harness by its full path against a real worker, as labs 7 and 9 did with a wheelhouse.
- **Restore and rebuild.** The record and lockfile are there; capturing an environment's changes and rebuilding it from the lockfile belong to the environment manager (W3, D48).
