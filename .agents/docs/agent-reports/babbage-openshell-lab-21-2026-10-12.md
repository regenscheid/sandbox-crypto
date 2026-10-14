# Sticky package recovery admission in OpenShell on babbage

Run on 2026-10-12. Cancellation and worker-offline interruption passed against
real managed OpenShell VMs. The runs used separate fresh labs and are retained
against their exact signed source commits. The first lab was completely removed
before the final original-binding proof correction.

The cancellation evidence (archived outside git, sha256 `e0f741bb4f06`)
and final worker-offline evidence (archived outside git, sha256 `4529e8576a29`)
are retained separately. They contain API results and bounded durable-state projections,
without client or daemon tokens. The reusable check script is
[`recovery_gate.py`](../../labs/recovery_gate.py).

## What ran where

| Component | Configuration |
| --- | --- |
| Host | babbage, Ubuntu 26.04; development Python 3.13.15 |
| Initial worker | Signed clean `ef77f6d6641476bca6c67141d29ed76ad26627d5` |
| Final worker | Signed clean `e320236c8be60c19e0adbb8e784a0793163f5d05` |
| Final reusable helper | SHA-256 `38d22b9be795b3042272db95aa1f735c34e98ab37370efc6a5218d74ccf2b4d5` |
| Middleware and package cache | Main `999de2726d84acdf23d9c0cf02c023959fe98735`, unchanged throughout the lab |
| Gateway | OpenShell 0.1.3, MicroVM driver, loopback mTLS gateway |
| Sandboxes | `sandbox-science/minimal:02f85b3`, Python 3.14.8; `lab21-failed`, `lab21-success`, `lab21-other` |
| Services | Disposable transient `openshell-lab-*` user units |
| Policy and credentials | Example policy; synthetic provider and project-restricted client credentials |

## Cancel during real package linking

An already-running Python cell waited for a recovery-access file while retaining
the global `VALUE=1729`. A separate successful `networkx` installation finished
and admitted a subsequent cell. The interrupted operation requested `sympy`,
`networkx`, `matplotlib-base` and `scipy`. An independent guest observer saw
three new Conda metadata records while the operation was still `installing`;
the API cancellation followed that observation. The worker recorded
`needs_recovery`, with last good revision 0 and a post-interruption revision 1.

| Check | Observed result |
| --- | --- |
| Successful install | Finished `completed`; subsequent cell succeeded |
| Queued cell and background job | Both ended `failed`, with `started_at=null`; both forbidden command markers returned HTTP 404 |
| New shell, cell and background job | HTTP 503 `unavailable` |
| Explicit kernel restart and new provisioning | HTTP 503 `unavailable` |
| PDF execution after worker restart | HTTP 503 `unavailable` before file parsing |
| Already-running research cell | Succeeded and printed `1729` from the existing kernel |
| Independent environment | New cells succeeded before and after worker restart |
| Recovery access | File write/read, execution status/cancellation, revision history and both revision lockfiles remained available |
| Worker restart | Recovery refusal remained sticky; durable gate retained its original immutable sandbox binding and last good revision |

Both lockfile response byte hashes matched their `X-Content-SHA256` headers.
The durable failed-environment gate remained `needs_recovery`, generation 2,
bound to the original sandbox ID. The success and independent environments
remained `ready`. A worker restart did not clear the failed gate or recycle
the healthy VMs.

## Final source and worker-offline interruption

Independent review found an additional legacy proof seam: a retained hold with
unknown original binding could accept current metadata's deletion receipt. The
correction validates the original gate or exact retained executor endpoint before
any deletion mutation, including API compositions with no optional shutdown
callback. It persists that binding before shutdown and revalidates it atomically
with durable provisioning completion before clearing the hold. Unknown and
replacement identities refuse. Local adversarial probes cover those legacy and
replacement cases; the initial runtime run predates this correction.

The final signed source then ran the same successful-install control and observed
three newly linked Conda records in a second fresh lab. The helper stopped the
worker with `systemctl --user stop` while the failed operation was `installing`.
While the worker was offline, guest inspection recorded no installer process,
no forbidden queued marker, and no unreadable process command lines.

Restarting the same disposable worker with the same settings and credentials
retained `needs_recovery`. New shell/cell/job/PDF work, kernel restart and
provisioning all received HTTP 503. The original cell succeeded and printed
`1729`; the VM's immutable ID and guest boot ID were unchanged across the worker
restart. Both queued calls ended without `started_at`, and their markers remained
absent. File recovery, the known-good lockfile and revision history remained
available, and the independent environment's new cell succeeded. The final
operation's unknown post-interruption revision is not reported as a new lockfile.

After retaining the recovery evidence, explicit deletion succeeded twice against
the same original immutable ID. Recreating `lab21-failed` allocated a different
sandbox ID and boot ID, with a `ready` package gate and no old release marker.
A new cell succeeded and printed `23`; the independent environment then succeeded
and printed `29`. This proves the confirmed modern deletion/reuse path while
the negative legacy and replacement cases remain local injected checks.

## Integrated validation

The final signed source passed 9,532 Python tests (43 opt-in skips and two existing
serializer warnings), 436 web tests and 24 post-build math-check cases. Locked uv
and npm installation, Python/npm licenses, Ruff formatting/lint, strict Pyright,
Svelte/TypeScript checks, generated protocol consistency and both production
builds passed. Independent adversarial review cleared that exact source and the
reusable helper. Fresh PR CI remains the merge gate.

## Scope and remaining work

The offline run validates graceful worker stop/reconnect during installation;
abrupt host/process death and malformed historical state are local injected
tests, not additional real-VM observations here.

This slice refuses new execution after uncertain or changed managed packages.
It does not restore packages, provide an operator clear command, detect every
unmanaged package failure, or validate relocation to another host. CP-20,
uninstall/upgrade, disk limits and provisioning GUI work remain open.

The legacy unknown-binding and replacement identity cases are adversarial local
tests, distinct from these real managed VM observations. OpenShell 0.1.3 uses
name-addressed lifecycle commands; observed immutable-ID checks are not an
atomic compare-and-delete API against external controllers.

## Cleanup

The first gateway reported zero sandboxes, and the teardown removed 1.3 GiB of
disposable state. Independent checks recorded all seven owned units inactive,
the lab directory absent, all lab ports free, and no owned processes, driver
socket directories or OpenShell XDG paths. An unrelated capture test executor
was left untouched. Shared Docker images and persistent configuration were
preserved.

The second gateway also reported zero sandboxes and removed 1.3 GiB of disposable
state. Independent verification at 12:13:54 UTC recorded all seven units inactive,
no lab or VM processes, free lab ports, no driver directories, no lab directory
and no OpenShell XDG paths. Each run kept its tested source and helper unchanged.
Token scans before teardown confirmed that the retained final evidence
contains none of the lab's client tokens.
