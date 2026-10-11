# Execution tools: shell, kernels and files

**Status.** Draft (W0), to review with the user. W1 implements it first. It is written from the architecture and the observed behavior of the reference harness, in the project's own words (D56).

## Scope

This spec covers the agent's execution tools and the worker API operations behind them:
- shell commands;
- background jobs: long computations that run while the agent works on, or after its turn ends;
- persistent kernels: Python and Sage now, R later, all through the Jupyter protocol (D41);
- file handoff between the session workspace and the agent;
- admitting network hosts ahead of use;
- errors, cancellation and reconnection.

These belong to other specs: Conda provisioning ([conda-provisioning](conda-provisioning.md)), artifact versioning beyond publication ([research-artifacts](research-artifacts.md)), and documents.

## Principles

- **No local execution.** Every tool call runs on a worker, through the worker API. If no worker can run it, the call fails with `unavailable` or `unsupported`. It never falls back to the orchestrator's host ([architecture](../ARCHITECTURE.md)).
- **Identity.**
  - Each call is an *execution* with a stable ID, run as one or more immutable *attempts*.
  - Its context names the project, session, logical environment, concrete sandbox, environment revision, kernel (for cells), policy revision and actor (`ExecutionContext`).
- **Results are untrusted data.** They are bounded in size, decoded defensively and never interpreted by the orchestrator. File operations do not follow symlinks.
- **Evidence of location.** Every result says where it ran, so a test can show that code ran in the sandbox and not beside the orchestrator. It also gives the isolation tier in effect, with the evidence the worker found for it (D43).

## Shell

