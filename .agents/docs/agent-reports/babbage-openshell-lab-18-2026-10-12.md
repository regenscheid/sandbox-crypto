# OpenShell v0.1.3 lab 18 on babbage, 2026-10-12: the provisioning tool

The provisioning tool introduced by PR #64 ran against the real gateway, policy
middleware and package cache. CP-F1 to CP-F4, CP-F8 and CP-F10 passed, as did the
catalog's nine functional probes. Cancellation and a worker restart after linking
began left visible `needs_recovery` records. The lab found no platform change needed.

The run began late on October 11 and finished on October 12, America/New_York.
The [recorded evidence](babbage-openshell-lab-18-evidence-2026-10-12.json) keeps the
operation results, resolved files, probe output, relevant cache requests, grant
records and interruption observations. It contains no client token or provider secret.

## What ran where

| Component | Configuration |
| --- | --- |
| Host | babbage, Ubuntu 26.04, kernel `7.0.0-38-generic` |
| Checkout | main at `48f9730`, including PR #64 and the lab scripts from PR #68; the worker later ran the lab worktree's identical platform code |
| Gateway | OpenShell 0.1.3, VM driver, mTLS, loopback port 17670; each VM had 4 vCPUs, 4,096 MiB RAM and an 8,192 MiB overlay |
| Services | Transient `openshell-lab-*` user units: gateway, policy middleware (50051), package cache (8090), worker (8100) |
| Profiles | `sandbox-science/minimal:02f85b3` and `sandbox-science/crypto-base:02f85b3` from the existing Docker store |
| Policy | The example policy; a separate revision changed only `packages.conda.action` to `escalate` |
| Credentials | A synthetic GitHub provider token and the lab's disposable worker client tokens |
| Environments | Separate VMs for the two independent installs, the escalating policy, the catalog, cancellation and restart; all belonged to project `lab18` |

The [check script](../../labs/provision.py) uses the worker API for provisioning and
ordinary executions. Its interruption observer uses OpenShell directly to watch
`conda-meta` in the target VM. Solving, installing and probing ran inside the VMs.
The host parsed the resulting records and compared lockfiles; it installed no
research packages. No registry, persistent unit, system setting or shared Docker
image was changed.

## Resolve, pin, install and record

| Check | Observed result |
| --- | --- |
| Real micromamba dry run for `pari` | The strict parser accepted all six LINK entries. Each carried `url`, `fn`, `subdir`, `sha256` and `license`; cache paths were `/conda/conda-forge/<subdir>/<file>`. The cache saw index requests only |
| Real pip dry run for `snowballstemmer` | The strict parser accepted the wheel's cache URL and SHA-256. The cache saw two index requests and one `.whl.metadata` request, with no wheel download |
| CP-F1: request capability `gp` from minimal revision 0 | `completed`, revision 0 → 1, six decided files. The initial probe failed because GP was absent; the installed probe passed on `[274177, 1; 67280421310721, 1]` |
| Real pinned Conda installs | The revision 1 lockfiles matched every decided file's version, build, channel, subdir, file name, SHA-256 and license: six for GP, four for python-flint in the other minimal VM, sixteen for fpylll, and one for python-flint beside Sage. No operation reported an unplanned package |
| The original request in Conda history | GP's history recorded `micromamba install … pari` and `# update specs: ["pari"]`; the decided builds did not become persistent requested pins |
| A nonexistent build | Requesting `pari==2.17.3=ss_lab18_missing_build` failed during `resolve`, with no install |
| CP-F2: request GP again | `already_satisfied`, still revision 1, citing the passing revision 1 probe |
| pip's pinned, hash-checked install | `snowballstemmer` completed, revision 1 → 2, one file and a passing `import_check` of its installed version |
| Lockfiles | No cache host or URL appeared in the four compared lockfiles |

The nonexistent-build check used an explicit request for a missing build. This lab
did not change a live index between a dry run and its pinned install; PR #64's
offline and fake-index tests cover that race.

## Decisions and the grant's lifetime

