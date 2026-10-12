# Sticky package recovery admission (CP-13 prerequisite)

This slice enforces refusal after a managed install leaves uncertain or changed packages. It does not restore packages, detect arbitrary unmanaged failures, or prove recovery by comparing package lists. `Restorer`, an operator clear command, uninstall/upgrade and CP-20 remain open. Runtime acceptance against OpenShell is a separate Lab21 gate; local fixtures are not that evidence.

## Durable hold and positive authority

The worker's existing SQLite store holds a monotonic per-environment generation, mode (`ready`, `hold`, `needs_recovery`), exact provisioning owner, last known good revision and immutable sandbox binding. SQLite uses `synchronous=FULL`. Entering `INSTALLING` and persisting its hold occur in one transaction, before the worker asks the executor to acknowledge it and before any installer is dispatched. Failure to save or acknowledge the hold prevents installation. The original recovery mark remains sticky; later operations cannot replace its owner or last good revision. An operation's final callback can release only its own ordinary hold, never a newer hold or recovery state.

Only the internal provisioner registers exemptions. The saved provisioning context/state, exact step operation ID and canonical request digest must match; the guest also requires its current hold owner/generation and current controller. A public request cannot preseed the reserved `prv-<24hex>.<step>` operation namespace, supply internal authority, or reuse an exemption after the operation ends. All new provisioning requests recheck recovery after queue waits.

Each new executor begins closed with a fresh gate incarnation. Synchronization checks the bridge/controller, guest incarnation, generation and complete acknowledged state; stale, changed or malformed replies never open admission. A legacy live daemon without this capability retains status, cancellation and file access, but refuses new protected work and provisioning without killing healthy kernels. Already accepted legacy waiters cannot be retroactively claimed protected.

## Actual start boundary

Ordinary shell, cell, PDF and background-job requests can wait behind an active install hold. `needs_recovery` refuses new ordinary work and explicit kernel restart. Queued/racing work is checked again after package, kernel readiness, snapshots, shared-file and channel waits. The final process spawn, kernel spawn or synchronous cell execute is locally serialized with the guest hold acknowledgement: either launch has already occurred before the hold is acknowledged, or it waits/refuses. Gate waits release that serialization so the installer can start, and use the request's original timeout budget.

The hold does not terminate already running research calls or healthy kernels. Recovery does not block result/event/history queries, cancellation, file reads/writes or ordinary inspection. Closing a session, deleting an environment, configured idle suspension and explicit shutdown retain their existing lifecycle meaning; they are not automatic restoration. Restart control launches and actual process/kernel start tasks remain owned through repeated cancellation and shutdown.

Startup reconciles canonical recovery status and unfinished operations conservatively, including valid older-writer installs when a ready gate already exists. All unfinished provisioning operations are classified before any ready gate acknowledgement from that classification batch. Unknown effects remain held/recovery; absence of a row, a lost RPC or a matching package lockfile is not evidence of restoration. Retained malformed scalar/identity/authority state refuses through bounded SQL projections before Python parsing.

## Original binding and deletion

A sticky hold belongs to the original immutable sandbox binding. An unknown legacy held binding can be backfilled only from the worker's retained exact executor endpoint identity, not current same-name metadata. VM stop/start used to reap a lost daemon preserves the package gate because it preserves the writable package overlay.

An abandoned or replaced sandbox keeps its original gate, revisions and lockfile evidence while the originating operation returns to classification. It never stops an observed replacement. Explicit deletion/reuse owns required provisioning cancellation and actual task drain under the environment lifecycle lock, including repeated caller cancellation. Task completion is insufficient: every relevant SQL flag and small canonical operation-state/context/identity projection must prove durable completion. A failed classification write retains the hold and prevents advisory cleanup or reuse; a later call retries classification.

Only gateway-confirmed deletion/absence of the exact retained binding records durable deletion proof. Critical cleanup can then clear that original gate, before reuse; cleanup failures are retryable against the exact proof and cannot fall through to best-effort hooks. Fresh immutable-ID checks precede lifecycle mutations and returned state is checked. OpenShell 0.1.3 addresses stop/delete by name, so this provides locally serialized observed-binding validation, not an atomic gateway compare-and-delete against another external controller. Observed replacement, unknown identity and incomplete deletion refuse without mutating a replacement or clearing the original hold.

The required pre-delete guard runs even when the optional executor-release callback is absent, and on deletion-proof retries. It validates the retained original binding before any guest mutation, preserving an exact legacy endpoint proof across executor shutdown. Atomic final cleanup checks that binding again with durable operation completion. Current same-name metadata or its deletion receipt cannot reset an unknown legacy hold; ready empty rows without a prior hold remain compatible.

## Local validation boundary

The initial worker suite passed 703 cases with 32 opt-in skips, followed by focused retained-binding checks. The first root-owned Lab21 pass on predecessor `ef77f6d` passed successful release, actual linking cancellation, queued-launch denial, retained kernel/files/revisions/lockfiles, an independent environment and sticky recovery after worker restart; root verified complete teardown. Final original-binding proof corrections add required pre-delete and atomic-clear guards, including unknown legacy refusal and exact endpoint/bound/ready positives. The corrected source passed 321 affected cases plus one separate corrupt-binding allocation case, formatting, lint and strict typing. Its combined full gates and fresh offline/delete-reuse Lab21 pass remain pending. The predecessor's full run passed 9,520 cases with 43 opt-in skips and one older HTTP-composition namespace failure; only its missing lifecycle hook defaults were corrected, without changing assertions or timeouts.

## Disposable acceptance recipe

Root coordinates Lab21 only after independent exact-source clearance. Keep the tested source tree/branch and the primary checkout used by middleware/cache unchanged throughout the run. Existing profiles use Python 3.14.8; normal repository development uses Python 3.13 with 3.12 retained in CI. No profile/runtime pins change in this slice.

```bash
R=/absolute/path/to/reviewed/worktree
R="$R" bash "$R/.agents/labs/lab_up.sh"
R="$R" PROJECT=lab21 bash "$R/.agents/labs/worker_up.sh"
"$R/.venv/bin/python" -I "$R/.agents/labs/recovery_gate.py" init prepare success interrupt verify
```

`recovery_gate.py` refuses unmarked/non-lab21 settings, uses only the synthetic lab token without exporting it, and records token-free evidence under the disposable lab. It initializes an existing stateful cell before managed installation; observes at least three real newly linked Conda records; queues a cell/job behind the install; cancels during linking; proves sticky refusal, absent queued-start markers, status/cancel/files, existing kernel globals and an independent healthy environment. The successful install checks release separately.

For worker-offline restart, use a **fresh complete lab** and replace `interrupt verify` with `interrupt --offline`. The helper stops only `openshell-lab-worker` and returns. Record the stopped unit, guest/queued markers and current existing call while offline; explicitly start the same disposable unit, then run `verify`. That includes PDF refusal, retained revision/lockfile availability, recovery-file read and actual 404 queued-marker evidence. `interrupt --restart` performs the immediate restart variant. Do not interpret unit stop by itself as proof the installer was reaped; capture final operation/attempt/guest state too.

After `verify` has saved its evidence, separately select `delete-reuse`. It deletes only marked `lab21-failed`, retries deletion idempotently, recreates the logical environment with a different immutable sandbox ID and proves a fresh cell and the independent healthy environment still work. This is a modern-binding runtime check; synthetic fixtures cover unknown legacy holds. Final teardown uses `lab_down.sh`, plus the existing README's units, listeners, VM/container, process and lab-directory checks. The parent acceptance report records actual outcomes and all cleanup evidence.
