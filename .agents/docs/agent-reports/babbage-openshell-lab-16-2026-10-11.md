# OpenShell v0.1.3 lab 16 on babbage, 2026-10-11: shared data across a project's environments

This lab ran the worker with D79's sync of `shared/` against a real gateway, with a project's default environment and a dedicated session environment, then tore everything down. It ran twice. The first run used the first implementation. The second run used the version reworked after its adversarial review, with the checks the review asked for. Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); sandboxes with 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Worker | From the `impl/shared-sync` branch, on the host under `systemd-run --user`, with `[openshell.environments]` and `shared_sync` on (the default) |
| Policy | `examples/policy.yaml`, with its provider `github-readonly` registered from the endpointless `github-token` profile and a synthetic token |
| Harness | On the host, with the pool's token and project records: session `a` in the project's default environment, session `c` with `--dedicated` |
| Environments | Two VMs with no filesystem in common, both from `sandbox-science/minimal:02f85b3`. First run: `lab16-default-…` in `ss-65029f771656db55` and `lab16-c-…` in `ss-6d5c2db79eb5fa6f`. Second run: `lab16-default-5c2f8b19a964` in `ss-aee0b29a0cb12eb8` and `lab16-c-02c61c8a44a9` in `ss-8afc3bf547225821` |

## Results

### First run: the first implementation

| Check | Result |
| --- | --- |
| Session `a` writes `shared/results.csv` in the default environment | Done; the call took part of the session's 18 s, environment creation included |
| Session `c`, in its own sandbox, reads it | `n,lwe` / `512,1`: the content `a` wrote, with no transfer by anyone |
| A writes `notes.md`; C reads it | `a`, 0.1 s after A's call ended |
| C rewrites `notes.md` (taking 5 s) while A rewrites it and finishes first | C's result carried the notice: `shared/notes.md was also changed in another of the project's environments: that version stays as shared/notes.md, and this one is kept as shared/notes.conflict-c.md` |
| Afterwards | Both environments listed `notes.conflict-c.md`, `notes.md` and `results.csv`. In C, `notes.md` held A's `b`, which arrived first, and `notes.conflict-c.md` held C's `c` |
| A call that changes nothing in `shared/` | 0.02 to 0.03 s from submission to result, both syncs included |

### Second run: after the review

The reworked sync checks what executors report, keeps each project's blobs apart, marks each environment's directory, and names conflict copies after their environment ([worker](../../../docs/WORKER.md#shared-data-across-environments)).

| Check | Result |
| --- | --- |
| Session `c`, dedicated, reads what `a` wrote | `n,lwe` / `512,1` |
| The mark | `.sandbox-science-sync` beside `sessions/` and `shared/` in each project root, holding a random token and the project's ID; the executor wrote and read it under OpenShell's filesystem policy |
| Concurrent changes to `notes.md` | C was told `… this one is kept as shared/notes.conflict-lab16-c-02c61c8a44a9.md`. Afterwards C's `notes.md` held A's `b`, and the conflict copy C's `c`, in both environments |
| A deletes the conflict copy | Gone from C at its next call |
| File requests: A and C each write `shared/x.txt`, expecting the same version | A: 200. C: 409 `conflict`, since C was brought up to date before its write was checked |
| C writes `shared/data/n.txt` (1000 lines) in a new directory | A counted 1000 lines at its next call |
| A call that changes nothing in `shared/` | 0.03 s, five times in a row |
| The worker's log | No warnings or errors |

## Findings

1. **A dedicated session sees the project's shared data.** What one environment writes in `shared/` is in the project's other environments at their next call, which the M2 exit run needs.
2. **Conflicts are explicit.** Of two concurrent changes to one file, the first keeps the name and the second is kept as a conflict copy in every environment. The agent whose change came second is told.
3. **File requests check the project's version.** Two writers in different VMs expecting the same version get what two writers in one environment get: the second is refused.
4. **The checks cost nothing visible.** With the review's checks in place, a call that changes nothing still takes 0.03 s, two scans through the executor's channel included.

## Cleanup

- **Removed after each run:** both sandboxes, the provider and its profile, and the units: worker, gateway, middleware and cache. The lab directory (1.1 GB) was removed.
- **Checked afterwards:** no lab processes, the lab ports free, no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`, no driver socket directories in `/tmp`.
- **Kept:** the worker's log and the harness's output, which hold no tokens, in the session scratchpad; the images in the local Docker store.
