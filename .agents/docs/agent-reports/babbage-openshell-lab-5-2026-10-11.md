# OpenShell v0.1.3 lab 5 on babbage, 2026-10-11

Runtime validation of Sage kernels (PR #20) on a real gateway, followed by a full teardown. A real SageMath from conda-forge ran in OpenShell VM sandboxes, driven through the [worker service](../../../docs/WORKER.md) and the harness. Like the earlier labs, this report records observed behaviour.

## Setup

- **Gateway, policy middleware, package cache.** As in labs 2 to 4, on loopback in transient systemd user units, brought up and torn down by scripts. Sandboxes had 8 vCPUs, 8 GiB of memory and a 32 GiB overlay disk, since Sage is large.
- **Research sandboxes.** `docker.io/library/buildpack-deps:noble-scm` with the compiled example policy and the `github-readonly` provider (synthetic token). Three sandboxes:
  - `research-1`: the policy as compiled before this lab;
  - `research-2`: the same policy with a hand-written filesystem section (finding 1);
  - `research-3`: the policy compiled by the fixed compiler.
- **Sage.** micromamba 2.9.0 (static binary from GitHub, checked against its published SHA-256) installed `sage` from conda-forge through the cache: SageMath 10.9, Python 3.14.8, PARI 2.17.3, Singular 4.4.1, fpylll 0.6.4, 386 packages. Its `sagemath` kernelspec runs `python -m sage.repl.ipython_kernel`, and jupyter_client runs that with the executor's interpreter.
- **Worker service.** It ran on the host with the OpenShell backend and tier 3 declared, and used the Sage environment's Python, so the executor offered `python` and `sage`.
- **Baseline.** The host's own Conda environment had the same SageMath 10.9 build. It served as the tier-0 baseline, run with `HOME` and `DOT_SAGE` pointing into the lab directory.

## Results

| Check | Result |
| --- | --- |
| Install | A dry-run solve took 85 s with empty caches, including fetching repodata through the package cache. The install then downloaded 1.45 GB and took 34 s. A second sandbox, with the package cache warm, took 31 s for everything. Unpacking dominates, not the download |
| Capabilities | The worker reported `python` and `sage` for the sandbox |
| Kernel start | The first Sage cell took 2.3 s, including the kernel's start |
| Preparsing | `2^10` gave 1024 and `1/3 + 1/6` gave 1/2; integer literals are Sage `Integer`s |
| Finite fields | GF(2^8) had order 256 and modulus x^8 + x^4 + x^3 + x^2 + 1, and x^256 = x for every element. `finite-field-identity-v1` gave the roadmap's canonical JSON natively, once its counts were converted with `int()` (finding 3) |
| Polynomial rings | x^6 − 1 over GF(7) factored into six linear factors |
| Gröbner bases | Through libSingular: the basis of (x² + y² − 1, x − y) over QQ was {x − y, y² − 1/2}. Cyclic-5 to Cyclic-7 over GF(32003) worked once the sandbox could open a pseudo-terminal (finding 1) |
| Exact matrices | The inverse of [[1, 2], [3, 4]] over QQ came out exact, with determinant −2 |
| LaTeX | `latex(M)` returned LaTeX source. A result came back as `text/plain` only by default, and as `text/html`, `text/latex` and `text/plain` after `%display latex` |
| Number theory and elliptic curves | `factor(2^128 + 1)` and a point count over GF(next_prime(2^64)) took about 50 ms each, through PARI |
| GAP and Maxima | libgap gave \|S5\| = 120, with 1.5 s for the first use. Maxima integrated sin(x)² |
| Lattices | fpylll's LLL on a 60-dimensional q-ary lattice took 74 ms; Sage's `LLL()` on a random 100 × 100 integer matrix took 27 ms |
| Two kernels | The Python and Sage kernels of one session kept separate variables |
| Interrupt | A long PARI factorisation stopped 0.59 s after cancellation, and the next cell still saw earlier variables |
| Timeout, restart, death | A cell over its 2 s limit ended `timed_out` at 2.0 s. A restart and an `os._exit` both reported `state_lost`, with the generation still growing, and left the Python kernel untouched |
| Network | From the Sage kernel, the package cache answered 200 and `pypi.org` was refused |
| Harness | `sandbox-science-harness run` with a scripted model used the `sage` tool, `restart_kernel` with `language: sage`, and the `python` tool, all reporting tier 3. With `%display latex`, the model received the LaTeX form |
| Latency | The median round trip of a trivial Sage cell through the worker API was 36 ms (a Python cell took 20 ms in lab 4) |

**Performance at tier 3.** One file of six single-threaded workloads ran as a fresh process, with `OMP_NUM_THREADS=1`, in the sandbox and on the host. The table gives the medians of three runs each, from two alternating rounds, at a load average of 1.5:

| Workload | Host, tier 0 (s) | Sandbox, tier 3 (s) | Difference |
| --- | --- | --- | --- |
| Python loop, 40 million squares | 1.12–1.15 | 1.18 | +3–5% |
| PARI: factor a 57-digit semiprime | 0.71 | 0.73 | +2% |
| fpylll: LLL then BKZ-25, dimension 80 | 0.70 | 0.75 | +7% |
| M4RI: 16000 × 16000 product over GF(2) | 1.06–1.08 | 1.07 | about 0 |
| FLINT: determinant of an 800 × 800 integer matrix | 1.15 | 1.17–1.18 | +2–3% |
| Singular: Cyclic-7 Gröbner basis over GF(32003) | 1.04 | 1.07 | +3% |

So the VM costs about 0–7%, typically 3%, on CPU-bound mathematics.

## Findings

1. **OpenShell's filesystem baseline blocks pseudo-terminals and shared memory.**
   - A policy without a `filesystem_policy` gets OpenShell's baseline: read-only `/usr`, `/lib`, `/etc`, `/app`, `/var/log`, `/proc` and `/dev/urandom`, plus read-write `/tmp`, `/dev/null` and the workdir. Landlock refuses everything else.
   - **What broke:**
     - Opening `/dev/ptmx`, `/dev/pts`, `/dev/shm`, `/dev/zero`, `/dev/full`, `/dev/random` and `/dev/tty` failed with EACCES.
     - With no pseudo-terminal, Sage's pexpect interfaces could not start. `sage.rings.ideal.Cyclic`, which loads a Singular library that way, failed with "out of pty devices".
     - Python's `multiprocessing`, even a single `Lock`, failed without `/dev/shm`.
     - This also explains lab 1's unreadable `/dev/zero`.
   - With those paths in the policy (`research-2`), PTYs, `multiprocessing.Pool`, Sage's `@parallel` and the pexpect interfaces all worked.
   - **Fix:** the compiler gives each research sandbox a filesystem section with these devices, and the deployment's targets file can add its image's paths. OpenShell fixes this section when a sandbox is created; later revisions change only the network sections (`network_only()`). `research-3`, created from the fixed compiler's output, could read `/dev/zero`, write `/dev/shm` and allocate a PTY.
   - **Side effect:** the guest kernel has `dev.tty.legacy_tiocsti = 1`, so sandbox code can inject keystrokes into any terminal of the sandbox user, such as an interactive `openshell sandbox connect` session. The sandbox user can run those commands anyway, so this stays inside the sandbox's trust zone. Do not type secrets into interactive sessions in research sandboxes.
2. **The environment's tools were not on `PATH`.**
   - The tools spec puts them first, but the executor passed its own environment through unchanged.
   - `PATH` held only the image's system directories, so Sage's `gp` interface reported that `gp` was not found, and its GAP console interface crashed.
   - **Fix:** the executor puts its interpreter's directory first on `PATH` and sets `CONDA_PREFIX` for a Conda environment. After a worker restart, `gp`, `gap`, `Singular` and `maxima` resolved to the Sage environment and the interfaces worked.
3. **Sage's preparser makes integer literals Sage `Integer`s.** `json.dumps` refuses them, so the fixture's counts needed `int()`. The `sage` tool's description now says so. It also corrects the claim that results include LaTeX: Sage returns plain text unless `%display latex` is set or `latex()` is called.
4. **Worker logs.** When a client stopped reading an event stream early, the worker logged an error with a traceback ("Cannot write to closing transport"). That is normal and should not be an error; it is passed to the W1 hardening work.
5. **Worker restarts.** The worker was restarted twice with finished executions in its store and started cleanly both times. The adversarial review's crash-on-restart finding needs an execution that was still running, which this lab did not produce.

## Cleanup

- **Removed:**
  - the three sandboxes and the provider;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (2.5 GB, of which 1.5 GB was package cache).
- **Checked afterwards:**
  - no OpenShell, middleware, cache, worker or executor processes;
  - the five lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`;
  - the host's `~/.sage` untouched (last changed 2026-09-28).
- **Kept:** the journal entries of the transient units, and the OpenShell binaries.
