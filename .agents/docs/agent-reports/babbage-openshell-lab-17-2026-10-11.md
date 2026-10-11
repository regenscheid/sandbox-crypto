# OpenShell v0.1.3 lab 17 on babbage, 2026-10-11: the final shared-data sync, package turns, the cache's sandbox budgets and the proxy's forward

This lab ran the latest pieces against a real gateway, then tore everything down:
- the shared-data sync (D79) as merged, whose atomic swap and in-use check had not yet run in a VM;
- package commands taking turns, with the real pip and micromamba through the package cache (PR #54);
- the package cache's per-sandbox budgets, which depend on a header the policy middleware writes;
- the MCP proxy's forward, kept up by a unit like the example in `examples/systemd`.

Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); sandboxes with 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Worker, middleware, cache | From main at 04a3793 (PR #53 merged) for the shared data, the cache and the forward. For the package turns, the worker ran PR #54's commits as its review went on: e230f97, f2df0da, and a30932c, the head that merged |
| Policy | `examples/policy.yaml`, with its provider `github-readonly` registered from the endpointless `github-token` profile and a synthetic token |
| Environments | Project `lab17`: `lab17-a` (sessions `a` and `b`) and `lab17-c` (session `c`), two VMs from `sandbox-science/minimal:02f85b3`, created through the worker's API by a client limited to the project |
| MCP proxy | `ss-lab17-proxy`, from `sandbox-science/mcp-proxy:d27e41a`, with DeepWiki's public server only |
| Harness | Scripts on the host calling the worker's API (`checks.py`, `budget.py`) and the proxy through its forward (lab 15's caller) |

## Shared data (D79)

| Check | Result |
| --- | --- |
| The swap the executor uses, `renameat2(RENAME_EXCHANGE)` through ctypes, in the directory it writes | Returned 0 on the VM's file system (`overlayfs`); the two names swapped |
| A writes `shared/notes.md`, then v2; C reads each | C read `v1`, then `v2`. The file's inode changed (264, then 267): replaced, not rewritten in place |
| C's kernel opens `shared/log.txt` to append and writes `c1`; A replaces it | C's next call still read `c1`, and its result carried the notice: `shared/log.txt is not updated in this environment while a process here is writing it; the project's newer version arrives once it is closed` |
| C's kernel writes `c2` and closes the file | That cell's result carried the conflict notice. Both environments then held `log.txt` (A's `a-version`) and `log.conflict-lab17-c.txt` (`c1 c2`) |
| C's kernel opens `notes.md` only to read; A writes v3 | C's next call read `v3`: a reader does not keep a file |
| C's kernel maps `shared/data.bin` shared and writable, and closes the descriptor; A replaces the file | C's next call still saw the 4,096 bytes of zeros, with the same notice. Once the kernel unmapped it, the next call read A's `from A` |
| A call that changes nothing | 0.02 s from submission to result, both syncs included (lab 16: 0.03 s) |

## Package turns (CP-4, CP-11, CP-12, CP-16, CP-18)

The worker ran PR #54's head (e230f97) on the same state directory, which it took over from main's without trouble. The checks used a PEP 517 build backend that sleeps (`slowpkg`), so that a real `pip install` holds its turn for as long as a check needs.

| Check | Result |
| --- | --- |
| The wrappers | First on `PATH` for shell calls: `pip` and `micromamba` in `.executor/provisioning/prefix/bin` |
| `pip install ./slowpkg` (a 15 s build) in session `a`; session `b` submits a call 5 s in | `b` stayed queued and started 0.22 s after `a`'s install ended, at the new revision (1) |
| The same, with a 3 s time limit | `timed_out` without running: `waited 3 s for a package command in this environment to finish (pip install ./slowpkg (pid 679, for 8 s)); the attempt did not start` |
| A call in the other environment meanwhile | Not held up |
| `micromamba install -y toolz` (26 s), `%pip install tomli` in a kernel, `pip install attrs` | One managed revision each, set against its attempt, with its command; `%pip` came through the hook (`module`, `python -m pip install tomli`) |
| `python -I -m pip install iniconfig` | An unmanaged revision, as documented: `-I` skips the hook |
| A 60 s install cancelled 6 s in | `cancelled`; no pip or build process left, the lock free, and the next install ran at once |
| SIGKILL to an install's wrapper, from the same call | pip, the wrapper's child, died at once: `PR_SET_PDEATHSIG` works under OpenShell's seccomp filter. The lock was free. pip's own child, the build backend, survived, reparented, until the executor's leftover cleanup ended it with the call (the review's L4) |
| The history | Revisions 0 to 6 as expected: 0 with 62 Conda packages and no pip entries (those Conda installed are left out); then slowpkg, toolz, tomli, the unmanaged iniconfig, attrs and slowpkg 0.3 |
| Revision 5's lockfile | 18,792 bytes, 63 Conda packages and 4 pip distributions. `conda-forge` names the channel of toolz, python and pip; neither the cache's host nor its port appears. slowpkg's entry has its `file://` URL |
| What scripts find beside the wrappers | `command -v python` named the mirror's stand-in, a `#!/bin/sh` script; `jupyter` was not beside it; `dirname`-`dirname` named the mirror, not `/opt/sandbox-science`. This is the regression the second review found (its M2); `"$(dirname "$(command -v pip)")/python"` worked |