| Check | Observed result |
| --- | --- |
| CP-F3, denial | fpylll resolved sixteen files and created one permission request listing all sixteen paths. The operator denied it; the operation ended `denied`, with no package file request reaching the cache |
| CP-F3, approval | A fresh operation listed the same sixteen files. Approval let exactly those sixteen paths reach the cache. Sixteen middleware `approved` records named `grant: packages` and the file-set request. The operation completed at revision 1 and its LLL probe passed |
| Fetch outside the grant while the install ran | An already-running shell repeatedly fetched GP's package file, which was outside fpylll's set. All 32 attempts received HTTP 403 with `approval_required`; none reached the cache |
| Fetch an approved file after the operation ended | HTTP 403 with `approval_required` |
| A late approval | `pending --request` showed `withdrawn: its operation has ended`. `approve` exited unsuccessfully and explained that the files could no longer be granted |
| CP-F4, unlisted channel | `bioconda::samtools` ended `denied` at `resolve`, naming `packages.conda`, before a dry run |
| CP-F10, Conda AGPL refusal | `pymupdf` ended `denied`, naming AGPL licenses on `jbig2dec`, `mupdf` and `pymupdf`. The cache saw 440 index requests and no package file request |
| PyPI AGPL refusal | `pymupdf` ended `denied`, naming its Affero GPL/commercial declaration. The cache saw two index/metadata requests and no package file request |

## Independent environments and the catalog

CP-F8 passed across two minimal VMs. GP was installed only in A and python-flint
only in C, each with its own revision 0 → 1 and lockfile. `command -v gp` failed in
C, and `import flint` failed in A. Their own functional probes passed.

The crypto-base VM initially lacked python-flint, while its other eight catalog
probes passed. The provisioning tool added python-flint there as one decided file,
revision 0 → 1, without changing Sage's packages. Then all nine probes passed in
that VM. Final probe execution times, excluding API and executor startup:

| Capability | Expected output | Seconds |
| --- | --- | ---: |
| gp | `[274177, 1; 67280421310721, 1]` | 0.035 |
| gap | `[ 274177, 67280421310721 ]` | 0.526 |
| sage | `274177 * 67280421310721` | 0.909 |
| sympy | `{274177: 1, 67280421310721: 1}` | 0.295 |
| fpylll | `True` | 0.204 |
| python-flint | `[(274177, 1), (67280421310721, 1)]` | 0.057 |
| networkx | `5` | 0.197 |
| numpy | `1.0` | 0.081 |
| pandoc | `<h1 id="hi">Hi</h1>` | 0.023 |

## Interruptions after linking began

Each check used a fresh minimal VM and a real install of `sympy`, `networkx`,
`matplotlib-base` and `scipy`, resolving seventy files. An observer took the
initial set of sixty-two Conda records, then waited for three new records:
`cycler`, `networkx` and `pyparsing`. The worker still reported `installing` at
that point. Only then did the check interrupt it.

| Interruption | Operation and environment |
| --- | --- |
| Cancel through the provisioning API | `needs_recovery`, revision 0 → 1, with the install's cancellation error. The queue exposed `needs_recovery` at revision 1 |
| Restart the worker's transient unit | `needs_recovery`, reason `worker_restarted`, revision before 0 and revision after unknown. The queue exposed `needs_recovery`, initially still at its last recorded revision 0 |
| A later ordinary call in each environment | Succeeded; no pip, Conda or micromamba process remained. Both calls reported revision 1, including the restarted environment's first subsequent observation |

Both results named revision 0's lockfile as the recovery reference. Neither
claimed restoration. As documented, the current slice marks the environment but
still permits executions; automatic restoration and refusal after a failed
restore remain future work. The restart check was a service restart, not an
abrupt crash or power loss. This lab did not exercise the failing post-link and
confinement fixtures CP-F5 and CP-F6.

## Repository checks

The Python checks passed: ruff formatting and lint, strict pyright, **8,898 tests
passed and 43 integration tests skipped**, licenses and generated-protocol checks.
The web checks passed: lockfile and installed-package licenses, svelte-check and
TypeScript, **405 tests**, and the production build. npm installed with
`--ignore-scripts`.

## Cleanup

All six sandboxes were deleted. The lab units stopped, the synthetic provider
and gateway state were removed with the lab directory (14 GiB), and teardown
verified no lab processes, listening ports, driver socket directories, or
OpenShell paths under `~/.config`, `~/.local/state` and `~/.local/share` remained.
The Docker images were preserved.
