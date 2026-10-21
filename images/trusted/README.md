# Unpublished trusted service images

These local-only targets are separate from the agent runtime and research images.
They use the digest-pinned Ubuntu/base stages and exact shared
`orchestrator/conda-lock.yml` bootstrap. Hashed, wheel-only requirements exported
from `uv.lock` fix the service versions; the image record distinguishes bootstrap
Conda metadata from the actual installed Python distributions. Neither trusted
target installs Sage/Jupyter research kernels; the worker omits the orchestrator
and workspace application wheels.

Trusted apt installation retains the exact Ubuntu snapshot and normal signature/TLS
verification. A shared build helper uses five apt transport retries and makes any
index failure fatal before installation. This prevents an unavailable snapshot from
being misreported as nonexistent packages; a persistent archive outage still stops
the build. It never falls back to current packages or changes the snapshot date.
Only the worker target installs apt tools: git and OpenSSH support its OpenShell
bridges. The application and its web builder need neither; pinned Node extraction
uses the existing Python lzma/tarfile support with the data extraction filter, so
they do not depend on external xz or worker-only apt downloads. Built-in and already
imported skills work without git. Importing a new skill pack from git remains a
separate operator preparation command that requires git; the application image
does not claim that optional import capability.

`trusted-app` installs contracts, orchestrator and workspace, including the wheel's
built-in skills. Its immutable web files are at `/usr/share/sandbox-science/workspace`
and math/figure checker files at `/usr/share/sandbox-science/math`. The Node binary
is `/usr/local/bin/node`. `trusted-worker` installs contracts, policy, event store,
worker, policy middleware and package cache, with `/usr/local/bin/openshell` and
snapshot-pinned OpenSSH client/git dependencies. No gateway server, VM driver,
cluster token or default deployment config is embedded.

`images/build_trusted.sh trusted-app|trusted-worker` takes exactly one profile,
loads the result locally and accepts no arbitrary Buildx arguments, exporters,
registry outputs or push flags. `PLATFORM=linux/amd64|linux/arm64` and `BUILDER`
select local build machinery. Existing publication scripts and workflow profile
lists do not include these targets. Building does not publish an image, and does
not qualify either architecture on a real cluster.

`images/test_trusted.sh trusted-app|trusted-worker [LOCAL_IMAGE]` runs an existing
local image with pull and network disabled, a read-only root, no capabilities and
bounded scratch/memory/PIDs. It checks installed CLIs and identities; the application
also checks packaged skills, S3 support, web/preview assets and actual math/figure
bundles through their production checkers. It does not start a model, use keys or
connect to a gateway, cluster or existing service. A passing smoke test is not live
OpenShell/Kubernetes qualification.

Native binary pins in the Containerfile were checked against primary release
manifests on 2026-10-20:

- Node **24.22.0**, the maintained Node 24 LTS patch released 2026-10-14, matches
  the repository's CI major and `>=22.12` engine. Both architecture checksums come
  from the [official SHA256 manifest](https://nodejs.org/dist/v24.22.0/SHASUMS256.txt)
  and the version/date from the [official release index](https://nodejs.org/dist/index.json).
- OpenShell CLI **v0.1.3** for AMD64 and ARM64 uses the official Linux musl assets
  and [release checksum file](https://github.com/NVIDIA/OpenShell/releases/download/v0.1.3/openshell-checksums-sha256.txt),
  matching the [release](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.3).
  The gateway, drivers and supervisor must independently match that release.

The native archives' upstream LICENSE files remain in the images. Python and web
dependencies retain their checked package notices. Startup/configuration, state
mounts and keys are deployment concerns, not image defaults. A trusted image is
not the restricted orchestrator runtime profile and fails that runtime's profile
check if accidentally used there.
