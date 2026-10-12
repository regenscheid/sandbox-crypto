# Worker service

The worker service runs research code for the orchestrator. It is trusted infrastructure, one per worker pool, outside every research sandbox. It starts an executor inside each sandbox and talks to it. The orchestrator reaches it only through the worker API, so the harness has no local execution path ([architecture](ARCHITECTURE.md)). [Execution tools](specs/tools.md) defines the behavior. The code is in `packages/worker` (`sandbox_science.worker`), the shared types in `packages/contracts`, and the orchestrator's client in `packages/orchestrator`.

## Components

```mermaid
flowchart LR
  O[Orchestrator / harness] -->|worker API: HTTP + bearer token| W[Worker service]
  W -->|stdin/stdout JSON lines| E[Executor in the sandbox]
  E --> K[Jupyter kernels]
  E --> S[Shell commands]
  W --> D[(SQLite: executions, events, kernels, artifacts)]
  W --> A[(Artifact blobs by SHA-256)]
```

- **Worker service** (`app.py`, `manager.py`): the HTTP API, idempotent submission, event numbering and persistence, cancellation, watchdogs, file operations and publication.
- **Provisioning** (`provisioner.py`, with `resolution.py`, `permissions.py`, `catalog.py` and the catalog `capabilities.toml`): operations that install a capability or packages into an environment on an agent's request, one at a time in each environment ([provisioning operations](#provisioning-operations)).
- **Executor** (`executor.py`, with `provisioning.py` beside it): two files that depend only on the standard library and, for kernels, `jupyter_client` and `ipykernel`, so the worker can copy them into any sandbox that has Python. The executor runs shell commands in sessions of their own and kernels through the Jupyter protocol (D41), ends what a command leaves running ([leftover processes](#leftover-processes)), and handles the file operations. It offers each language whose kernelspec its interpreter's Jupyter installation finds: `python3` for Python and `sagemath`, SageMath's own kernel, for Sage. Platform code never imports Sage; Sage's kernel preparses Sage syntax itself. `provisioning.py` makes package commands take turns and records what they change ([package commands](#package-commands)).
- **Backends** (`backends.py`) start executors:
  - **`openshell`** copies the executor and its helpers into a configured sandbox with `openshell sandbox upload`, and runs it with `openshell sandbox exec --no-tty --no-login-shell`. Its required `isolation_tier` setting declares the tier the gateway's driver gives: 3 for the VM driver on babbage (D43).
  - **`local`** runs the executor as a plain subprocess of the worker. It has **no isolation**: it declares tier 0, and its probe shows why. It exists for development and tests, and the harness refuses it unless told otherwise (`--allow-unisolated`).

## Worker API

Every route needs `Authorization: Bearer <token>`: the pool's own token, which acts for every project, or a client's, which acts only for the projects its settings list ([clients](#clients-limited-to-projects)). Bodies are the JSON forms of the `sandbox_science.contracts` models. Errors return a `WorkerErrorResponse` carrying an error kind from the spec, with status 400, 403, 404, 409, 422, 500, 503 or 504 as the kind implies. The routes that name an execution, an artifact or a provisioning operation take an optional `project_id` query parameter; when it is given, one of another project is `not_found`.

| Route | Request → response |
| --- | --- |
| `GET /v1/capabilities?environment_id=…` | → `Capabilities`: kernel languages, features (`jobs`, and `pdf` where the environment has pdf_oxide) and the location (pool, backend, sandbox, host, and the [isolation tier](#isolation-tiers) in effect, declared and probed). The ID must be an identifier |
| `POST /v1/executions` | `ShellRequest`, `JobRequest`, `CellRequest` or `PdfRequest` ([PDFs](#pdfs)) → `Execution`. Status is 201 when created, 200 when the project already has the operation ID with the same request, and 409 when it has it with a different one. Operation IDs are kept per project |
| `GET /v1/executions/{id}` | → `Execution`, with its result once terminal |
| `GET /v1/executions/{id}/events?after=N` | → NDJSON stream of `Event`s after sequence number `N`, ending with a `result` event, or at once if the execution has ended and `N` is its last event. While nothing happens, an empty line comes every 15 seconds. No line is longer than `MAX_EVENT_BYTES` (32 MiB) |
| `POST /v1/executions/{id}/cancel` | → `Execution`. A running shell call gets SIGTERM, then SIGKILL; a running cell is interrupted; a queued cell is dropped |
| `GET /v1/jobs?project_id=P&session_id=S&environment_id=E&after=ID&limit=N` | → `JobListing`: jobs in that session, oldest first, at most `N` (default 100, maximum 500); `next_after` is the last returned execution ID, and `truncated` says there is another page |
| `GET /v1/jobs/{id}` | → `JobStatus`: execution, bounded current stdout/stderr head and tail, observed process CPU seconds and resident-memory bytes; finished jobs use their stored result. The optional `project_id` query has the same meaning as on execution routes |
| `POST /v1/kernels/restart` | `KernelRestartRequest` → `KernelInfo`, with the new generation. A cell running on the kernel ends, `cancelled` |
| `POST /v1/sessions/close` | `SessionCloseRequest` → `SessionClosed`: stops the session's kernels in that environment and lists them. Running cells end `cancelled` ("the session was closed"), queued ones never start, and the next cell starts a new generation that reports `state_lost`. Jobs are cancelled and reaped; files and foreground shell calls are untouched, and an environment without an executor starts none |
| `POST /v1/files/read`, `/write`, `/list` | `ReadFileRequest` → `FileContent`; `WriteFileRequest` → `WriteFileResult`; `ListFilesRequest` → `FileListing` |
| `POST /v1/artifacts` | `PublishRequest` → `ArtifactRef` (201). A `media_type` must be `type/subtype`, with optional `; name=value` parameters |
| `GET /v1/artifacts/{id}`, `/content` | → `ArtifactRef`, or the bytes with `X-Content-SHA256`, as a download (`Content-Disposition: attachment`, `nosniff` and a sandboxing content security policy), so no media type turns them into a page |
| `POST /v1/environments` | `EnvironmentRequest` → `EnvironmentInfo`: 201 when created, 200 when the same environment exists, 409 when it exists with another project, profile or policy ([environments](#environments)) |
| `GET /v1/environments/{id}` | → `EnvironmentInfo` |
| `POST /v1/environments/{id}/policy` | `PolicyRevisionRequest` → `EnvironmentInfo` under the new revision |
| `DELETE /v1/environments/{id}` | → `EnvironmentInfo` in state `deleted` |
| `POST /v1/environments/{id}/admissions` | `AdmissionRequest` (up to 32 hosts and ports) → `AdmissionResult`: each host admitted or refused with the reason, and the environment ([ahead of use](#admission-on-demand)) |
| `GET /v1/environments/{id}/revisions?after=N&limit=M` | → `RevisionHistory`: a page of the environment's revisions after revision `N` (default -1), oldest first, each with how its change came and its lockfile ([package commands](#package-commands)): at most `M` (1 to 100, the default) and 4 MiB of JSON. `next_after` is the `after` of the next page, while there is one. It works for any environment that ran something; a client limited to projects sees only its projects' environments, and none of the revisions recorded under an ID before the worker created its environment |
| `GET /v1/environments/{id}/revisions/{n}/lockfile` | → the revision's `Lockfile` as JSON, at most 8 MiB, with `X-Content-SHA256`; `not_found` while it has none |
| `GET /v1/environments/{id}/provisioning?project_id=P` | → `ProvisioningQueue`: the environment's provisioning operations that have not finished, in request order, and whether its packages are `ready`, `changing` or `needs_recovery` ([provisioning operations](#provisioning-operations)). The same access as the revisions route; with `project_id`, an environment that is not that project's is `not_found`, as the harness asks |
| `POST /v1/provisioning` | `ProvisionRequest` → `ProvisionOperation`: 201 when created, 200 for an operation ID the project used with the same request, 409 with another. A capability the catalog lacks is `invalid_request`; an environment the worker does not govern, `unsupported` |
| `GET /v1/provisioning/{id}?after=N&wait=S` | → `ProvisionOperation`. With `after`, it answers once the record's `seq` passes `N`, the operation finishes, or `S` seconds (at most 60) pass |
| `POST /v1/provisioning/{id}/cancel` | → `ProvisionOperation`: a waiting operation ends at once; a running step is cancelled like any execution |

### Clients limited to projects

A pool can serve several orchestrators, each with a token of its own that acts only for some projects (D78). The settings name each client's token by its SHA-256 in a `[[clients]]` entry, never the token itself. `sandbox-science-worker token --out FILE` writes a new token and prints the line the entry takes; `token --sha256 FILE` prints it for an existing token. The pool's own token acts for every project, and no client may have it.

For such a client:

- A request that names another project, in its context or body, is `policy_denied`.
- An execution, artifact, provisioning operation or environment of another project is `not_found`, whatever the route: its state, events, results, files and policy are out of reach, and so is cancelling it.
- The client may use only the environments the worker created for its projects, and not once they are deleted. An environment of another project, or a sandbox made by hand, is `not_found`. Clients therefore need `[openshell.environments]`: only the environments the worker creates are known to belong to a project.
- Its projects may have at most `max_environments` environments at once (8 unless the entry says otherwise), deleted ones aside, since each is a sandbox, a VM with the VM driver. Beyond that, creating one is `policy_denied` until one is deleted. Concurrent requests cannot pass the limit together.
- It may create or revise environments only under the policy revisions an operator granted to the project: `sandbox-science-worker policy add --project p1 policy.yaml`. A revision not granted gets the same `not_found` as one never stored, so a client learns nothing of other projects' revisions, and cannot borrow another project's credential providers by naming its revision.

An environment ID stays with the project that first created it, even once the environment is deleted: creating it for another project is a `conflict`, from any token, with the same answer as for a sandbox made by hand, which names no project. Before an executor runs in an environment's sandbox, the worker checks that the sandbox is still the one it created (its OpenShell ID), and gives the environment up otherwise. Whatever a request was checked against therefore belongs to the same project when it runs, however long it waits for the environment (a revision, a resume, a deletion). For the same reason, the worker never creates an environment under an ID the settings map to a sandbox made by hand, and an ID the worker created never falls back to that map. Only the pool's own token may use sandboxes made by hand; with it, a request that names a worker's environment with another project is a `conflict`. Operation IDs are kept per project, so no project meets, or takes, another's.

Environment IDs are still shared by the whole pool, so a client can take an ID before the project that would use it, and then keep it. It learns nothing of that project and reaches nothing of it: the ID never moves. The harness ends its environment IDs with a random tag (`p1-default-3f9c0a1b2d4e`), so no other orchestrator can predict one. Keeping environment IDs per project is in the backlog, as are limits per client on executions and requests.

Every request that may change something (all but `GET` and the file reads) is logged with the route it matched, the caller's name and the answer's status, never with the path it sent. Closing a session where no environment of the project is left stops nothing and answers with no kernels.

## Behavior worth knowing

- **Durability.** An execution is stored before it is dispatched, and each event before it is delivered. A client that loses its stream asks again with `after`, and receives each event once; a replay reads the store a page at a time. An execution's last two events (its terminal state and its result) and its final record are checked and stored in one transaction.
- **Restarts.** When the worker starts, it reports attempts a previous run left unfinished as `lost`, numbering their last events after the highest stored one. It never runs them again. A record that no longer validates is logged, no longer counted as unfinished, and reads as an `internal` error; it does not stop the start. SIGTERM and SIGINT stop the worker cleanly: executors stop, and attempts still running end as `lost` ("the worker stopped").
- **Untrusted executors.** Research code shares the executor's sandbox and can take it over, so the worker trusts nothing it sends:
  - Each line is decoded on its own: at most 16 MiB and 100,000 objects and arrays, invalid UTF-8 and lone surrogates replaced, non-finite numbers made null. A line that does not decode is dropped and the connection carries on. Events count only from the executor an execution was sent to.
  - Results are checked and bounded by the attempt's output limit (stream texts, outputs, errors, `written_files`), and must survive a round trip through JSON in one event line, or they are shrunk. Malformed replies to file operations are `internal` errors.
  - Each execution may store stream text and rich output up to four times its output limit, and 100,000 events. Past the event count, further events are dropped; past the byte budget, which an honest executor never reaches, the attempt is also cancelled.
  - Whatever happens, an execution ends: an unexpected failure while dispatching it fails it as `internal`, and the watchdog ends one whose executor goes quiet. When reading from an executor ends, for any reason, the worker stops the executor (closing its stdin, then killing it after 15 seconds), as it does every executor when it closes.
- **Event streams.** A stream that fails once it has started is dropped, so the client reconnects; a client leaving a stream is not an error. The harness's client accepts event lines up to `MAX_EVENT_BYTES`, counts a stream with nothing, not even an empty line, for 60 seconds as dropped, and gives up on an execution 300 seconds after its own limit.
- **Output.** A stream event carries at most 65,536 characters. A cell returns at most 10,000 rich outputs, within its output limit; one note stands for those left out. An executor keeps each line under 16 MiB: an end event too large for one line keeps less of its output, marked as truncated.
- **Environment revisions** (W3). An environment's installed packages have a revision: `0` when the worker first sees them, one more for each change.
  - Before and after every attempt the executor takes a fingerprint of the packages installed for its interpreter: Conda's `conda-meta` records and pip's `dist-info` and `egg-info` directories, in the prefix's and the user's site-packages. `conda-meta`'s `history` and `pinned` record requests, not packages, and do not count: an install that links nothing makes no revision.
  - A different fingerprint counts the revision up. Each revision records whether a package command made it through the environment's wrappers (managed) or not (unmanaged), the attempt behind it, and a lockfile ([package commands](#package-commands)).
  - Every execution's location gives the revision it started under, after any wait for a package command, and its result the revision it ended under; `overlapping_change` marks an attempt the packages changed under. The harness tells the model either way.
  - Files the packages own, and installs into other Conda environments under `envs/`, do not count.
- **Kernel generations.** Kernels die with their executor. When the worker starts a new executor for an environment, the next result reports `state_lost`, and the generation keeps counting up from the last one the worker reported. A restart is never hidden.
- **Kernels.** An interrupted cell ends `cancelled` (or `timed_out`), with the kernel's own error, `KeyboardInterrupt` for Python. A cell cancelled while its kernel starts never runs. A restart does not wait for the running cell: that cell ends `cancelled`, and queued cells run on the new kernel.
- **Watchdog.** An execution still running 30 seconds past its own limit is cancelled by the worker. If no result follows within another 60 seconds, it is reported `lost`.
- **Written files.** The executor lists the regular files in the session workspace and the shared area before and after each attempt. The result names those the attempt created or changed (`written_files`, at most 1,000). An attempt running at the same time in the session may contribute some of them. The lists come from the executor, so research code that takes it over can name the wrong attempt.
- **Publication** reads the file through the executor in ranges. The executor hashes the whole file once, with the first range. The ranges must start where asked and add up to exactly the size first reported, within the size limit, the size must not change between ranges, and the stored bytes must hash as first reported. `produced_by` names the last finished attempt that wrote the file. Session files count only that session's attempts, shared files any session's. A file written through the file API has no producer. (Modification times could not do this: in lab 4, the guest kernel's coarse clock gave a cell and the next shell call the same time.)
- **Paths.**
  - Session workspaces are `<project root>/sessions/<session>`; the shared area is `<project root>/shared`.
  - The file tools refuse absolute paths, `..` and paths through symlinks. They open one directory at a time, relative to the last, without following a symlink, the workspace and the shared area themselves included.
  - Reads refuse anything but a regular file without waiting for it (a FIFO, say), and hash in a thread, so a large file does not hold up other sessions.
  - Writes to the shared area need `expected_sha256` or `create_only`.

## Background jobs

A `JobRequest` (`kind: job`) supplies a shell script or argv, a purpose and expected
progress. Submission returns its `Execution` immediately. Its ID, numbered events,
idempotency, cancellation and written files use the same lifecycle as calls. It can run
beside foreground tools and outlive the initiating HTTP connection, turn and orchestrator.
The `jobs` capability advertises this support. Kernels do not run background cells.

`Execution.job` stores the purpose, expected progress, actual time limit and whole-log
paths, `.jobs/<execution ID>/stdout.log` and `stderr.log`, in the session workspace.
Inline and live status output stay bounded; the file API reads whole logs in ranges.
The executor writes chunks in threads through held regular-file descriptors, creating
parents without following symlinks and logs exclusively. A write failure such as a full
disk terminates and reaps the job and reports failure with bounded output and an explicit
warning that whole logs may be incomplete. Logs share the workspace's disk limits;
separate filesystem quota enforcement is still part of the resource-limit work.
Like all session files, research code can change these files; they are research output,
not trusted audit records.

Job limits come from the project's policy and pool ceiling ([policy](POLICY.md#background-job-resources)):
24 hours by default, seven days at most, four active jobs per project/session and eight
per environment. Queued accepted jobs reserve slots before any dispatch waits. A request
above a concurrency bound is refused with active job IDs; explicit time limits above the
maximum are refused. Omit `timeout_seconds` to select the policy default; JSON clients
must preserve omission rather than serialize a default value as an explicit request.
Retrying an operation with the same original request returns its execution even after
policy changes; an omitted timeout and an explicit timeout are different requests.

Closing a session cancels its jobs in that environment, including queued jobs. Deleting
an environment cancels its jobs before dropping its executor, and an environment with
accepted jobs is busy and cannot be suspended as idle. Every direct job cancel has a completion watchdog of its own; session close and deletion
also bound cancel acknowledgment and completion together. In both paths, an executor that cannot confirm them within 30 seconds is disconnected
and affected outcomes become `lost`, rather than blocking lifecycle operations for days.
Package commands take their existing environment turns, and shared files sync before and
after jobs as they do calls. A worker restart still loses in-flight jobs: the executor
currently ends with its stdio connection. Partial whole logs stay at their recorded paths;
the executor daemon is the next slice. `wake_session` records completion intent for the
harness; the worker itself does not start model turns or implement a wake scheduler.

## Leftover processes

A shell call may not leave processes running (tools spec, Shell 8). The executor finds a call's processes through `/proc` and signals them one by one, since OpenShell refuses signals to process groups:
- **Marks.** Every command and kernel runs with `SANDBOX_SCIENCE_OWNER` in its environment, naming the call or kernel. Children inherit it, and `/proc/<pid>/environ` keeps it even after a process unsets it.
- **Adoption.** The executor makes itself a child subreaper (`prctl(PR_SET_CHILD_SUBREAPER)`), so a process whose parent exits stays below the executor instead of going to the sandbox's init. It reaps the adopted processes it kills.
- **Attribution.** A process belongs to a call if it is in the session the call's command began, carries the call's mark, or descends from such a process without a mark of its own. Below the executor, a process with no mark at all has replaced its environment to get away; the next call to end takes it.

When a call ends, times out or is cancelled, everything that belongs to it gets SIGTERM (on a timeout or cancel), then SIGKILL, in a few rounds that catch processes forked meanwhile. Each process's start time is checked again just before its signal, so a process ID reused since the scan is left alone. Kernels and their processes carry their own marks and survive other calls; when a kernel stops or restarts, what it started ends with it. When stdin closes, the executor ends everything below it.

What is guaranteed, and what is not:
- **Guaranteed:** processes that stay in the call's session or keep its mark, wherever in the sandbox's `/proc` they are, including those that left its process group with `setsid` or job control (`set -m`), and orphans of the call.
- **With a subreaper:** also processes that replaced their environment and left the session, as long as they are below the executor. The executor reports in its `hello` whether `prctl` let it become a subreaper, and the worker logs a warning if not.
- **Not guaranteed:** a process that research code deliberately disguises, by copying another owner's mark or, without a subreaper, by clearing its environment and leaving the session. Research code controls its sandbox; these checks catch accidents and ordinary daemons, not a determined attacker, whom only the sandbox contains.

## Isolation tiers

Each backend declares an isolation tier ([threat model](THREAT-MODEL.md#isolation-tiers), D43): `local` declares 0, and `openshell` declares its `isolation_tier` setting, which has no default. Whenever the worker starts an executor, it checks the declared tier against evidence (`isolation.py`):
- The executor reports what it sees in the sandbox, reading files only: the kernel release, the boot ID, the host name, `/etc/machine-id`, `Seccomp`, `NoNewPrivs` and `CapEff` from `/proc/self/status`, its uid, the CPU's `hypervisor` flag, and the DMI vendor and product name. A value it cannot read is unknown.
- The worker reads the same facts on its own host. It also writes a file with a random name in its state directory, and the executor looks for it at the same path.

Evidence only lowers the declared tier, never raises it:
- **Kernel.** The worker host's boot ID means the same running kernel: tier 1 at most, and 1 only if the executor runs under a seccomp filter with no-new-privs and no effective capabilities (the threat model's hardened container), else 0. A boot ID that either side cannot read counts the same way, so tiers 2 and 3 need a kernel of their own.
- **Files.** A sentinel file the sandbox can see, or one it cannot check for, means tier 0.
- **Failure.** If the probe fails, the tier is 0, never the declared one.

Results and capabilities report the tier in effect (`isolation_tier`), the declared tier and the evidence, so a lowered tier says why; the worker also logs a warning. The harness compares only the tier in effect with `--min-isolation-tier`, and refuses a worker that reports no evidence.

What the probe cannot do:
- **Prove tier 3.** Nothing seen from inside one sandbox shows that it has a VM to itself rather than sharing one with other sandboxes. A declared tier 3 stands when nothing contradicts it.
- **Withstand a hostile sandbox.** A compromised sandbox can give the answers a well-isolated one would. Since answers only lower the tier, the probe catches a misconfigured backend, such as a container driver declared as a VM, not a hostile sandbox.
- **See beyond the worker's host.** It compares the sandbox with the worker's host only.

## Running under OpenShell

Labs 4 and 5 ran the worker against a real gateway ([lab 4](../.agents/docs/agent-reports/babbage-openshell-lab-4-2026-10-11.md), [lab 5](../.agents/docs/agent-reports/babbage-openshell-lab-5-2026-10-11.md)). OpenShell v0.1.3's seccomp filter refuses two kinds of call that ordinary software makes. The worker works around both:
- **Scheduler calls on a thread.** libzmq aborts when `pthread_setschedparam` fails, so no Jupyter kernel or client could start.
  - The worker builds a small shim (`shim/sched_self.c`) with the host's C compiler (`cc`), uploads it beside the executor and preloads it with `LD_PRELOAD`. Every process the executor starts inherits it.
  - For the calling thread, the shim makes the equivalent `sched_setscheduler(0, …)`, which the filter allows. It needs only glibc 2.2.5 symbols.
  - The `sched_shim` setting turns it off.
- **Signals to process groups.** `kill(-pgid)` is refused. The executor never signals a group: it signals each process of a call, found through `/proc` ([leftover processes](#leftover-processes)).

The executor also calls `prctl` twice at startup:
- **Subreaper.** It becomes a child subreaper. OpenShell allows this (lab 8).
- **Not dumpable.** It tries to make itself not dumpable, which closes its `/proc` files (`fd`, `environ`, `mem`) and ptrace to research code running as the same user, so research code cannot write into the protocol or read other sessions' requests.
  - OpenShell's filter allows the call, but its supervisor then refuses every socket call from the process, even a loopback or Unix socket. That stops kernels from starting, since the executor is their client (lab 8).
  - So the executor checks that it can still bind a loopback socket, and otherwise becomes dumpable again.
  - Under OpenShell v0.1.3 it therefore stays dumpable, and research code in the same environment can reach its file descriptors. The worker treats everything the executor sends as untrusted, and sessions that share an environment are not isolated from each other anyway ([projects and sessions](PROJECTS-AND-SESSIONS.md)).
  - Its `hello` reports both results (`subreaper`, `not_dumpable`), and the worker logs when either is missing.

Other things to know:
- **Providers.** A sandbox compiled from a policy that names providers needs those providers attached; OpenShell refuses the policy otherwise.
- **Devices.** OpenShell's Landlock baseline does not include pseudo-terminals or `/dev/shm`, so without them Sage's pexpect interfaces and Python's `multiprocessing` fail. The compiled sandbox policy adds them ([policy](POLICY.md), "Mapping to enforcement"; [lab 5](../.agents/docs/agent-reports/babbage-openshell-lab-5-2026-10-11.md)).
- **Tools on `PATH`.** The executor puts its interpreter's directory first on `PATH` for commands and kernels, behind only the [package wrappers](#package-commands), and sets `CONDA_PREFIX` for a Conda environment, so an environment's programs (`gp`, `gap`, compilers) are found.
- **Kernel environment.** The sandbox's Python environment needs `ipykernel` and `jupyter_client`. In lab 4 they came from conda-forge through the package cache, in 16 s. For Sage, the same environment needs SageMath, which installs the `sagemath` kernelspec. In lab 5, SageMath 10.9 (386 packages, 1.45 GB) installed in about 30 s and its kernel started in 2.3 s; a kernel gets 120 s to start.
- **Latency.** Median round trips through the worker API were 14 ms for a shell call, 20 ms for a Python cell and 36 ms for a Sage cell.
- **Performance.** Single-threaded mathematics (PARI, FLINT, M4RI, Singular, fpylll and plain Python) ran 0–7% slower in the VM sandbox than on the host, typically 3% (lab 5).

Both restrictions are described for upstream in `.agents/upstream/`.

## Environments

With an `[openshell.environments]` table, the worker creates the sandboxes behind logical environments itself (D73). The orchestrator holds no gateway credentials (threat model, Z2), so it asks the worker, naming an environment, a project, a base profile (D46) and a policy revision. The standard profiles' images, the interpreter they provide and the filesystem paths the targets file must list for them are in [base profile images](IMAGES.md).

- **Policy revisions** come from the pool's policy store, the policy middleware's state directory. Only an operator adds them, on the pool host:

  ```bash
  sandbox-science-worker policy add --settings worker.toml [--project p1] policy.yaml
  ```

  The command compiles each file to check it, then prints its revision (`sha256:…`) and the compiler's findings. The API cannot add a revision, so a compromised orchestrator can only choose among the stored ones, and a client limited to projects only among those granted to its projects (`--project`, [clients](#clients-limited-to-projects)).
- **Creating** an environment:
  1. The worker compiles the revision under the pool ceiling, with the deployment's targets file.
  2. It checks the providers. Every provider in `credentials.providers` must be registered in the gateway under the profile the policy names, and that profile must name no endpoints (D57). Otherwise it reports `unavailable`.
  3. It records the environment as `creating`, then creates a sandbox with a new random name, `ss-` and 16 hex digits, which the record keeps; no two records name one sandbox. (OpenShell takes at most 19 characters, too few for a name derived from the ID to be unique: a client could have chosen an ID whose name was another project's sandbox.) Creating an environment again deletes only the sandbox its own record left behind in a crash. The sandbox comes from the profile's image, with exactly those providers (`--no-auto-providers`) and the compiled document, filesystem section included.
  4. It waits until the sandbox is ready, then binds it to the revision in the middleware's store.

  If any step fails, the worker deletes the sandbox and records the environment as `deleted`. A sandbox left by a crash during creation is replaced on the next request.
- **Revising:**
  1. The worker compiles the new revision, deciding again every host it decided on demand under the old one ([admission on demand](#admission-on-demand)). It refuses a revision whose providers differ, since OpenShell attaches providers only when it creates a sandbox.
  2. It reads the sandbox's live filesystem section (`openshell policy get --base`), because OpenShell refuses a revision that removes any path from it.
  3. It applies the new document with `openshell policy set --wait`, then rebinds. Between those two steps the middleware denies the sandbox's requests as `policy_stale`, so the change fails closed.
- **Deleting:** the worker stops the environment's executor (running attempts end as `lost`), deletes the sandbox and removes its binding.

Sandboxes made outside the worker still work through the static `sandboxes` map.

## Admission on demand

A sandbox resolves only the host names its compiled policy names. A policy with `*` rules or a non-deny default reaches hosts nobody names in advance, so the worker admits them as they are needed (D69; [policy](POLICY.md), "Mapping to enforcement"):

1. Research code connects to a host the policy does not name. The lookup returns a placeholder address and OpenShell refuses the connection, which fails with `Permission denied`.
2. OpenShell drafts a proposal for that program, host and port. Drafts reach the gateway about every 10 s.
3. Every `admission_interval`, the worker lists each ready environment's pending drafts (`openshell rule get --status pending`) and decides each host and port with the policy library (`admission_refusal`):
   - the policy must reach hosts it does not name;
   - the host must be public and not a platform service host;
   - the port must be one the compiled policy admits;
   - some HTTP or git request to the host there must be allowed or escalated, after the hard floor (with the settings' `admin_hosts`), the pool ceiling and the reserved hosts.
4. It names the admitted hosts in one new compilation of the environment's revision, and applies the result with `openshell policy set --wait`. The policy middleware still decides every request.
   - Each admitted host gets an endpoint limited to the public address ranges, less the addresses the library always denies ([policy](POLICY.md), "Names, not addresses"). So a name that resolves to a private, metadata or administration address stays refused.
   - Hosts already in use keep their place, so a new batch or a revision never displaces them.
5. It rejects every draft it read, giving its decision as the reason, and records the decisions. It never approves a draft: a draft's rule is raw TCP for one program, which the fail-closed middleware could not inspect.

**Ahead of use.** The orchestrator can name hosts before code contacts them, so that the first connection does not fail and the reload comes before any transfer starts. `POST /v1/environments/{id}/admissions` decides up to 32 hosts and ports the same way, in one compilation, after starting a suspended environment, and answers with each decision. A host the policy names already is admitted without a reload. Capabilities list the `admission` feature for environments where this works, and the harness then offers the agent the `admit_hosts` tool ([execution tools](specs/tools.md#network)).

The environment's `admitted_hosts` lists the hosts admitted so far, and `sandbox-science-worker admissions --settings worker.toml --environment ID` prints every decision with its reason. A revision decides every recorded host again. Hosts the new policy no longer reaches are dropped, and hosts it now reaches are admitted.

Things to know:
- **Timing.** In [lab 10](../.agents/docs/agent-reports/babbage-openshell-lab-10-2026-10-11.md), a host was reachable 9 to 20 s after the first refused connection. OpenShell's 10 s draft flush and the sandbox's 10 s policy poll account for most of it. Research code, or the agent, retries.
- **Notices.** Each result carries what admission decided for its environment since the session's previous result (`notices`): which hosts were admitted, and which were refused and why. The harness passes them to the model as `platform_notices`. A host admitted again for another program is no news.
- **Reloads interrupt transfers.** When a sandbox loads a new compilation, OpenShell closes every connection opened under the old one, so an admission cuts downloads in progress.
- **Limits.**
  - An environment admits at most `max_admitted_hosts` hosts (64 by default). Each adds an endpoint of about 4 KiB of YAML, and OpenShell limits a sandbox's policy to 256 KiB.
  - Beyond that limit, or once the compiled policy would pass 320 KiB of YAML, new hosts are refused.
  - One check of an environment handles at most 32 drafts and 64 endpoints; real drafts beyond that wait for the next check.
  - The worker keeps at most 512 decisions per environment: every admitted one, then the newest refusals. It forgets them when the environment is deleted.
- **Failures.**
  - If an environment's check fails, the worker leaves its drafts pending and skips it for a minute. A failure in one environment never stops the others.
  - If the same batch fails three times because OpenShell refuses the revision, its hosts are refused with the error as the reason. A later revision of the policy decides them again.
- **Untrusted text.**
  - At v0.1.3, `openshell rule get` has no JSON output, so the worker reads its text.
  - The sandbox chooses the host names and program paths in it. A crafted path can add hosts to decide, which the sandbox could have tried anyway, or hide its own draft.
  - Such text is made printable before it reaches records, logs or the reasons OpenShell stores.
- **Leave OpenShell's approval mode manual.** With `proposal_approval_mode` set to `auto`, OpenShell would approve drafts itself and add its own raw-TCP rules. The fail-closed middleware would deny their traffic, but the policy would no longer be the worker's alone.

## Idle suspension

Environments persist, but need not keep running ([projects and sessions](PROJECTS-AND-SESSIONS.md), "Provisioning and persistence"). With `suspend_after` set, the worker stops the sandbox of an environment nobody is using, and starts it again on the next use (W3):

- **Idle** means three things for `suspend_after` seconds:
  - no execution ran there, and no other request was in flight (file operations, publication, kernel restarts, closing a session);
  - the environment did not change: it was not created, resumed, revised or given admitted hosts;
  - its kernels used almost no CPU: under 2% of one core, sampled from the executor (`usage`), so a computation left running in a kernel thread keeps the environment up. Two samples from the same executor are needed, and a counter that went down tells nothing.
- **Not seen as use:** work that waits rather than computes (a download, a remote job being polled), and the CPU of children a process reaps automatically by ignoring `SIGCHLD`. Such work ends with the suspension; the next cell reports the lost kernel state.
- **Suspending:**
  1. The worker checks again, under the execution manager's lock, that nothing started meanwhile. It forgets the executor before closing it, so a request arriving during the close starts afresh instead of using an executor nobody tracks.
  2. It records the environment as `stopped`, then runs `openshell sandbox stop`.
  3. OpenShell keeps a stopped sandbox's writable disk: the session workspaces, the shared area and installed tools survive. Its identity and policy survive too, including hosts admitted on demand and the middleware's binding.
  4. Kernels do not survive. The next cell in a session that had a kernel reports `state_lost`, as after any executor restart.
- **Resuming.** Any use of a stopped environment first resumes it: an execution, a file operation, capabilities, `create` with the same request, or a policy revision.
  - The worker reads the sandbox's phase. If a stop failed or was cut short, the sandbox is still ready, and it carries on. Otherwise it runs `openshell sandbox start` and waits up to three minutes for the sandbox to be ready.
  - Phases that cannot lead there (`Error`, `Deleting`, `Completed`) fail at once, and after a failed start the next minute's uses fail at once too.
  - A request that arrives while a suspension is stopping the sandbox waits for it, then starts it again.
  - If the sandbox is gone, or comes back with another ID, the worker stops it, records the environment as `deleted` and asks for it to be created again.
- **Cancelling.** A cancel goes only to the executor its execution was sent to, so it never starts a sandbox or an executor.
- **Trust.** The executor reports the CPU time, and research code may take the executor over. It can keep its own environment running, or let it be suspended, and nothing else:
  - each sample has a 15 s limit, and environments are sampled eight at a time;
  - a request waits at most 30 s to be written, so an executor that stopped reading its requests holds up no other environment.

## Shared data across environments

Each environment is its own sandbox, so the `shared/` area of a project's sessions is a separate directory in each of the project's environments. The worker keeps them in step (D79): it holds the project's copy of every shared file, in a blob store of the project's own, and syncs each environment with it.

- **After every call** in an environment (a shell call, a cell, a PDF operation), it copies the files that changed in that environment's `shared/` into the project's copy, before the call's result is reported. A later call anywhere in the project then sees them. Files the call deleted are deleted in the project's copy.
- **Steps are bounded.** A step starts no transfer 40 seconds after it got its locks, or after 1 GiB read, and all of one sync's steps stop starting transfers after 100 seconds, within the manager's 120. A step records what it copied in batches as it goes, so a large change arrives over several calls instead of never, and environments that wait for one another take turns.
- **Before every call**, it brings the environment's own unsynced changes up first, then writes into it the files whose project version is newer than its copy, and removes the ones the project deleted.
- **File requests** in `shared/` sync the same way. Reads and listings bring the environment up to date first. A write is made under the project's lock, after the environment was brought up to date, and goes up at once, so its `expected_sha256` names the project's version: of two writers in different environments expecting the same version, the second gets `conflict`. If the sync before a write fails, or leaves that file out of date, the write is not made (`unavailable`). What these syncs find is passed on with the results of the calls running in the environment, or with the requesting session's next result there.
- **Conflicts.** Every change to the project's copy takes the next value of a counter as its version, and each environment's record says which version its copy matches. A change an environment makes to a file another environment changed since is a conflict. The project's file keeps the version that arrived first, and the later one is kept beside it as a conflict copy named after its environment, `results.conflict-<environment>.csv` (shortened to fit 255 bytes). The environment's own file is renamed to that name in place, keeping its inode, so a call still writing to it goes on writing into the conflict copy; the project's version then takes the name at once. The call's result tells the agent (`notices`, which the harness passes on as `platform_notices`). A deletion that meets a change loses to it.
- **Files in use.** A file that a process in the environment has open for writing, or maps writable, is not replaced or removed until it is closed; the agent is told once, and a file request's write to it is refused with the reason. Readers do not hold a file back. The executor checks through `/proc` just before replacing or removing a file and again just after, putting it back if a writer appeared in between. Processes it may not inspect, such as non-dumpable ones, are not seen. In [lab 17](../.agents/docs/agent-reports/babbage-openshell-lab-17-2026-10-11.md), the swap the executor uses (`renameat2` with `RENAME_EXCHANGE`) worked on the VM driver's overlay file system, and a kernel holding a file open for writing, or mapped shared and writable, kept the project's newer version out until it let go.
- **What an executor reports is checked.** Research code controls `shared/`, so paths must be plain paths below it, stamps and digests must be well formed, and a digest is never used as a path. Content is read from the environment and kept only if the bytes received hash to the digest it reported, unless the same project already holds that content: a digest of another project's file gets nothing.
- **An environment's directory is recognized by a mark**, `.sandbox-science-sync` beside `shared/`, which the worker writes. A sandbox made again under a known ID, or a mark removed, makes the environment's records unverified: nothing in it reads as deleted, files it lacks are written into it, the files it has are hashed again, and the deletions the project made still apply, so nothing deleted comes back. An environment's `shared/` is synced for the first project that uses it; another project using the same sandbox made by hand (with the pool's own token) is told it is not synced, and its sync leaves the directory alone.
- **Scans that miss files prove nothing.** A scan cut short (past 20,000 files or 4 MiB of paths) or with parts it could not read infers no deletions, and the agent is told once. A deletion the project made still reaches such an environment, since a file is removed only if its content is the one synced there. Names that cannot be synced (not text, a name over 255 bytes, a path over 4096 bytes) are left out and named once; they stop nothing else.
- **Cost.** The executor reports each file's stamp (modification time, size, inode); only files whose stamps changed are hashed and copied, and the executor gives a digest only for a file still as scanned. A file recorded within two seconds of its last change is hashed again at the next scan, since a clock coarser than a rewrite could leave a stamp unchanged. A scan hashes up to 256 MiB, and the rest waits for the next. A call that changes nothing in `shared/` costs two scans.
- **Limits.** Files over 32 MiB are not copied, and are not hashed again until they change. Changes that would take the project's copy past 2 GiB or 10,000 files are not copied either, and are not read: the size a scan reports is checked first, and a file larger than reported is dropped as soon as it is read. The agent is told once for each. Publish large files as artifacts.
- **Failures.** A file that cannot be written into an environment, such as one whose name is a directory there or one in a read-only directory, is tried again after ten minutes, and the agent is told once; the other files go on. Only a failing executor or a full disk stops a step. Files the sync writes into an environment while a call runs there, and that the call leaves as they were, are not counted among its `written_files`.
- **Storage.** Blobs are on disk before they are recorded, and checked against their digest whenever they are read; a damaged or missing one is not copied, and is read again from an environment that has the content. One that cannot be read for the moment (too many open files, say) is tried again later and kept. Blobs that no shared file uses any more are deleted at once; one that a cut-short step left is deleted at the project's next step, or when the worker next starts. The records are in `shared.sqlite3` in the state directory, and each project's blobs in `shared/<hash of the project ID>/`.
- **What it does not do.** It copies at call boundaries, so a file another session in the same environment is still writing can be copied in part, and is copied again when that session's call ends. The executable bit travels with a file's content; a change of the bit alone is not synced. Removing a file also removes the directories it leaves empty.

Apart from file requests' writes, a failed sync never fails the call: the call runs with `shared/` as it is, and its result says the sync failed. If the executor goes away while the shared files sync after a call, the call keeps the result it reported. The setting `shared_sync = false` turns the sync off.

## Package commands

Package managers run from a shell call or a kernel take turns in their environment, executions wait while one changes it, and each change becomes a revision with a lockfile ([Conda provisioning](specs/conda-provisioning.md), CP-4, CP-11, CP-12, CP-16, CP-18). The sandbox's side is `provisioning.py`, beside the executor; the worker's is `revisions.py`.

- **Wrappers.** The executor writes a wrapper for each of `pip`, `pip3`, `pip3.X`, `uv`, `conda`, `mamba` and `micromamba` that is on `PATH`, and puts the wrappers first on `PATH` for commands and kernels. It does so when it starts and again before each attempt, so a manager installed meanwhile gets one and a manager that went loses its own: `command -v` still tells whether a manager is there. `CONDA_EXE` and `MAMBA_EXE` name the wrappers unless they are set, since IPython's `%conda`, `%mamba` and `%micromamba` run what they name.
  - The wrappers are in `bin` of a directory laid out like the environment's prefix, in the executor's state directory (`<project root>/.executor/provisioning/prefix`). Each of the prefix's other entries is a link to the prefix's own. `bin` also holds a link to each of the prefix's programs but the package managers and the Pythons, which have stand-ins that run the prefix's own; a link whose target is gone goes, and a program a package command installs gets its link when the command ends. So what scripts look for beside a manager or its Python is there: `. "$(dirname "$(command -v conda)")/../etc/profile.d/conda.sh"`, `"$(dirname "$(command -v pip)")/python"` and `"$(dirname "$(command -v python)")/jupyter"`.
  - The Pythons stay stand-ins, short shell scripts: a Python started by a link would take the link's directory for its own, and record it as `sys.executable` in what it installs. So `command -v python` names a script, which `readlink -f` resolves to itself, and `$(dirname "$(dirname "$(command -v python)")")` names the directory around the wrappers, not the prefix: its `bin` is the wrappers', and a `--prefix` install there would put programs beside them. `$CONDA_PREFIX`, or `python -c 'import sys; print(sys.prefix)'`, names the prefix.
- **The hook.** A `sitecustomize.py` first on `PYTHONPATH` sees the environment's Python start with `-m pip`, `-m uv` or `-m conda`, as `python -m pip` and IPython's `%pip` and `%uv` do. That process then stands in for a wrapper: it runs the same command line as its child and ends as the child did. It never runs the module itself, since `python -m uv` replaces its process with the `uv` program. The environment's own `sitecustomize`, if any, still runs. Each Python start pays about 0.4 ms for the hook.
- **What takes a turn.** A command that may change the environment: one with an argument `install` or `uninstall` for pip; `pip` and one of `install`, `uninstall` or `sync` for uv; `install`, `update`, `upgrade`, `remove` or `uninstall` for conda, mamba and micromamba. An option's value that happens to be one of these words costs a turn, never a missed change. Any other command (`pip list`, `pip --version`, `uv sync` of a project's own `.venv`, `conda info`) runs at once, without the lock, for about 0.2 ms more than without the wrapper, and so does a dry run (`--dry-run`), which only reports what it would change, as the provisioning tool's resolve step does.
- **Turns.** A turn holds an exclusive lock (`flock`) on the state directory's `lock`. A command that finds it held prints `sandbox-science: waiting for a package command to finish in this environment: pip install sympy (pid 812, for 4 s)` on stderr and waits; waiters go in the order they came, as far as the kernel's file locks keep it. A command started inside a turn, such as `python -m pip` run by pip's build isolation, runs within that turn instead of waiting for itself. A turn costs some 40 ms besides the command.
  - The wrapper runs the real command as its child, with the same arguments, standard streams, signal mask and signal dispositions, and exits with its exit code, or by the signal that ended it, SIGKILL included.
  - It passes on the signals it gets (hangup, interrupt, quit, terminate, the user signals, alarm and window size). Off a terminal, the command has a process group of its own, so a signal sent to the wrapper's group, as a kernel interrupt is (`killpg`), reaches the command once, from the wrapper. On a terminal, the command stays in the foreground group, to read from the terminal, and the wrapper does not pass on what the kernel sent to the whole group, as a terminal does, which reached the command already.
  - When the executor stops a shell call (a cancellation, its time limit), it sends SIGTERM to each of the call's processes but a command whose wrapper is among them, so the command gets one SIGTERM, from its wrapper, and its clean-up (conda's rollback) is not cut short by a second. The 2 s before SIGKILL count for all of the call's processes, not only its shell, which may end at once and leave the others; a package command still in its turn gets 10 s.
  - The command dies with its wrapper (`PR_SET_PDEATHSIG`): a wrapper killed with SIGKILL, as `subprocess.run` kills its child on a timeout or an interrupt, leaves no command running outside its turn. What the command started, such as pip's build backend or conda's post-link script, carries the turn's mark (`SANDBOX_SCIENCE_PACKAGE_LOCK`): the next turn, or the executor's next look, ends what a killed wrapper's turn left running, and a turn that ends normally gives what its command left 2 s, then ends it. The lock's descriptor stays with the wrapper, since a process the command left running in the background would otherwise hold the lock for as long as it ran.
- **Executions wait while packages link** (CP-12). Before an attempt starts, the executor waits while a turn is held: the shell call or cell stays `queued`, and can be cancelled. The wait counts against the attempt's time limit; an attempt still waiting at its limit ends `timed_out` without running, and its error names the command it waited for. Attempts already running are not held up.
- **Revisions.** A turn ends with a record in the state directory's journal: the command, the mark of the call or kernel that ran it (`SANDBOX_SCIENCE_OWNER`), when it got its turn and ended, its exit status, and the packages' fingerprint before and after, with a lockfile when they changed. When an attempt starts and when it ends, the executor reads the journal and the packages under a shared lock, and sends the worker a `revision` event for each change, in order, before any event that reports its fingerprint.
  - The records of one attempt's commands, one after another, make one managed revision, set against that attempt, with its last 8 commands and the number of those before (a loop of 70 `pip install` makes one revision).
  - A change that no record accounts for is unmanaged (CP-4), set against the attempt after which it was seen; at an attempt's start, against none. Research code running `/opt/sandbox-science/bin/pip` by its full path makes one, and so does a change a command finds before it runs.
  - A record leaves the journal once the worker has its event, so the next look reports the records of an attempt cancelled while it looked. A look reads at most 256 records, the oldest first, and an attempt's start or end looks up to 8 times; records beyond wait for the next attempt.
  - A result's `overlapping_change` says that the revision changed while the attempt ran, or that a package command was still running when it ended. While a turn is held, an attempt that ends reports the revision as it was before that turn.
  - A command line is kept as the record has it, credentials replaced by `***`: the user information of URLs (`--index-url=https://***@private.example/simple`), which runs to the `@host` after a password with a `/`, `?` or `#` in it; anaconda.org's token (`/t/***/`) and the token that Cloudsmith's and Gemfury's index URLs take as their first path segment; and the values of URL parameters and options that name a token, key, secret or password. Control characters become `?`; at most 32 arguments of 512 characters and 4,096 in all are kept, and a note for any beyond. The worker bounds and redacts it again. What runs is never changed.
  - The executor reads records and package metadata as `op_read` reads files: regular files only, without following a symlink or waiting for a FIFO.
- **Lockfiles** (CP-18) are JSON (`Lockfile` in `packages/contracts`):
  - each Conda package, from `conda-meta`, by name, version, build, channel, subdir, file name, SHA-256, MD5 and license. Channels are named, never addressed: `https://conda.anaconda.org/conda-forge` and the package cache's `<cache>/conda/conda-forge` ([URL layout](PACKAGE-CACHE.md#url-layout)) are both `conda-forge`. Any other channel keeps its URL, laid out like the cache or not, without user information, an anaconda.org token, query or fragment, and with its host in ASCII: a look-alike host shows as the `xn--` name it is;
  - each distribution pip or uv installed, from `dist-info` and `egg-info`, by name, version, installer and place (the prefix or the user's site-packages); for a direct-URL install also its URL, cleaned the same way, and its SHA-256 where the installer recorded it. A file that came through the cache is named by its files.pythonhosted.org URL. Distributions a Conda package installed are left out, since its Conda entry stands for them;
  - `skipped`, the records left out because their name, version or build cannot be held.
  - Every field has a pattern: printable ASCII, without the spaces, line breaks or path separators a value could use to pass for another.
  - The executor names conda.anaconda.org's channels; the worker names the cache's, from its `package_cache` setting, whatever the executor wrote. Hosts compare in one spelling: lower case, without a trailing dot, an IP address in its one form (`167772165` and `0xa.0.0.5` are `10.0.0.5`). It refuses a lockfile that fails a pattern, holds a URL that does not parse, or names the cache's host in any other way, and, when the cache's URL has a port, one that holds its `host:port` anywhere.
- **Storage.** The worker checks, names and hashes a lockfile in a thread, never on its event loop, and stores it in a store of its own (`<state_dir>/lockfiles`) by the SHA-256 of its JSON, up to 8 MiB.
  - Its shape comes first: known keys only, at most 20,000 entries in each section, each an object of known keys. Only then are values checked, an entry at a time, so that a hostile lockfile costs little memory to refuse and holds the interpreter (CPython's GIL) only briefly at a time. A failure is logged by the entry, its count of errors and the first.
  - The executor sends a lockfile with each attempt's last change. The worker keeps one for each attempt and checks at most 4 for each execution, those it asks for when the execution starts and ends included. A revision left without one gets the current lockfile asked for, in the background (`lockfile`), while it is current; revision 0 gets its own when the first execution starts.
  - Each environment keeps its newest 500 revisions, and lockfiles for its newest 50 in 64 MiB at most. A lockfile no revision names is deleted, and a worker deletes those a crash left when it starts.
  - Deleting an environment, or the worker giving one up because its sandbox is gone or came back as another, deletes its revisions and their lockfiles; and when the worker creates an environment, it forgets the revisions recorded under its ID before (D78): a sandbox made by hand, or an earlier environment of the ID, had them. The store thus holds at most an environment's share for each environment the worker keeps. There is no quota across environments: one would let an environment's executor push out, or shut out, another project's lockfiles.
- **On babbage.** [Lab 17](../.agents/docs/agent-reports/babbage-openshell-lab-17-2026-10-11.md) ran package commands in OpenShell VM sandboxes with the real pip and micromamba through the package cache: turns, the wait before an attempt and its time limit, cancellation, `%pip` in a kernel through the hook, the death signal under OpenShell's seccomp filter, and lockfiles that name `conda-forge`, never the cache.
- **Upgrading.** Contracts refuse fields they do not know, so an orchestrator older than this change refuses a result that carries `overlapping_change`. Upgrade the orchestrators before the workers.

What it does not do:
- **Bypasses.** A manager run by its full path, from a `PATH` without the wrappers, by a Python started with `-I` or `-E` (which ignore `PYTHONPATH`), or as the `conda` shell function of a sourced `conda.sh` (which points `CONDA_EXE` at the real binary), takes no turn. Its change shows as unmanaged after the attempt. Only the prefix's own Python is hooked: a venv's Python changes its venv, not the environment.
- **Attribution.** With attempts running side by side, an unmanaged change counts against the first attempt to see it. A record names the attempt from the mark of its call or kernel, which research code can copy, and research code can write records itself. The lock serializes cooperating commands within one sandbox; it is no boundary, and the records are as trustworthy as the sandbox. The worker bounds what it accepts: 64 revisions an execution, past which the state it leaves is one revision of unknown cause, and lockfiles that validate.
- **Not yet.** A kernel's own start revision, and telling the agent which kernels predate a change (CP-12). Rebuilding an environment from a lockfile (W3). Keeping every lockfile that a recorded result's lineage names, whatever the caps, once results record their lineage (the user's decision of 2026-10-11). The provisioning tool is the next section's.

## Provisioning operations

Runtime evidence: [lab 18](../.agents/docs/agent-reports/babbage-openshell-lab-18-2026-10-12.md) ran the provisioning tool with real micromamba and pip, the middleware and cache, and independent OpenShell VMs. Its dry runs passed the parsers, installs matched their decided files and lockfiles, an escalated install downloaded exactly its approved set, and the grant ended with the operation. All nine catalog probes passed. Cancellation and a worker restart after linking began both exposed `needs_recovery`; restoration remains unimplemented.

An agent asks for a capability or for packages, and the worker decides, installs in the environment's turn, records and probes, telling the agent what happened ([Conda provisioning](specs/conda-provisioning.md), CP-1 to CP-25). The code is `provisioner.py`, with `resolution.py` (commands, parsing and decisions), `permissions.py` (permission requests) and `catalog.py`; the harness's tools are `provision` and `provision_status` (`packages/orchestrator`, `provision.py`).

- **Which environments.** The worker provisions the environments it created ([environments](#environments)), under their stored policy revision. Their capabilities list the `provisioning` feature, and the harness then offers the tools. An environment of a sandbox made by hand is `unsupported`.
- **The request** (`ProvisionRequest`) names the target environment (the session's, unless the agent names another of the project's), the purpose, and either a capability of the catalog or package specs: Conda specs, or pip specs only with the reason conda-forge lacks them (CP-15), and system packages, which are always refused. Conda and pip specs come in separate requests, the Conda ones first: pip then resolves against the Conda packages as installed, and brings its own copy of one only where a version constraint demands it, never as an unseen dependency of a package installed beside them. A pip name that a Conda package installed counts as installed. A spec is a channel, a name and a version constraint, or a PEP 508 requirement without a URL or markers: nothing that a package manager could read as an option, a file or a URL, and nothing ending like a package file (`.conda`, `.whl`, `.tar.gz` and the like). Operation IDs are kept per project, as for executions. An environment of another project is `not_found`, whatever the token, so that an agent naming one learns nothing of it.
- **The catalog** (`capabilities.toml`, CP-3) maps each capability to conda-forge specs and probes: a command run as an execution, which passes only on its expected output. It ships with the worker and is reviewed like code; only the platform extends it for now (open question 6). Each operation records its version and the file's SHA-256. Run on babbage outside any sandbox, in a conda-forge environment with SageMath 10.9, PARI 2.17.3, GAP 4.15.1 and fpylll 0.6.4, the probes of gp, gap, sage, sympy, fpylll, networkx, numpy and pandoc pass; python-flint's is still to check.
- **One at a time.** Operations on an environment run in the order they were requested, each waiting its turn in `queued`, with its place in the queue (CP-11). An operation waiting for permission keeps its turn, so the ones behind it wait too; cancelling it lets them go. An environment has at most 32 operations that have not finished; more are `unavailable` until some end. Operations on different environments are independent. Shell installs still take the sandbox's own turn (CP-4); the install step takes it too.
- **The steps** are ordinary executions in the operation's context, with operation IDs of the worker's own (`prv-<hex>.<step>`, which clients may not use) and `origin` naming the operation, so the harness can read and cancel each like any other:
  1. **Check** (a capability): if its probes passed at the current revision, the operation ends `already_satisfied`, citing those records; otherwise the probes run now, and pass or not (CP-2).
  2. **Refusals before any dry run**: system packages (`base_image_change_required`, CP-5), before the check too; a channel the policy does not list, or an ecosystem whose index reads it denies (CP-6, CP-F4).
  3. **Resolve** (CP-7): `micromamba install --dry-run --json`, or `pip install --dry-run --only-binary :all: --report -`, in the sandbox, through the package cache, under the environment's policy. pip takes wheels only, whose metadata the index serves, so no source distribution is built, which would run its code; a package with no wheel cannot be installed through the tool yet. A dry run (`install --dry-run`, as its first words) takes no turn. The worker reads the files to link, each with channel (named as lockfiles name it, never by the cache's address), subdir, file name, version, build, SHA-256 and declared license, and its path at the cache, in a thread, at most 2,000 files. Names and paths must agree, since the permission request grants the paths while people read the names: a Conda file at the cache is `/conda/<channel>/<subdir>/<name>-<version>-<build>.conda` (or `.tar.bz2`), and a wheel is `/pypi/files/<project>/<sha256>/…` with its own project and SHA-256; anything else fails the resolve. Nothing to install ends `already_satisfied` with a version check of each requested package, or `failed` at `probe` when a check does not pass or a capability's probes fail.
  4. **Decide** (CP-6, CP-9): each file's path at the cache is classified and evaluated with the policy library, as the policy middleware will decide its download; a path that is not the package file its record names is denied. A package declaring the GNU Affero GPL is refused, naming the package and its license, unless the policy's `packages.agpl_exceptions` names it (open question 5). A file the policy denies is refused with the reason.
  5. **Permission** (CP-8): when the policy escalates any file, the worker records one permission request for the whole resolved set in the policy middleware's event store, and waits in `awaiting_permission`. The operator reviews it with `sandbox-science-policy-middleware pending --request N`, which shows what the agent asked for and why, and every file, and answers it like any other (D76): `approve --request N`. The middleware then lets exactly those files download to the environment's sandbox under that policy revision; anything else escalated is still refused. A denial, or no answer within the escalation timeout (counted by the worker, whether or not it can read the store), ends the operation `denied`, and nothing has downloaded. However an operation ends, the worker then withdraws its request: a denial marked `withdrawn`, built from the request's key, after which nothing grants its files again (a late approval neither, which the command line refuses). A withdrawal that fails is owed: the next operation to end tries it again, and a starting worker withdraws every open request of the operations it knows, those a crash or a stop left before the operation noted its request included. In any case the request names its own deadline (`valid_until`: the wait, the install's time limit and 30 minutes), after which it grants nothing. A grant therefore lasts until its operation ends, the approval expires or that deadline passes, whichever comes first.
  6. **Install** (CP-10, CP-14): `micromamba install` with the request's specs, pinned to exactly the decided files, or `pip install --require-hashes --no-deps --only-binary :all:` with exactly the resolved distributions, in the environment's turn, under the policy's wall time (its `resources.wall_time`, under the pool's cap; one hour when it names none; at most a day). The pins are `channel/subdir::name==version=build`, one for each decided file, in `MAMBA_PINNED_PACKAGES`: micromamba 2.9, the images' (ARG `MICROMAMBA_VERSION`), reads them there as `pinned_packages`, so the solve links those versions and builds, and the environment's history records the request's specs, not the pins. A pin holds a package to its name, version and build, not to its channel, so the channel each package came from is checked after the install. Checked offline with mamba 2.9, over HTTP in the cache's layout: the worker's own command installed the pinned builds, real dry-run output passed the strict parser, and a pinned build the index no longer offers failed the solve, at `resolve`, with nothing changed. The change is a managed revision whose `operation` names the operation. A file beyond the decided set (another version, build or channel) is still refused at its download if the policy escalates it, noted in the result if the policy allows it, and, if it declares the AGPL, ends the operation `needs_recovery` (`agpl_license`).
  7. **Probe** (CP-21 to CP-24): a capability's probes, or for packages a version check of each in the new revision's lockfile, recorded as `import_check`. Results belong to the revision they ran at (`probes` in its history entry). A probe that fails leaves the revision standing (CP-23).
- **Records** (CP-16, CP-17): the operation (`ProvisionOperation`) keeps the request, the catalog revision, the revision when it was requested and those before and after its install, the policy revision, each resolved file with the policy's action and rule, the permission request, the package changes (from the two lockfiles), the probes, its executions, timings, and, short of completion, the stage where it stopped (`queue`, `check`, `resolve`, `permission`, `download`, `link`, `post-link` or `probe`), a reason, the error and what to do next. A revision is the operation's only when its install's own package commands made it (managed): a change seen only after a step, or made by another execution meanwhile, is no operation's. A probe that ran at no revision the worker could tell has none. The tool's result reports from these records (CP-25).
- **Failures** (CP-13, in part): a failed install whose packages did not change ends `failed`. One that changed them ends `needs_recovery`, naming the revisions before and after, and the environment's package status stays `needs_recovery`, shown in its queue; later operations say so. The first such mark holds, with the last revision known good, which the worker keeps, with its lockfile, beyond its caps of 500 revisions and 50 lockfiles; a later failure's next step names that revision. Clearing a mark by hand, once the user has recovered the environment, is not built yet. Restoring it, from a snapshot or from its lockfile (open question 4), is the next slice's: `Restorer` is its seam, and with one an operation ends `rolled_back`. Until then, executions still run in such an environment. A changed fingerprint whose lockfile equals the one before (a rollback that rewrote the records) counts as unchanged. An install whose effect nobody saw counts as a change: a step that ends without reporting a revision, as when its executor is lost, is followed by a step that does nothing (`install-conda-look`, say), whose start reports the fingerprint, and if that cannot run either, the operation ends `needs_recovery` and says the packages may have changed. So does an install that a worker's stop, crash or own failure cut short, unless its step never started or its end was seen without a change.
- **Cancellation** (CP-14): a queued operation ends before its turn, one waiting for permission ends and its request is withdrawn, and a running step is cancelled like any execution; a cancelled install follows the failure rules.
- **Restarts.** Operations do not survive a worker restart: a stopping worker ends those still running (`worker_restarted`), and a starting one ends any a crash left, withdrawing their permission requests. Each ends `failed`, or `needs_recovery` if its install had started and nobody saw it end: it may have changed the packages, and nothing looked.
- **Trust.** The dry runs run in the sandbox, whose code can forge their output. The decision is the worker's forecast of the middleware's, made with the same library and checked against it in the tests; the middleware's decision on each download is what holds, and the grant is for exact paths at the cache, to one sandbox and policy revision, while the operation lasts. A forged dry run can only ask for other files, which the request lists by path for the operator to see; names that disagree with their paths fail the resolve. The AGPL check reads the licenses packages declare; it keeps honest installs within the licensing policy, not a hostile sandbox.
- **Upgrading.** New result fields (`origin` on executions, `operation` and `probes` on revisions, the `provisioning` feature) need orchestrators upgraded before workers. A policy that uses `packages.agpl_exceptions` needs the new policy library in the worker, the policy middleware and the MCP proxy.

Not yet: restoring a revision, so `rolled_back` in practice (CP-13, CP-20's return), disk limits (CP-14), uninstalling and upgrading through the tool (CP-20), the GUI (CP-26), logs as artifacts and the installers' versions (CP-16), events in the event store (`provision.requested` and the others), PyPI packages that ship no wheel, local test channels for the fixtures that need them (CP-F5, CP-F6), and extending the catalog by skill packs or project profiles (open question 6).

## PDFs

A `PdfRequest` runs one operation of the [PDF sources spec](specs/pdf-sources.md): `info`, `pages`, `search` or `render`.
- **Where it runs.** The executor runs the PDF helper (`pdfhelper.py`) as a process of its own, with its own interpreter, through the same path as a shell call's argv. Time limits, output limits, cancellation and leftover-process cleanup therefore apply as they do to shell calls. PDFs are untrusted, and nothing parses them outside the sandbox.
- **The helper.** The OpenShell backend uploads it beside the executor. It needs only the standard library and pdf_oxide (D59).
- **Results.** The result's stdout is the helper's JSON. On an error the exit code is 3, and stdout holds `{"error": {"kind", "message"}}`.
- **Renders.** A render writes its PNG into the session workspace, so it appears in `written_files` and can be published like any output.
- **The feature.** The executor offers the `pdf` feature only when its interpreter can import pdf_oxide. Without it, a `PdfRequest` fails as `unsupported`. The harness offers its PDF tools (`pdf_info`, `pdf_pages`, `pdf_search`, `pdf_render`) only for an environment with the feature.

## Executor protocol

The worker sends one JSON object per line; each carries an `id` and an `op`. The operations are `hello`, `probe` (the [isolation probe](#isolation-tiers)), `configure`, `shell`, `cell`, `cancel`, `restart`, `read`, `write`, `list`, `lockfile` (the packages' fingerprint and lockfile now, after waiting up to 30 s for a package command), and for the shared files' sync, `shared` and `remove`.
- **Lines.** UTF-8 JSON, one object per line. The executor's lines stay under 16 MiB (`MAX_LINE`, the worker's limit too); it reads requests of up to 64 MiB.
- **Replies.** Each request gets exactly one reply: `{"type": "reply", "id": …, "ok": true, …}`, or `ok: false` with `{"error": {"kind", "message"}}`. The `hello` reply says whether the executor became a subreaper (`subreaper`). A `read` with `"hash": false` leaves out the whole file's SHA-256, for publication's later ranges.
- **Configuration.** `configure` writes files into the executor's configuration directory (`<project root>/.executor/config`) or the home directory. It adds variables to the environment of every later shell command and kernel: its `env`, and for each file with a `variable`, that variable set to the file's path. It records what it wrote, keeps a file that changed since or that it did not write, and names each one it kept in its reply (`skipped`). The OpenShell backend sends the package clients' configuration this way, right after `hello`.
- **The shared files' sync** ([above](#shared-data-across-environments)). `shared` lists the regular files below `shared/`, each with its modification time, size, inode and executable bit; hashes the files asked for, within a size limit per file and a budget per scan, giving a digest only for a file still as scanned; and writes the worker's mark when given one and reports it. `remove` deletes a file in `shared/` only if its SHA-256 is the one expected, then the directories it leaves empty. `move` gives a file a free name in `shared/`, keeping its inode. A `write` may set a file's executable bit, and reports the stamp the written file has; a write that names the expected SHA-256 swaps the new file in at once (`renameat2` with `RENAME_EXCHANGE`) and swaps it back if the old one changed after its check. With `if_unused`, a `write` or `remove` leaves alone a file another process is writing and replies `busy`. Errors on the file itself come back as `invalid_request` or `policy_denied`; a full disk or an exhausted executor (no file descriptors or memory) as `unavailable`.
- **Events.** `shell` and `cell` reply when they are accepted, then send events: `{"type": "event", "execution": …, "event": "state" | "stream" | "output" | "revision" | "end", …}`. The `end` event carries the terminal state and the result fields of `Result`.
- **Packages.** The `running` state event carries the packages' fingerprint as the attempt starts (`environment`), and the `end` event's result their fingerprint as it ends and whether a package command was still running (`environment_changing`). A `revision` event, sent before either of those shows its change, carries a fingerprint, `managed`, the execution that caused the change (`cause`), the package commands (`commands`, the last 8, and `omitted_commands`) and the lockfile ([package commands](#package-commands)).
- **Hygiene.** The executor keeps the protocol on its own file descriptor and sends anything else that writes to stdout to stderr. Where the sandbox allows it, the executor is not dumpable, so research code cannot open that descriptor through `/proc` (not under OpenShell v0.1.3; see "Running under OpenShell").
- **Shutdown.** When stdin closes, it stops every command and kernel, and anything left below it, and exits. It sends no `end` for the attempts it stops then: the worker reports them `lost`.

## Settings

```toml
listen = "127.0.0.1:8100"
token_file = "worker.token"          # the pool's own token: sandbox-science-worker token --out worker.token
state_dir = "state"
pool_id = "babbage"
kernel_idle_timeout = 28800          # optional: stop kernels unused for 8 hours
shared_sync = true                   # keep each project's shared/ in step across its environments (D79)

[openshell]
cli = "/home/andy/.local/opt/openshell-0.1.3/bin/openshell"
python = "/opt/sandbox-science/bin/python"   # with ipykernel and jupyter_client; the base images' interpreter
project_root = "/sandbox/project"
isolation_tier = 3                   # required: the tier the driver gives (D43); the probe can lower it
sched_shim = true                    # build and preload the scheduler shim (needs cc on this host)
package_cache = "http://host.openshell.internal:8090"   # the package cache, as sandboxes reach it
env = {XDG_CONFIG_HOME = "/home/andy/.local/state/openshell-lab/config"}
sandboxes = {"env-1" = "research-1"}   # sandboxes made by hand

[openshell.environments]             # optional: the worker creates sandboxes (D73)
policy_store = "/var/lib/sandbox-science/middleware"   # the policy middleware's state_dir
targets = "targets.yaml"             # OpenShell targets for the compiler
ceiling = "pool-ceiling.yaml"        # the same pool ceiling the middleware applies
profiles = {minimal = "sandbox-science/minimal:<sha>", crypto-base = "sandbox-science/crypto-base:<sha>"}   # base profile -> image (IMAGES.md)
create_timeout = 600                 # seconds for a sandbox to become ready
event_store = "/var/lib/sandbox-science/middleware/events.db"   # optional: provisioning's permission requests; this is the default
admission = true                     # admit hosts on demand (D69)
admin_hosts = ["10.0.0.2", "gateway.lab.internal"]   # the policy middleware's admin_hosts
admission_interval = 3               # seconds between checks of each environment's drafts
max_admitted_hosts = 64              # hosts admitted on demand per environment
suspend_after = 3600                 # optional: stop sandboxes idle for an hour (W3)

[[clients]]                          # optional: tokens limited to some projects (needs [openshell.environments])
name = "orchestrator-p1"
token_sha256 = "<64 hex digits>"     # printed by sandbox-science-worker token --out client.token
projects = ["p1"]
max_environments = 8                 # live environments across its projects
```

Run it with `sandbox-science-worker serve --settings worker.toml`. The sandbox's environment needs Python with `ipykernel` and `jupyter_client`. On babbage they come from conda-forge through the [package cache](PACKAGE-CACHE.md), like any other tool.

`package_cache` is optional. With it, the worker points pip, uv, conda, mamba, micromamba, Cargo, Go and npm in each sandbox at the cache whenever it starts an executor there ([client configuration](PACKAGE-CACHE.md#client-configuration)). Without it, nothing is written. It is one setting for the whole backend: sandboxes made by hand and those the worker creates reach the cache at the same address, whatever their profile. With an `[openshell.environments]` table, the worker refuses to start unless `package_cache` names the host and port the targets file gives the package cache, since the policies it compiles admit the cache only there. The local backend has no such setting, since its executor shares the worker user's home.

The harness's command line runs a task through the worker. A JSON script of model turns can stand in for the model:

```bash
sandbox-science-harness run --worker http://127.0.0.1:8100 --token-file worker.token --project p1 --session s1 --environment env-1 --script turns.json "the task"
```

## Conformance suite

`packages/worker/tests/test_conformance.py` runs the [execution-tools fixtures](specs/tools.md#acceptance-fixtures) through the worker API alone, so it checks any backend without the harness (D52). It covers shell calls, the location and the isolation probe, kernels (state, interrupt, restart, death, streaming, reconnection), output bounds, cancellation, the file tools' confinement and publication. A backend passes only if the probe leaves its declared tier standing. CI runs it against a worker with the local backend that the suite starts. To check a worker that is already running, such as one with the OpenShell backend in a lab, set:

| Variable | Value |
| --- | --- |
| `SS_CONFORMANCE_WORKER_URL` | The worker's base URL, such as `http://127.0.0.1:8100` |
| `SS_CONFORMANCE_TOKEN_FILE` | The file holding its bearer token |
| `SS_CONFORMANCE_ENVIRONMENT` | The logical environment to run in, which the worker maps to a sandbox. The suite acts for the project `conformance`, so a worker's own environment must have been created for that project; a sandbox made by hand needs the pool's own token |

```bash
SS_CONFORMANCE_WORKER_URL=http://127.0.0.1:8100 SS_CONFORMANCE_TOKEN_FILE=worker.token \
  SS_CONFORMANCE_ENVIRONMENT=env-1 uv run pytest packages/worker/tests/test_conformance.py
```

The local runs are then skipped; setting only some of the variables is an error. Each test works in a new session (`conformance-…`) of the project `conformance`. Its files stay in the sandbox, and its kernels run until the executor ends. Tests that need the local backend's internals, such as a killed executor or a crashed store, are in `test_service.py`; those for the adversarial review of 2026-10-11, some with a stand-in for an executor that research code took over (`hostile_executor.py`), are in `test_robustness.py`.

## Not yet done

- **Survival across worker restarts.** The executor serves one stdio connection, so it ends when the worker restarts, and kernels end with it. The planned fix is a daemon in the sandbox listening on a socket, with the worker reconnecting through `openshell sandbox exec`. Background jobs, which run for hours (D83, [tools](specs/tools.md#background-jobs)), need it to survive a worker restart.
- **Environment lifecycle.** The worker creates, revises, suspends and deletes sandboxes ([environments](#environments)), and provisions them ([provisioning operations](#provisioning-operations)). Restoring a revision after a failed install, and rebuilding an environment from a lockfile, come next (W3). Policy revisions enter the store only through the operator's command; an approval path from the GUI, with re-authentication for widening (D54), comes later.
- **Data grants.** Scoped grants of project data, and session containers beside the project container.
- **Large outputs.** A rich output over the limit is replaced by a note; the spec's spill to a referenced file is not implemented.
- **Authentication.** Bearer tokens: the pool's own, and [clients' limited to projects](#clients-limited-to-projects). Environment IDs are still shared by the whole pool (see there). The outbound, worker-initiated channel for hosted control (architecture) comes later.
- **Monitoring.** No connection yet to the monitor or the event store with a hash chain.
- **Probes across sandboxes.** Two sandboxes with the same boot ID share a kernel, so neither is tier 3; the worker does not compare sandboxes with each other yet.