1. A call takes either a `script`, run by `/bin/bash -c` without a login shell, or an `argv` list, run without a shell.
2. **No state between calls.** Each call starts in the session workspace, unless it names another working directory under the session workspace or the shared project area. A `cd` or `export` lasts only for that call. Kernels are the stateful tool.
3. **Environment.** The session environment's variables: the environment's tools first on `PATH`, `HOME`, `SESSION_WORKSPACE`, `PROJECT_SHARED` and `PROJECT_DOCUMENTS`, the living documents' files ([living documents](living-documents.md), LD-3). There are no secrets; credentials appear only as provider placeholders (D57).
4. **stdin** is empty, so a command waiting for input reads end-of-file instead of hanging.
5. **Output.** stdout and stderr are separate streams of ordered chunks, each numbered within the execution. A stream larger than its limit keeps its head and its tail, drops the middle with a marker, and reports the total byte count. Bytes that are not UTF-8 are decoded with replacement characters.
6. **Ending.** The result carries the exit code or the terminating signal, the elapsed time, and whether the call timed out or was cancelled. Partial output is kept.
7. **Timeouts.** Each call has a wall-time limit, with a default and a maximum set by policy. When it expires, the process group gets SIGTERM, then SIGKILL after a short grace period.
8. **Leftover processes.** When a call ends, the processes it started that are still running are terminated, including those that left its process group or session. A shell call cannot leave daemons running ([worker](../WORKER.md#leftover-processes) says how far the executor can find them). A computation that should outlive the call is a background job.

## Background jobs

A background job is a shell command that runs without holding the agent's turn: lattice sieving, parameter searches and other computations that take hours (D83, confirmed by the user on 2026-10-11).

1. **Starting.** The agent starts a job with the shell tool's script or argv, a purpose and an optional time limit. The call returns at once with the job's ID, and the job runs in the session's environment and workspace, under the same policy, network path and resource limits as a shell call. The agent can go on working, start other tools, or end its turn.
2. **A job is an execution.** It has the execution identity, states, numbered events, idempotency and reconnection of the lifecycle below, and the shell's rules for its environment, stdin and output, apart from what follows. Its result lists the files it wrote, as an attempt's does.
3. **Output.** The job's stdout and stderr are also written whole to log files in the session workspace, under `.jobs/<job ID>/`, within the workspace's disk limits, so nothing is lost to the inline bounds. The agent reads the output so far, its head and its latest tail, at any time, while the job runs.
4. **Status and control.** The agent lists the session's jobs (running and recently ended, with elapsed time and the purpose), reads one job's state and output, and cancels a job: SIGTERM, then SIGKILL after the grace period, as for a call. When a job ends, its leftover processes are ended as a call's are.
5. **Limits.** A job's wall-time limit has a default and a maximum of their own in the policy's resources, longer than a call's; past it the job ends `timed_out`, with its output kept. The policy also bounds the jobs running at once in a session and in an environment; starting one more is refused with the list of jobs running.
6. **Notice when it ends.** The harness tells the agent when a job ends, with its outcome, exit code, elapsed time and output tail: at the next safe point of a turn in progress (after the current tool call), or at the start of the session's next turn. The run that started the job gets the notice while it runs; after it ends, the session's main agent does. A job may also ask to resume the session when it ends, which starts a turn if none is running (an open question: *Waking the session*).
7. **Lifetime.** A job outlives the turn, the call and the run that started it, the orchestrator's disconnection or restart, and the GUI's closing. It ends when it finishes, is cancelled, reaches its limit, or its session is closed or its environment deleted, which cancel it. An environment with a running job is busy: it is never suspended as idle.
8. **Worker restarts.** A job survives the worker's restart: the worker reconnects to the sandbox's executor and resumes the job's events where they stopped, so a job is `lost` only when its sandbox is. This needs an executor in the sandbox that outlives the worker's connection ([worker](../WORKER.md#not-yet-done)); until it exists, a worker restart makes running jobs `lost`, as it does calls, and the harness says so.
9. **The rest of the platform.** A job counts as a running execution everywhere else: package commands it runs take turns and new executions wait while they link ([Conda provisioning](conda-provisioning.md), CP-12); files it writes in `shared/` reach the project's other environments at the next sync point (D79); and the GUI lists the session's jobs with their state, elapsed time and output tail, and offers to cancel them.

## Kernels

1. **Identity.** A kernel belongs to one session and one language. It has an ID and a *generation*, which starts at 1 and grows by one on every restart.
2. **Executing a cell** streams its outputs as they occur:
   - text on stdout and stderr;
   - rich results and displays as MIME bundles (`text/plain`, `text/latex`, `text/markdown`, `image/png`, `application/json`, and others);
   - errors as a name, a value and a traceback.

   The cell ends with a status (`ok`, `error` or `aborted`) and an execution count. LaTeX is kept as written (`text/latex`), never only as rendered output.
3. **State.** Within one generation, state persists from cell to cell: a variable one cell defines is visible to the next.
4. **Queue.** A kernel runs one cell at a time, in submission order. Cancelling a queued cell removes it from the queue.
5. **Interrupt** stops the running cell. It ends with an error (`KeyboardInterrupt` for Python), and the kernel keeps its state.
6. **Restart** is explicit. State is lost and the generation grows; the next results report the new generation, and the harness tells the agent its state is gone. A restart does not wait for a running cell: that cell ends `cancelled`.
7. **Kernel death.** A kernel that dies (a crash, running out of memory, `os._exit`) ends its running cell with `kernel_died`. The next cell starts a new generation and reports that earlier state was lost. The worker never hides a restart.
8. **Limits.** A cell has a wall-time limit, like a shell call, after which it is interrupted. Its outputs have size limits; a large rich output is written to a file and referenced instead of being returned inline.
9. **Working directory.** A kernel starts in the session workspace.
10. **Languages.** The agent gets a tool for each language the environment offers (`python`, `sage`), and none for the others. Sage runs in SageMath's own Jupyter kernel, so Sage syntax applies (`^` is exponentiation); the platform never emulates it in Python.
11. **Closing a session** stops its kernels. Running cells end cancelled, queued cells never start, and the session's next cell starts a new generation that reports the lost state. The session's files stay.
12. **Idle kernels and environments.** A pool may stop kernels nobody has used for a set time, or suspend a whole environment nobody has used, whose kernels are not computing. Files and installed tools survive a suspension; kernels do not. The session's next cell then starts a new generation and reports the lost state, as after a restart.

## Files

1. **Paths.** Paths are relative to the session workspace, or start with `shared/` for the project's shared area ([projects and sessions](../PROJECTS-AND-SESSIONS.md)), which the worker keeps in step across the project's environments (D79), or with `documents/` for the living documents' files, whose writes are merged as direct edits ([living documents](living-documents.md), LD-50). Absolute paths, `..` segments and paths through a symlink are refused. The shell can still use symlinks; the file tools do not follow them.
2. **Read** returns a file's bytes, up to a size limit (ranges for larger files), with its SHA-256. Directories, devices and FIFOs are refused.
3. **Write** creates or atomically replaces a file in the session workspace. A write to the shared area must name the content hash it expects to replace (or none for a new file), and fails with `conflict` if the file has changed.
4. **List** returns up to a bounded number of entries. Each gives its name, type, size and modification time; symlinks are reported as links and not followed.
5. **Publish** copies a workspace file into the project's artifact store as an immutable revision. It returns an `ArtifactRef` carrying the content hash, the media type and the producing attempt: the last finished attempt that wrote the file.
6. **Written files.** Each attempt's result lists the files in the session workspace and the shared area that it created or changed, as the worker observed them. An attempt running at the same time in the session may contribute some of them.

## Network

1. **Admission ahead of use.** Where the worker admits hosts on demand (D69), an environment's capabilities list `admission`, and the agent can name the hosts its code will contact before running it: `admit_hosts`, with host names or `host:port` (443 unless given), at most 32. The worker decides each host with the project policy, as it decides a host a sandbox could not reach, and names the admissible ones in one new compilation of the environment's policy. The result lists each host as reachable or refused, with the reason.
2. **Reachable is not allowed.** Admission lets code connect to a host; the policy still decides each request.
3. **Without it**, the first connection to a host the policy reaches without naming fails, and the host becomes reachable some seconds later. A later result's notices say which hosts were admitted or refused.

## Errors

Every failure has one of these kinds and a message for the agent, which never contains secrets:

| Kind | Meaning |
| --- | --- |
| `invalid_request` | Malformed arguments, or a path outside the allowed areas |
| `unsupported` | The environment lacks the capability, such as a kernel language |
| `unavailable` | No worker or sandbox can be reached; nothing ran |
| `policy_denied` | The project policy forbids the operation |
| `not_found` | Unknown execution, kernel or file |
| `conflict` | An idempotency key was reused with a different request, or a shared file changed |
| `timed_out` | The wall-time limit expired; partial output is kept |
| `cancelled` | Cancelled on request; partial output is kept |
| `kernel_died` | The kernel process ended while running the cell |
| `exception` | The cell raised an exception; its name, value and traceback describe it |
| `internal` | A worker fault; the attempt's outcome may be unknown and must be reconciled |

Network failures inside research code are the program's own errors, not these kinds. Two come from the platform:
- **A host the policy allows but does not name.** The first connection to it fails with `Permission denied`, until the worker admits the host on demand, usually within half a minute (D69).
- **An escalated request.** The policy middleware answers 403 with the reason code `approval_required`. The request works once someone approves it.

The harness's instructions tell the agent to wait and retry.

## Lifecycle, idempotency and reconnection

1. **Idempotency.** Every submission carries a client operation ID, kept per project. Submitting the same ID again with the same request returns the same execution. The same ID with a different request is a `conflict`.
2. **States.** An attempt moves through `queued`, `running`, then one terminal state: `succeeded`, `failed`, `timed_out`, `cancelled` or `lost`. Cancellation is a request: the attempt shows `cancelling` until the worker has observed the process end.
3. **Events.** Every state change and every output chunk is an event with a sequence number that grows within the execution. Events are kept until well after the execution ends.
4. **Reconnection.** If the client disconnects, the execution continues. On reconnecting, the client asks for the events after the last sequence number it saw, and receives each event once. The final result can be fetched any number of times.
5. **Worker restarts.** Attempts in progress when the worker restarts become `lost` unless the worker can show what happened to them. They are never silently run again. Background jobs survive the restart once the executor outlives the worker's connection (background jobs, item 8).

## Contracts used

- `ExecutionContext`: every request and result.
- `Event`: output chunks and state changes.
- `ArtifactRef`: publication.
- Kernel identity and generation, and location evidence: extensions this spec adds to the shared contracts.

## Acceptance fixtures

The worker's conformance suite runs these through the worker API against any backend ([worker](../WORKER.md#conformance-suite)); fixture 11, and the notices of fixtures 13 and 16, are the harness's.

1. **Shell basics.** `echo hello; echo err >&2; exit 3` gives stdout `hello\n`, stderr `err\n` and exit code 3. The location names the sandbox, not the orchestrator's host.
2. **Stateful cells.** Cell 1 `x = 41`, then cell 2 `x + 1`, gives an `execute_result` with `text/plain` `42`, in generation 1 of the same kernel.
3. **Publication.** A shell call writes `out.csv` containing `a,b\n1,2\n`; publishing it gives an `ArtifactRef` with that content's SHA-256, `text/csv` and the shell call's attempt ID. Reading the artifact returns the same bytes.
4. **Interrupt.** A cell running `time.sleep(60)` is interrupted. It ends with `KeyboardInterrupt` within 5 seconds, and the next cell still sees `x == 41`.
5. **Cancel.** A shell call running `sleep 60` is cancelled. It ends `cancelled`, and no process from it remains.
6. **Reconnection.** A cell prints 1 to 10 at half-second intervals. The client disconnects after three output events and reconnects asking for events after the third. It receives the rest exactly once, then the final result.
7. **Idempotency.** Submitting the same operation ID twice returns one execution. Reusing it with different code is a `conflict`.
8. **Restart.** After a restart the generation is 2, `x` raises `NameError`, and the result reports the loss of state.
9. **Kernel death.** `import os; os._exit(1)` ends the cell with `kernel_died`. The next cell reports generation 2.
10. **Output bounds.** A command printing 5 MiB returns its head and tail, a truncation marker and the total of 5,242,880 bytes.
11. **No fallback.** With the worker unreachable, a shell call fails `unavailable`, and a sentinel file on the orchestrator's host shows that nothing ran there.
12. **Confinement.** Reading `../x`, `/etc/passwd` or a path through a symlink with the file tools fails with `invalid_request`. A host-only sentinel file outside the sandbox cannot be read from a shell call.
13. **A job runs while the agent works.** Starting `sleep 20; echo done` as a job returns within 2 seconds with a job ID; a shell call made next runs and ends while the job is still running; the job then ends `succeeded` with stdout `done\n`, and the agent's next turn, or its next safe point, carries the notice.
14. **Output so far.** A job printing one line a second shows, after 5 seconds, about five lines through the job's output, and the job is still `running`; its log file in `.jobs/<job ID>/` holds every line when it ends.
15. **Job cancellation and limits.** Cancelling a running job ends it `cancelled`, with no process left. A job past its limit ends `timed_out` with its output kept. Starting more jobs than the policy allows at once is refused, naming the jobs running.
16. **Jobs outlive their callers.** The orchestrator restarts while a job runs; the job ends normally, its events are complete after reconnection, and the agent gets its notice. With the executor outliving the worker's connection, the same holds for a worker restart.
17. **Busy, not idle.** An environment whose only activity is a job running for longer than the idle period is not suspended.

## Open questions

- **Shell state.** Should the shell keep a working directory between calls, as some interactive harnesses do? This spec proposes no state, which makes calls independent and retries safe.
- **Default limits.** Wall time and output size for shell calls and cells.
- **Daemons.** May a shell call start a long-running process, such as a local server for a notebook? This spec says no. Long computations are background jobs (settled with the user on 2026-10-11, D83); a service tool, for servers that answer requests, could come later.
- **Job limits.** Proposed defaults: a job runs at most 24 hours unless the policy raises it, up to 7 days; at most 4 jobs run at once in a session and 8 in an environment.
- **Waking the session.** Should a job's end start a turn by itself, which spends model budget while nobody watches? Proposed: the agent chooses per job, and the default is a notice at the next turn; resuming the session is allowed within the project's model budget.
- **Closing a session.** Should closing a session cancel its jobs, as it stops its kernels (proposed), or let them finish and keep their results?
- **Long cells.** Kernels stay interactive: a computation that should run in the background is a script run as a job. Should a cell also be able to run in the background, holding its kernel?
