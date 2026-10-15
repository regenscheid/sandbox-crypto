<!-- SPDX-License-Identifier: MIT -->
# OpenShell Lab37: the launcher, worker protocol 5 and a real-model chat

Run on babbage on 2026-10-15 (UTC), from main `2a8243ad0cdee2e92c5e35a5ff22667b6e3fddb2` in a worktree, against the OpenShell 0.1.3 lab that `.agents/labs/lab_up.sh` brings up, with the images `sandbox-science/minimal:02f85b3` and `sandbox-science/crypto-base:02f85b3`. It was the first run of the [launcher](../../../docs/QUICKSTART.md) and the first whole run with a real model. Teardown was complete.

## Setup

- The launcher config was [`examples/sandbox-science.toml`](../../../examples/sandbox-science.toml) with its own state directory and two projects: `lab37` (crypto-base, session `main`) for the chat, and `lab37f` (minimal, sessions `files` and `results`) for the worker and recording checks. There was no `[mcp_proxy]`.
- `up` ran in a `systemd-run --user` unit. The model was the owner's gateway, alias `sandbox-orchestrator`. The config named the key file, and nothing else touched it.
- The worker checks used the run service's worker token, which the launcher limits to the config's projects. The file recording checks ran `sandbox-science-harness run` with a scripted model against the same worker and control store.

## Results

| Item | Result | Evidence |
| --- | --- | --- |
| `up` | Pass | Worker, policy grant to both projects, both projects and their sessions, run service and workspace, in that order. Listeners were 127.0.0.1:8100, 127.0.0.1:8200 and 127.0.0.2:8201 only |
| `status` | Pass | Lists each part's process and address, then each sandbox. Exit codes: 0 while running, 3 when stopped, 4 with sandboxes running and the services stopped |
| Workspace login | Pass | The address came from the private file (mode 600) and was opened in the built-in browser. No log held it |
| `down` | Pass | Stopped the workspace, then the run service, then the worker after suspending both sandboxes (about 1.5 s). `status` and OpenShell showed both `Stopped` |
| Second `up` resumes | Pass | The next use resumed each sandbox (6.5 s and 6.7 s). Files written before were intact, and the kernels reported `state_lost` |
| `down --keep-sandboxes` | Pass | The services stopped in 0.65 s and both sandboxes kept running |
| SIGTERM to `up` | Pass | Stopped in 2 s, the same as `down`, and suspended the running sandbox |
| A part dying (extra) | Pass | After SIGKILL to the worker, `up` exited 1 and said the sandboxes were not checked. `status` exited 4, and `down` started the worker alone and suspended the running sandbox |
| Per-run kernels (protocol 4) | Pass, with bug B2 | Each run got its own `krn.r…` kernel and shared no state. A run's restart and close left the session's kernel alone. A cell for a closed run failed `conflict` |
| `deleted_files` (protocol 5) | Pass | Shell calls and kernel cells reported the files they deleted, untruncated |
| Daemon push (#246) | Pass | Lines printed 0.37 s apart arrived 0.368 to 0.377 s apart, the first within 35 ms. With replay every 0.1 s they would be quantized; with the 2 s pass, batched |
| Abstract Unix sockets (#179) | Pass | Kernel connection files say `ipc` with `@ss-kernel-` and 32 hex digits. Each kernel still has one loopback TCP listener, ipykernel's own debugger socket, which the kernel binds itself |
| State-directory lock (#224) | Pass | While the launcher's worker ran, a second `serve` and `captures` on its state directory were refused |
| Kernels across a worker restart | Pass | After `down --keep-sandboxes` and `up`, the session kernel, a run's kernel, and the chat's Python and Sage kernels kept their globals and generation, with no `state_lost`. After a suspension the state was lost and reported, as designed |
| File recording, all files | Pass, with bug B1 | A shell call and a kernel cell made 8 revisions: 6 with content, 1 hash-only (`too_large`, 9 MB) and 2 tombstones with `artifact.deleted`. An identical rewrite added only a reproduction. Ignored paths (`__pycache__/`, `tmp/`) were left out. Attribution was `execution_record` with the execution and its attempt state |
| File recording, results only | Pass | After switching the project to `results`, a call deleting a recorded file and writing and deleting others recorded nothing: no revision, event or `recorded_files` |
| Real-model chat | Pass | The run started 70 s after the message, while the crypto-base sandbox was made, and its turns then appeared in the chat as they came. Python and Sage both gave 2^64 mod 1000003 = 350687. The run made the document `lab-37-smoke-test` (shown with its math) and the ledger claim C-1 (Claims, proposed). The kernel panel showed `krn-main-python` and `krn-main-sage`. The run recorded 6 model calls, 51,997 input and 418 output tokens |

## Bugs

- **B2, medium: the daemon dropped a run's name from its kernel's start event** ([#287](https://github.com/regenscheid/sandbox-crypto/pull/287)). Over OpenShell, the worker took a run's kernel start for the session's:
  - the execution's `kernel` said `run: null`;
  - the session kernel's generation record took the run kernel's generation, so after the suspension it came back at generation 3, not 2;
  - after a run's kernel restarted, the session kernel's start revision became unknown for good.

  The fix keeps `run` among the daemon's pinned fields. The conformance test now checks this over the daemon and fails without the fix. Only delegated runs use run kernels, and delegation is not enabled yet.
- **B1, low: tombstones counted as `without_content`** ([#288](https://github.com/regenscheid/sandbox-crypto/pull/288)). The fix counts only the files recorded without their bytes.

## Observations, not fixed

- **The ledger tool's `fields` is a bare object in its schema.** The model guessed the claim kind `computational`, got `invalid_request`, and retried with `observation`. A schema giving each type's fields would save the call.
- **The chat says "Queued" while a project's first run makes its sandbox,** for about 70 s here.
- **The project page's environment panel shows "This recorded environment or selected record is no longer available."** for an environment the first run has not yet made.
- **The workspace log had one "kernel observation capacity is busy" warning** while the chat ran.
- **The images are the ones built at `02f85b3`.** The recipe's last change since then (`8850a285`) only removed a type-checker comment.

## Evidence and teardown

The evidence is kept outside git, in an archive on babbage (sha256 `f7f8f97a1d01ab8ccdc2ba2a350114e3c5b68a2efc97a0f7258fe50d1743577d`, 57 entries). It holds the config, the scripts, the check records, the transcripts, the chat run's record, a copy of the control store, the parts' logs and the command outputs. It holds no tokens and no login address.

Teardown ran `down`, then `lab_down.sh`. That deleted both sandboxes, stopped the lab units and removed the lab directory. The launcher's state directory was then removed. Afterwards no lab unit, port, process, OpenShell path or driver socket was left. No image was changed.