The second review's fixes (f2df0da) then ran on the same environments, the worker restarted on them:

| Check | Result |
| --- | --- |
| What scripts find beside the wrappers | `jupyter` was linked beside the wrappers, so `"$(dirname "$(command -v python)")/jupyter"` worked. As documented, `command -v python` still names a stand-in script and `dirname`-`dirname` the wrappers' directory; `$CONDA_PREFIX` names the prefix |
| An install started from a kernel with `subprocess.Popen`, its wrapper killed with SIGKILL 5 s in | pip died with its wrapper; the build backend it had started survived the cell, and was gone by the next cell, ended by the executor's look. Kernels never clean up leftovers themselves, so this is the case the fix was for |
| Turns, waits, the time limit, cancellation, SIGKILL to a wrapper in a shell call | As before |
| Installs that change nothing (`pip install attrs` with attrs installed, `%pip install tomli`) | No revision |
| `micromamba install -y toolz` with toolz installed | A new revision (8), managed, whose lockfile is identical to the one before. micromamba linked nothing but appended an entry to `conda-meta/history`, which the fingerprint covered |

The third round's fixes (a30932c, the head that merged), with `conda-meta/history` out of the fingerprint, ran last:

| Check | Result |
| --- | --- |
| `micromamba install -y toolz` with toolz installed, then `pip install pyparsing`, then the micromamba command again | No revision, then revision 11 (managed, `pip install pyparsing`), then no revision |
| The first attempt after the restart | Revision 10, with no command and the same lockfile as revision 9: the fingerprint's definition had changed, not the packages |

## The cache's budgets per sandbox

| Check | Result |
| --- | --- |
| A sends `X-Sandbox-Science-Sandbox: forged-by-a` with a request to the cache; C sends none | The cache logged A's request with `sandbox=b7da2e5e-…`, A's OpenShell sandbox ID, and C's with `sandbox=05e0524a-…`, C's. The middleware's header arrived and replaced the forged one |
| The cache restarted with `sandbox_rate = 0.5`, `sandbox_burst = 2`. A asks for 8 PyPI projects the cache has not seen, at once, each request naming C's sandbox ID; C asks for 3 others 1 s later | Every one of A's requests was charged to A. Two were fetched at once, five waited 2, 4, 6, 8 and 10 s for A's bucket, and the eighth was refused at once with 503, since its wait would have passed the 10 s limit. C's first two were fetched at once and its third waited 2 s, from C's own bucket, untouched by A |

## The MCP proxy's forward

The forward ran as a transient user unit with the example unit's settings: `Restart=always`, `RestartSec=2s` and `StartLimitIntervalSec=0`.

| Check | Result |
| --- | --- |
| A call to DeepWiki through the forward | `tools/call` 200 |
| `sandbox stop` | The forward exited (`sandbox 'ss-lab17-proxy' is no longer ready (phase: Stopped); stopping`). systemd started it again every 2 s, and each attempt failed while the sandbox was down: 4 restarts in 10 s, with no start limit reached |
| `sandbox start` | Ready in 6 s, by which time the unit had the forward running again (7 restarts in all) |
| A call after the start | Succeeded 9 s after the start, on the second try: the proxy comes back a few seconds after `Ready` (lab 15) |

## Findings

1. **The final sync works in the VMs.** The atomic swap is available on the VM driver's overlay file system, under OpenShell's seccomp filter, so the executor does not fall back to a plain rename. The in-use check reads other processes' descriptors and maps there: a kernel writing a file, or holding a writable shared map of it, keeps the project's newer version out until it lets go, and a reader does not.
2. **The cache's budgets are per sandbox, and a sandbox cannot spend another's.** The middleware's header reaches the cache on OpenShell 0.1.3 and replaces what the sandbox sent.
3. **The forward unit keeps the proxy reachable across a stop and start without anyone's help.** A caller still needs to retry for a few seconds after the sandbox is ready.
4. **Package commands take turns under OpenShell with the real managers.** The wrappers, the hook in a kernel, the wait before an attempt starts, the time limit on that wait, cancellation, and the death signal under the seccomp filter all behaved as the unit tests have them, and lockfiles came out without the cache's address.
5. **Two problems were found and fixed before PR #54 merged.** `command -v python` named the wrappers' stand-in, without the prefix's other programs beside it (also found by the second review); and an install that linked nothing still made a revision, because the fingerprint covered `conda-meta/history`. On the merged head (a30932c), `micromamba install -y toolz` with toolz installed made no revision, twice, while `pip install pyparsing` made one.
6. **Changing the fingerprint's definition records one revision of unknown cause.** When the worker restarted on a30932c, the environment's first attempt recorded revision 10, with no command and the same lockfile as revision 9. Only a worker that ran PR #54's earlier commits sees this.

## Cleanup

- **Removed:** the three sandboxes, the provider and its profile, the forward unit and the units of the worker, the gateway, the middleware and the cache. The lab directory (2.0 GB) was removed.
- **Checked afterwards:** no lab processes or units, the lab ports and the forward's port free, no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`, no driver socket directories in `/tmp`.
- **Kept:** the harness's scripts and outputs and the worker's log, which hold no tokens, in the session scratchpad; the images in the local Docker store.
