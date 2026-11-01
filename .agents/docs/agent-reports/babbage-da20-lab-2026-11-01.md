<!-- SPDX-License-Identifier: MIT -->
# Babbage lab: DA-20, a delegated run's processes held to its write area, on real OpenShell (2026-11-01)

Run on babbage on 2026-11-01, from 17:50 to 18:15 UTC, against OpenShell 0.1.5 (VM driver; guest kernel 6.12.109, one vCPU), from a fixed worktree of `k8s-sync` at `5a7e4089` (lab scripts on the local branch `labs/da20-2026-11-01`). Images were built locally from that tree and never pushed: `sandbox-science/minimal:c0231dc2`, `sandbox-science/crypto-base:c0231dc2` and `sandbox-science/orchestrator:e8504410`. The workspace UI was built from the same tree.

- Part A ran `images/test.sh` for each image, in container mode and in the Landlock mode.
- Parts B and C ran in one disposable root brought up by `model_lab.py up` (its own gateway, middleware, cache, worker, control, workspace and agent runtime on random loopback ports, in transient `ss-modellab-c29fbb4d-*` units).
  - Part B used the new `da20_probes.py` against the worker's API, with no model.
  - Part C was a real-model session through the gateway alias `arr-claude-opus-5.5` (listed by the gateway's `GET /models` before use; Messages API) for every role.

**Model spend: USD 1.05** reported by the gateway, over 28 calls (main USD 0.52, delegated USD 0.53; every call `cost_source: gateway`, none unpriced), against the lab's USD 30. The project's local daily limit was USD 15, as a stop. Nothing is left running.

**Verdict.** File confinement holds on real OpenShell. A child's shell, cells, background jobs and restarted kernel write only in its area and `$TMPDIR`. Its limits, priority, ptrace and lock calls on the parent's processes are refused or do nothing. Ordinary research work runs end to end with a real model. **Two of DA-20's guarantees do not hold inside an OpenShell 0.1.5 sandbox:**
- **Signals:** a child's `kill()` reaches its parent's processes. The lab ended the parent's kernel this way.
- **TCP:** a child can bind and listen on TCP, and can connect to a parent's TCP server.

Both bypass the child's Landlock domain because OpenShell's supervisor performs those calls on the sandbox's behalf (defects 1 and 2). Nothing was refused as `unsupported`.

## Results

| # | Check | Result | Evidence (one line) |
| --- | --- | --- | --- |
| A1 | `held-run`, minimal, container and Landlock | PASS | Both modes: "shell and a Python pool held: area written, input and prefix refused". All other checks passed too (12 in container mode, 13 under Landlock). |
| A2 | `held-run`, crypto-base, container and Landlock | PASS | Both modes: "…held: area written, input and prefix refused; Sage's gp and Singular through pexpect". All 22 and 23 checks passed. |
| A3 | orchestrator | n/a (no `held-run`) | The harness image plans no `held-run`. Its 8 and 9 checks passed in both modes. |
| B1 | `hello`'s confinement in the sandbox | PASS | In the sandbox, `sandbox_confine.abi()` gave `(6, None)` and `--probe` exited 0, which is what `hello` reports: ABI 6, no reason. The worker ran every held request (no `unsupported` in any answer or log). |
| B2 | Our seccomp filter on top of OpenShell's | PASS | `Seccomp_filters` is 5 in a parent's process and 6 in a child's. In the child, io_uring gives `ENOSYS` and AF_SMC `EAFNOSUPPORT` (ours). In the parent both give `EPERM` (OpenShell's), so the child's filter is the one that answers. |
| B3 | A child's shell: area, `$TMPDIR`, refusals | PASS | It started in `work/dlg-da20a` with `RUN_WORK` set and wrote `notes.md`, `results/r.txt` and `$TMPDIR/t`. Every other write was `Permission denied` and left the files unchanged: parent file, new file beside it, sibling's area, `inputs/`, `shared/`, the executor's directory, the package layer (`site-packages`, prefix `bin`), `$HOME`, rm, mv out, write through a link. |
| B4 | A child's shell reads `inputs/` and `shared/` | PASS | `read inputs: x,y 1,2`, `read shared: shared original`, `read parent: original parent file`. |
| B5 | A child's cell, then a kernel restart | PASS | Both cells ran in the area and wrote `cell.txt`. Parent, input, sibling, shared and executor writes were `13 Permission denied`. All 12 other `@ss-kernel-` sockets were refused (`EPERM`, 0 connected). Restart answered 200 (generation 2), and the restarted kernel was held the same way. |
| B6 | Kernel confinement refusals | PASS | A cell on the held kernel without an area: `conflict` "kernel_confinement: run r-da20a's python kernel holds write area work/dlg-da20a, not none". A restart without an area: HTTP 409, the same. A cell with an area and no run: `invalid_request` "a delegated run's cells run in its own kernel". |
| B7 | A child's background job | PASS | The same probe as a job gave the same lines (area and `$TMPDIR` written, everything else refused) and ended `succeeded` (exit 0). Files unchanged. |
| B8 | A signal from the child to the parent | **FAIL (defect 1)** | Cell: `os.kill(parent_kernel, 0)` and `SIGCONT` were "delivered". Job: `kill` worked on the parent's kernel and the executor, while `tgkill` and `rt_sigqueueinfo` gave `EPERM`. A child's `kill -TERM <parent kernel>` succeeded, and the parent's next cell ran on a new kernel (pid 920 became 1086, execution count 1, state lost). |
| B9 | Limits, priority, I/O priority, ptrace on parent kernel and executor | PASS | `prlimit` (NOFILE, CPU), `setpriority`, `ioprio_set` and `ptrace` attach: each `EPERM`. `prlimit` on itself works. The parent's kernel still burnt 1.5 s of CPU with limits `(1024, 4096)`, `(-1, -1)` and nice 0. `sched_setaffinity` "done" is inconclusive with one vCPU. |
| B10 | `flock` on a parent file, and a lease | PASS | The child's `flock` `LOCK_EX` and `lockf` "done" (no-ops) on a read-only descriptor. While its job held them, the parent's `flock -n -x sources/a.md` printed `parent-got-exclusive-lock`. `F_SETLEASE` was `EPERM`. |
| B11 | TCP through the proxy | PASS (but see B12) | `GET https://github.com/` gave 200 from the child. No proxy variable is set in a sandbox process (`proxy_variables: []`), so the wrapper got no `--connect` port. The connection worked only because the child's TCP rules are not applied (defect 2). |
| B12 | Local TCP bind and listen refused | **FAIL (defect 2)** | The child bound and listened on `127.0.0.1` ("bound 34275"). It also connected to a TCP server the parent's job opened on loopback, and the parent's server received `child-127.0.0.1`. The same wrapper on the host (Linux 7.0) gives `EACCES` for both bind and connect. |
| B13 | MPTCP, SCTP, AF_SMC, io_uring refused | PASS | Child: MPTCP and MPTCP6 `93`, SCTP `93`, AF_SMC `97`, io_uring `38`. OpenShell also refuses MPTCP and SCTP for the parent (`93`), and AF_SMC and io_uring as `EPERM`. |
| B14 | `provision` for the child; `pip` from its shell | PASS (message could be clearer) | Before: `ModuleNotFoundError: sympy`. `/v1/provisioning` `conda: [sympy]` completed (revision 0 to 1, 6 files). After: the held kernel gave `('1.14.0', '(x - 1)*(x + 1)*(x**2 + 1)')`, and the held shell imported it. `pip install six` exited 126: "pip: cannot run /opt/sandbox-science/bin/pip: [Errno 13] Permission denied: '…/.executor/provisioning/lock'" (finding 3). |
| B15 | The parent is not restricted | PASS | The same shell probe without an area started in the session directory, wrote every path above and printed `WROTE` for each. A parent job wrote `sources/job.md`. The parent can also bind TCP and reaches github. Files were restored and stray files removed afterwards. |
| C1 | Children doing ordinary work with a real model | PASS | 4 tasks, all `returned`/`answered`, all accepted by the parent: A (2 cells, `ls -la`, `results/a.json`: 2262 primes, sum 21171191); B (sympy: Smith form diag(2, 6, 12), x^8-1 factored); C (`start_job`, `wait_jobs`, a cell recomputing 111); D (networkx, see C2). |
| C2 | A package missing from the image reaches `provision` | PASS | D's shell `import networkx` failed. The child called `provision` (`conda: [networkx]`), then again for `numpy` and `scipy`. Its shell then printed `networkx 3.7` and `2 5 2000` and wrote `results/d.txt`. (B found sympy already installed by part B, which used the same environment.) |
| C3 | Every child execution held; no `unsupported` | PASS | All 10 of the children's cells, shells and job named their own `work/dlg-…` write area in the worker's record. Cells and jobs ran in the area (C's cell: cwd `…/work/dlg-a5974…`, `job-out.json` found there). No tool record or log has `unsupported`, `kernel_confinement` or `Permission denied`. |
| C4 | The parent reads the children's results | PASS | The main run called `read_file` on `work/dlg-…/results/a.json`, `b.txt`, `c.md` and `d.txt`, then `accept_delegation` on each, and answered with the contents. |

## Defects

1. **A child can signal any process in the sandbox with `kill()`. It ended its parent's kernel (high).**
   - **Where:**
     - `packages/worker/src/sandbox_science/worker/sandbox_confine.py:80`: the confinement relies on Landlock's `SCOPE_SIGNAL`.
     - `seccomp_program` (`sandbox_confine.py:190`) leaves `kill` alone.
     - The executor's probe (`executor.py:1809`, `confinement_status`, and `sandbox_confine.py:388`, `--probe`) checks only that the ruleset applies, not that it holds.
     - The claims are in `docs/specs/delegated-agents.md` (DA-20, "a signal to a process outside it") and `docs/WORKER.md` ("A delegated run's processes").
   - **Symptom:** under the held ruleset, `kill(pid, 0|SIGCONT|SIGTERM)` to any sandbox process succeeds. The scope does hold for `tgkill`, `rt_sigqueueinfo` and abstract sockets, which all give `EPERM`. The same wrapper on the host refuses `kill` too, including when nested in another ruleset.
   - **Cause (most likely):** OpenShell's "same-UID self-protection" intercepts `kill` with a seccomp user notification. The `openshell-sandbox` binary names "seccomp notification", "kill sandbox", "process-group signal", "fcntl F_SETOWN" and "same_uid_self_protection". The supervisor then performs the allowed signal itself, outside the caller's Landlock domain, so the scope never sees it. `kill(1, 0)` gives `EPERM`: the supervisor protects itself only.
   - **Suggested fix:**
     - Upstream: OpenShell should answer an allowed notification with `SECCOMP_USER_NOTIF_FLAG_CONTINUE`, so the caller's own LSM checks still run.
     - In the product, until then: our filter's `ERRNO` takes precedence over OpenShell's `USER_NOTIF`. So refuse `kill` for a pid other than 0 or the process's own, and map `kill(pid > 0)` to `tgkill(pid, pid, sig)`, which Landlock checks. A preload shim like `sched_self.c` could do that mapping, so that `subprocess.terminate` and the shell's `kill` keep working inside the child. Also decide how process-group signals (`kill(-pgid)`) are handled.
     - Either way, extend `--probe` to try `kill(getppid(), 0)` after `restrict` and expect `EPERM`. If the signal goes through, `hello` must report `unavailable`, so the guarantee fails closed. Correct the spec and WORKER.md meanwhile.
2. **A child's TCP rules do not hold in an OpenShell sandbox (high).**
   - **Where:**
     - `sandbox_confine.py:262`: `_ruleset` handles `BIND_TCP | CONNECT_TCP`.
     - `executor.py:412` (`proxy_ports`) and `:1863` pass the ports of the proxy variables "which OpenShell sets for every sandbox process".
     - The claims are in DA-20 and `docs/WORKER.md`.
   - **Symptom:**
     - A child bound and listened on `127.0.0.1`.
     - It connected to the parent's loopback TCP server, which received the child's bytes.
     - It reached github.com.
     - The ruleset had no `--connect` port at all, since no proxy variable is set in a sandbox process.
     - On the host, the same wrapper refuses bind and connect with `EACCES`.
   - **Cause (most likely):** OpenShell 0.1.5 virtualizes sockets. Its binary names "socket virtualization", "seccomp_addfd_send" and "socket loopback confinement". The supervisor makes the connection or binding and injects the descriptor, so Landlock's TCP rules in the child's domain see nothing. Two consequences:
     - The spec's threat ("a child cannot reach a server another run opens on a TCP port") is open on OpenShell.
     - If OpenShell stopped virtualizing, the child would have no TCP at all, because `proxy_ports` finds no proxy there.
   - **Suggested fix:**
     - Upstream: the same `CONTINUE` answer as in defect 1, for allowed socket calls.
     - In the product: add a TCP bind to the probe (expect `EACCES`) so `hello` reports what holds.
     - Either refuse delegated code where TCP is not held, or move "a child's TCP" to DA-20's *Not covered* list for OpenShell, naming the loopback reach to other runs' servers.
     - Make `proxy_ports` not depend on variables OpenShell 0.1.5 does not set (or drop `--connect` where the sandbox virtualizes sockets), and correct its docstring and WORKER.md.
3. **`pip` in a child's shell fails with an unhelpful message (minor).**
   - **Where:** `packages/worker/src/sandbox_science/worker/provisioning.py:2175` (`wrap`).
   - **Symptom:** the shell prints "pip: cannot run …/pip: [Errno 13] Permission denied: '…/.executor/provisioning/lock'". It refuses the install, as DA-20 decides, but does not say what to do instead.
   - **Suggested fix:** when the error is `EACCES` or `EPERM` and `RUN_WORK` is set, say that a delegated run installs packages with the `provision` tool. Opus 5.5 went to `provision` unprompted in C2, but a weaker model may retry `pip`.

Not product defects:

- **sympy was already installed for task B**, by part B's provisioning in the same environment. Stage `delegate2` (networkx) was added to exercise `provision` from a real child.
- **One vCPU.** `sched_setaffinity` on another process "done" cannot be told apart from a no-op when the only CPU is 0.
- **The first probe runs failed on fixture mistakes:** the environment had not been created yet, the shared write needs its guard, `curl` is not in the minimal image, and `set -u` broke the parent's run. They were fixed in the fixture and rerun. Their evidence was replaced, except `da20-hello-error.json`.

## Setup notes

- **Images.** `minimal` and `crypto-base` share recipe revision `c0231dc2`, and `orchestrator` has `e8504410`. All three were built from the worktree (layers cached; the image tests print each image's revision label).
- **Model.** `prepare --model arr-claude-opus-5.5 --api messages`. The gateway's alias list was read through `ModelGateway.aliases()`, given the key file's path. The key was never opened by the lab or printed.
- **MCP.** The portal stayed configured on the direct path as `model_lab.py` sets it. Its URL was read from the personal config at `prepare` and never written to the repository or the evidence. The personal instance was not touched and stays stopped.
- **The environment** for part B was created through `POST /v1/environments` with the project's profile and policy, as the harness's `ensure_environment` does at a session's first run. The process part deliberately sent SIGTERM to the parent's kernel in session `held`. Part C ran in session `delegate` of the same environment.

## Evidence

All of it is outside git. Each directory has a `SHA256.json`; the SHA-256 of that manifest file is given here.

| Part | Directory | Manifest SHA-256 |
| --- | --- | --- |
| A (image checks, build logs) | `~/.cache/pt/lab-da20-logs/` | `96c1a29fffa68568318c78472324332a8d87970f1e42c15db2c17e00a30204bb` |
| B and C (probe answers, session capture, cleanup) | `/var/tmp/ss-modellab-evidence-c29fbb4d/` | `224450d717dad6538351cfc292352223953f6e54333f7f9db811ad8e1f6ff2a4` |

`cleanup` deleted both owned sandboxes (the runtime's and the environment's) and stopped the six units. It checked that the gateway was empty, the ports closed, the processes gone and the driver directory removed, and then removed the root. No `ss-modellab-c29fbb4d-*` unit remains, and the root is gone.

## Addendum: live recheck of the fixes (2026-11-01, branch `b7/held-signals`)

Defects 1 and 2 and finding 3 were fixed on `b7/held-signals`: the held run's seccomp filter refuses `kill`, `rt_sigqueueinfo` and descriptor owners (OpenShell 0.1.5's supervisor carries out the first two with its own `pidfd_send_signal`, per its source), a preloaded signal shim gives held programs `kill` back through `tgkill`, the probe checks that no signal leaves (confinement protocol 2), TCP is recorded as not covered, and a refused package command names `provision`. Rechecked between 19:00 and 19:10 UTC in a fresh disposable root (`model_lab.py prepare --no-model`: no model, no direct MCP, no personal config read), OpenShell 0.1.5, VM driver, guest kernel 6.12.109, with `sandbox-science/minimal:2c057abb` and `sandbox-science/orchestrator:2c057abb` built locally from the branch and never pushed. `images/test.sh minimal` passed in both modes first, `signal-shim` and `held-run` included.

| # | Check | Result | Evidence (one line) |
| --- | --- | --- | --- |
| R1 (B1, B12) | `hello` in the sandbox | PASS | `abi (6, None) protocol 2`; `--probe` printed `{"tcp": false}` and exited 0: every signal call to the executor was refused, and TCP is reported not held. Held requests ran (protocol 2 accepted). |
| R2 (B8) | Signals from a held child to the parent's kernel, the executor and a parent's job that records what it gets | PASS | Through the shim and as raw calls: `kill`, `tkill`, `tgkill`, `rt_sigqueueinfo` and `rt_tgsigqueueinfo` with `si_code = SI_QUEUE`, each with 0, `SIGCONT` and `SIGUSR1`; `killpg` and `kill(-pgid)` of each one's group; `F_SETOWN`, `F_SETOWN_EX`, `F_SETSIG`, `FIOSETOWN`: all 70 `EPERM`. Raw `kill(-1, SIGUSR1)` `EPERM`; through the shim it returned 0 having reached nothing outside. The parent's job recorded no signal. |
| R3 (B8) | A child's `kill -TERM` of the parent's kernel | PASS | `kill: (777) - Operation not permitted` from the shell and from Python; the kernel group's `kill -CONT -- -777` refused too. The parent's next cell ran in the same kernel (pid 777, execution count 7). |
| R4 | A child signals its own processes | PASS | Shell: `kill -TERM` of its `sleep` (status 143), `timeout 1 sleep 10` (124). Python: `Popen.terminate` (-15), `kill` (-9), `os.killpg` of its own group (-15), `kill(0)` reached itself; a `multiprocessing` pool (285) and its `terminate`, a `ProcessPoolExecutor` (285), and joblib's `Parallel` on loky (285, joblib provisioned first). |
| R5 (B12) | TCP in a held child | As documented | Bound and listened on 127.0.0.1 and reached github.com with no proxy variable: not held under OpenShell, now in DA-20's *Not covered*. MPTCP, SCTP 93; AF_SMC 97; io_uring 38 (unchanged). |
| R6 (B14) | `pip install six` in a held shell | PASS | Exit 126, "pip: cannot run …/pip: [Errno 13] Permission denied: '…/provisioning/lock'" and "sandbox-science: a delegated run's processes cannot change the installed packages; install them with the provision tool instead". `provision` of sympy still worked (revision 1 to 2) and the held kernel and shell imported it. |
| R7 (B3–B7, B9, B10) | Files, cells, restart, job, limits, locks | PASS (no regression) | Same lines as before: area and `$TMPDIR` written, every other write `Permission denied`, files unchanged; 12 other kernel sockets refused; restart generation 2 held; `kernel_confinement` and `invalid_request` refusals; `prlimit`, `setpriority`, `ioprio_set`, `ptrace` `EPERM`; locks no-ops, the parent got its lock; lease `EPERM`. |
| R8 (B15) | The parent is not held | PASS | The parent's shell wrote every path and its job wrote `sources/job.md`; 5 seccomp filters in its process, 6 in a child's. |

Not rechecked: crypto-base (its image was not rebuilt; `held-run` there runs in the image checks), and a real model (part C).

Teardown: the first `cleanup` deleted both owned sandboxes, stopped the six `ss-modellab-6516dced-*` units and checked the gateway empty, ports closed, processes and the driver directory gone; its export then refused the no-model root's loopback model address as a private word (fixed in `model_lab.py`), so the receipts were exported and the root removed by the same steps by hand. No `ss-modellab-6516dced-*` unit or process remains, and the root is gone. The personal instance stayed stopped and the cluster was not touched.

| Part | Directory | Manifest SHA-256 |
| --- | --- | --- |
| Builds, image checks, probe output, teardown logs | `~/.cache/pt/lab-b7sig-logs/` | `6724df9d3d81dbd54416fec1be4cb645efd404b2710e372a941cf79bad6395b1` |
| Probe answers and receipts | `/var/tmp/ss-modellab-evidence-6516dced/` | `89833b3df1fefa33eab3f2a788e9faf124c8a68e3b08c88c3c37362d2caf6f1a` |
