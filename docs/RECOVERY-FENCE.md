# Sticky package recovery admission (CP-13 prerequisite)

This slice enforces refusal after a managed install leaves uncertain or changed packages. It does not restore packages, detect arbitrary unmanaged failures, or prove recovery by comparing package lists. `Restorer`, an operator clear command, uninstall/upgrade and CP-20 remain open. Independent review and root-owned OpenShell Lab21 passed on signed source `e320236`; both disposable labs are completely removed. PR #85 merged after all three fresh CI checks passed. Local fixtures and actual runtime observations are distinguished below.

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

## Validation and runtime boundary

Signed source `e320236c8be60c19e0adbb8e784a0793163f5d05` passed independent adversarial review and final combined local gates: 9,532 Python tests (43 opt-in skips, two existing serializer warnings), 436 web tests and 24 post-build actual math-check cases. Locked uv/npm installation, Python/npm licenses, Ruff format/lint, strict Pyright, Svelte/TypeScript, generated protocol consistency and both production builds passed. Development used Python 3.13.15; the supported 3.12/3.13 fresh CI matrix remains the PR merge gate.

Root's first [Lab21](../.agents/docs/agent-reports/babbage-openshell-lab-21-2026-10-12.md) pass on predecessor `ef77f6d` observed real package linking, cancellation and sticky recovery through a worker restart. It proved successful install release, queued calls with no start/markers, retained research globals, file/status/cancel/revision/lockfile access and an independent environment. That lab was completely removed before the final original-binding proof correction. The corrected source then passed 321 affected cases plus one corrupt-binding allocation case before its full combined run.

The final fresh Lab21 used exact `e320236`. After three new Conda records appeared, graceful worker stop/reconnect retained the original VM boot and kernel global `1729`, with no installer process left during the offline interval. New shell/cell/job/PDF requests, kernel restart and provisioning refused; queued calls had no `started_at` and their forbidden markers returned actual HTTP 404. Recovery file/status/cancel/history and the known-good lockfile remained available, and the independent environment stayed healthy. Explicit deletion twice retained the old identity; recreation proved a new immutable sandbox/boot, a ready gate and successful fresh/peer cells. Root independently verified complete final teardown at 12:13:54 UTC: all seven owned units inactive, lab directory absent, ports free, no owned VM/process/driver/OpenShell XDG state. Shared images and unrelated services were preserved.

Legacy unknown-binding/replacement, malformed retained state and abrupt-death cases are adversarial local fixtures; the real offline run used graceful worker stop. Unknown post-interruption revision is not a newly captured lockfile. No restoration, unmanaged-failure detection or atomic name-addressed gateway deletion is claimed. PR #85 merged after all three fresh CI checks passed.

Earlier local evidence remains historical: the initial worker suite passed 703 cases with 32 opt-in skips, followed by focused retained-binding checks. The predecessor full run passed 9,520 cases with 43 skips and one older HTTP-composition namespace failure. Its missing hook defaults were corrected without changing assertions/timeouts; the final 9,532-case run passed.

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
