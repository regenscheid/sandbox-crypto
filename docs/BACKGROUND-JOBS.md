# Background jobs as built

The worker runs long shell computations as executions of kind `job` (D83,
[tools specification](specs/tools.md#background-jobs), [worker API](WORKER.md)).
The harness offers `start_job`, `list_jobs`, `job_status`, `cancel_job` and
`wait_jobs` when the worker advertises the `jobs` feature, as the `jobs` tool group:
a run loads it with `load_tools`, by calling one of them, or when a job's notice or the
shell's note on a computation loads it ([tool groups](specs/tools.md#tool-groups)). Jobs keep running
when the harness exits, the turn ends, or the GUI closes. [OpenShell lab 19](../.agents/docs/agent-reports/babbage-openshell-lab-19-2026-10-12.md) records worker, harness and browser acceptance checks.

`start_job` takes a script or argv, a purpose and `expected_progress`, with an
optional working directory, time limit and `restart` ([below](#restarting-a-lost-job)). The worker's policy chooses an
omitted limit, bounds concurrency and provides the logs under
`.jobs/<execution ID>/stdout.log` and `stderr.log`. `job_status` returns bounded
head and tail output, resource measurements when the backend supplies them,
elapsed time, terminal outcome and failure reason. The existing `read_file`
tool reads full logs in ranges. All operations bind the job to the current
project, session and environment. Model arguments cannot replace that context.

## Restarting a lost job

A job whose sandbox, executor or worker stops while it runs ends `lost`: its outcome is
unknown, and the worker does not run its command again on its own account. A job started
with `restart: true` says that its command is safe to run again and resumes from its own
checkpoints, which are files it writes in the workspace as it goes; there is no other
checkpoint interface. The worker then starts it again (`job_restarts.py`), up to
`MAX_JOB_RESTARTS` (3) times, as attempts of the same execution:

- **Never two at once.** A job restarts only when its attempt is known to have stopped:
  its sandbox deleted, stopped or replaced, its daemon a new incarnation (which, as it
  starts, ends the old incarnation's processes by their owner mark and by the sessions
  the old executor noted its jobs began), or its daemon no longer holding it. A loss the
  same daemon may still be running (an invalid replay, a failed reconciliation, an
  unconfirmed cancel) ends the job `lost`, and its error says why it was not restarted.
  So restarts need the sandbox daemon transport: on the stdio transport `start_job` with
  `restart` is refused, as it is on an executor too old for restarts (`job_attempts` in
  its `hello`; the job fails before its command when the worker learns that only as it
  dispatches it), and when the worker's `job_restarts` setting is off.

- **Not restarted.** The command's own end (a non-zero exit, a time limit, an output
  overrun), a cancellation (also one stored before a worker crash), a session close or
  executor release under way, or a job sandbox's job (`profile`, which the contract
  refuses with `restart`).
- **One table.** `contracts/restarts.py`'s `RESTART_POLICY` says which loss causes
  restart the job and which do not, and why, and every reason a job is not restarted;
  the worker decides from it, and `restart`'s description, the job notices and each
  "not restarted: …" reason are made from it:

  | Loss cause | Restarted | Why not |
  | --- | --- | --- |
  | its sandbox stopped (the same sandbox started again) | yes | |
  | a new daemon answered in its sandbox, ending the old one's processes | yes | |
  | its daemon no longer holds it | yes | |
  | a restarted worker found it never sent | yes | |
  | the daemon that ran it misbehaved or could not be reconciled | no | that daemon may still run it |
  | its cancellation was never confirmed | no | it was being cancelled |
  | a stdio executor or its worker stopped | no | its processes may outlive it |

  A deleted sandbox (the environment given up), a sandbox replaced by another, and an
  environment not usable again within 24 hours end a waiting attempt `lost`: the files it
  would resume from went with the sandbox, or did not come back.
- **The bound.** An attempt counts once the worker sent its command to an executor that
  took it, or may have: its dispatch is recorded just before the send (store version
  13), and only a definite refusal by the executor clears it (version 14). So the
  command is sent to run at most four times (`MAX_JOB_RESTARTS` + 1) whatever reports
  reach the worker; an attempt lost before it was sent does not count. The record keeps
  at most `MAX_JOB_ATTEMPTS` (8) attempts in all.
- **Where it is refused.** "Never two attempts at once" holds where the sandbox is a VM
  the platform recycles when its daemon is replaced (OpenShell): the recycle ends every
  process of the lost daemon. On the local backend a process that calls `setsid` and
  clears its environment escapes both the owner mark and the noted session, so
  `restart` is refused there unless the worker setting `job_restarts_unisolated = true`
  allows it for development. It is refused too on the stdio transport, on an executor
  too old for restarts, and while `job_restarts = false`.
- **The attempt.** The worker records the lost attempt in the job's record
  (`JobDetails.attempts`: its number and attempt ID, when it started and was lost, what
  stopped, its logs), gives the execution a new attempt ID and number, and queues it. The
  next attempt waits for the environment to be usable (no recovery, fence or recycle in
  the way), with a backoff from 5 s to 5 minutes, for up to a day from the loss
  (`ManagerTimeouts.restart_wait`); it then runs the request stored when the job was
  accepted: the same command, working directory, bindings, write area and time limit,
  admitted under the stored context's authority, never an operator's or that of the
  request whose task noticed the loss, so a newer recovery epoch of the session refuses
  it. It runs only in the sandbox the lost attempt ran in (the worker records each
  attempt's sandbox, store version 13): an environment created again with another
  sandbox ends it `lost`. A stopped (suspended) environment holds it until another use
  starts the environment; the worker has no pause of a session of its own. The job's
  concurrency place is kept throughout. A worker restart while it waits takes the wait
  up again without counting another loss, and after a restart the attempt's time limit
  counts from its own dispatch.
- **Time.** The time limit applies to each attempt, so a restarted job can run for up to
  four limits, besides its waits. The job view gives `elapsed_total_seconds`, the sum of
  each attempt's own running time, and `waiting_seconds`, the time between attempts since
  the first started, beside the current attempt's `elapsed_seconds` (from its start, or
  while it waits from the loss before it).
- **What it is told.** The attempt's environment has `SANDBOX_JOB_ATTEMPT` (1 for the
  first attempt, then 2 and on). Its logs go to `.jobs/<ID>/attempt-<N>/`, beside the
  earlier attempts', which stay readable; `JobDetails.stdout_path` and `stderr_path` name
  the current attempt's. As it starts, its executor lists the files of the session
  workspace and `shared/` changed since the lost attempt was queued, at most 100, each
  with its size and SHA-256 (within 4 GiB hashed in all), and the lost attempt's record
  keeps them (`survived`), sorted against what else wrote meanwhile as a result's files
  are: another session's uploads left out, and a file a file request wrote while the lost
  attempt ran, or a `shared/` file while another session's attempt ran, in
  `survived_unattributed` (after a worker restart the lost attempt's `shared/` files are
  all unattributed).
- **When it stops.** Cancelling the job, closing its session or releasing its
  environment's executor cancels a waiting attempt like any queued job. An environment the
  worker gives up, because its sandbox is gone, ends its waiting attempts `lost`: the
  workspace they would resume from went with the sandbox. So does an environment that is
  not usable again within the wait, a worker whose `job_restarts` setting was turned off
  meanwhile, or the last restart's loss; the error then says that the job was lost that
  many times and why it was not restarted again, and the record lists every attempt.
- **Records.** One execution throughout: its events go on in one numbered stream, its
  result and `written_files` are its last attempt's own, and each lost attempt's files
  are in its record. The harness records the job's files as all its attempts' (RA-27):
  the last attempt's, and each lost attempt's that no later attempt wrote, by the latest
  attempt that wrote it, each entry naming its `attempt`. A lost attempt's file is
  recorded only while its bytes are still the ones the worker hashed when the next
  attempt found them; one that is gone (a later attempt deleted it, or another writer)
  is reported as written by that attempt and gone, and one another writer changed since
  is left out as changed. The review feed's projection lists the lost attempts
  (`lost_attempts`), each with what it wrote and its digest. Each attempt has its own
  output budget.

A restart needs no run and no model call: the worker owns it, as it owns recovery, and it
happens whether or not a run of the session is active. A budget stop limits model calls
only, so a restart, which makes none, goes on through it as a running job does.

## Job sandboxes

A job may run in a new sandbox of its own, sized by a trusted job profile (D93,
[spec](specs/job-sandboxes.md)): `start_job` takes `profile`, one of the sizes the worker
grants the project, such as "large: 12 cores, 32 GiB memory, up to 7 days", and `inputs`,
up to 64 session files or directories to copy in. The harness lists the granted sizes in
the tool's schema from the worker's capabilities and adds its own `--min-isolation-tier`;
without granted profiles `start_job` is offered as before. Agents never give CPU,
memory, placement or pod settings.

The job keeps the session's context, so ownership, Resume, notices and the GUI treat it
as any job. The worker records it, then:

1. **creates** a [job environment](WORKER.md#job-environments) from the session
   environment's recorded image, policy revision, providers and admitted hosts. A
   sandbox not placed within the profile's `schedule_timeout` (30 minutes by default)
   ends the job `failed` with `capacity_unavailable`, and is deleted by identity;
2. **prepares** it: the isolation probe runs in the new sandbox, and a tier below the
   request's minimum or the session environment's ends the job `isolation_insufficient`
   and releases the sandbox; the session's recorded package changes (its lockfile against
   the image's packages) are installed at their recorded versions, Conda packages by
   channel, name, version and build, then pip pins, as provisioning operations through
   the package cache under the session's policy, and the job sandbox's lockfile must then
   hold them. A failure ends the job `package_replay_failed` or `environment_mismatch`
   before its command. Then the inputs are copied to the same paths, following no symlink,
   within the profile's `stage_max_bytes`. A sandbox whose executor cannot start because
   its image lacks the workspace package prefix the profile requires ends the job
   `failed` (`sandbox_unavailable`, naming the prefix) and is released at once: nothing
   ran;
3. **runs** the command there; the job is `queued` until now, and `running` from now;
4. **collects**: the whole logs go to the job's usual `.jobs/<ID>/stdout.log` and
   `stderr.log`, and each file the command wrote outside `.jobs/` and `shared/` to
   `.jobs/<ID>/files/<path>`, within the profile's `collect_max_bytes` (4 GiB), 1,000
   files and 100 MiB a file. `shared/` returns by the ordinary sync. The result's
   `written_files` names what came back, and the harness records it as artifacts at the
   completion notice;
5. **stores the manifest** (`JobResultManifest`) once, with the outcome: profile, image,
   sandbox, tier and evidence, replayed packages, inputs, collected files and those not
   collected with why. The store refuses to change it; `job_status` returns it with the
   digest (`job.sandbox.manifest_sha256`), and `.jobs/<ID>/manifest.json` is a copy the
   job could have changed;
6. **releases** the sandbox at once after a success whose collection was complete.
   Otherwise (failure, timeout, cancellation, an incomplete collection) it stops the
   sandbox, keeping its disk and freeing CPU and memory, for `job_retention` (3 days),
   at most `job_retention_per_project` (2) per project: a third releases the oldest.
   An hourly sweep releases overdue ones. Released files are gone. A job sandbox is
   disposable, so a package gate held for recovery, which keeps a session environment
   from being stopped or deleted, never keeps a job sandbox.

A deleted or evicted sandbox, found by looking at it while the job runs, ends the job
`lost` (`sandbox_lost`); the command is never submitted again. A full count is refused at
once, before anything is created: an unknown or ungranted profile, the project's
`job_sandboxes` (default 0, so the feature is off until granted), the profile's
`max_concurrent` and the pool's `max_job_sandboxes` (2). Job sandboxes count while
creating, preparing, running or collecting; retained ones only hold disk.

**A worker restart.** The job's command runs as an ordinary daemon job of the job
environment, so the worker's [recovery](WORKER.md#sandbox-daemon-and-recovery) treats it
like any other: on start it looks the sandbox up by its recorded name and immutable ID,
reconnects the bridge and replays the numbered events it missed, or reports that
execution `lost` when the gateway says the sandbox is gone or a new daemon incarnation
answers without it. The worker then takes the job up from its recorded phase, never
submitting its command again:

- **creating or preparing** (the command was not dispatched): the job ends `failed`
  (`worker_restarted`) and its sandbox is released; nothing ran;
- **running**: the job waits for its command again, then collects and ends as usual;
- **collecting**: collection goes on after the last file it recorded, so no file is
  copied back twice, within the deadline that began before the restart;
- **ended** (the manifest is stored, the job not yet reported): the stored manifest is
  the outcome; it is never rewritten.

A command lost with its sandbox ends the job `lost` (`sandbox_lost`) and releases the
record once deletion by identity confirms the sandbox is gone, so a gone sandbox holds
none of the project's retention places. A command lost while its sandbox stays (a new
daemon without it) ends the job `lost` too, collects what it can reach and retains the
sandbox. Neither is run again. A graceful worker stop leaves a job sandbox's job
unfinished for the next worker; it is not reported lost.

**Closing the session** cancels a job sandbox's job, collects what it wrote, and releases
the sandbox without retention. The close waits for the job for the cancel grace (30 s);
a job still collecting goes on, and still releases its sandbox when it ends, after a
restart too.

**Deleting the session environment**, or the worker giving it up because its sandbox
went, cancels its job sandboxes' jobs and releases their sandboxes without retention, as
a close does, but copies nothing more into the environment that is going: collection
stops before its next file, the manifest names what was left ("the session environment
was deleted"), and `shared/` still returns by the ordinary sync. The job ends `cancelled`
with a notice saying why, never `lost`, and its record and manifest stay. The
environment's retained job sandboxes are released too, since nothing can reach their
files through it any more. The deletion waits for the jobs for the cancel grace. A
deletion the worker did not see (made while it was stopped, or by the offline
reconciliation command) has the same effect when it next starts, and the hourly sweep
releases a retained sandbox whose session environment is gone.

**Releasing early.** A retained sandbox holds only disk, and the user may delete it before
its deadline with "Release now" on the job's card in the [workspace](WORKSPACE.md#background-jobs)
(JS-24). The worker's operator-only route deletes it by its identity as the sweep would and
refuses one that is not retained; it never releases a running job's sandbox, which is
cancelled first.

**Tested over a real restart.** Besides the in-process tests of every phase,
`test_job_sandboxes_process.py` runs the worker as a process over the fake OpenShell CLI,
with each sandbox's daemon a local process that outlives the worker, kills the worker
mid-job and starts it again: the job is taken up running and collected once, a sandbox
lost meanwhile makes it `lost` without a rerun, and deleting the session environment
releases it.

Not yet: live head and tail output of a running job sandbox's job, checkpoints and
`resume_from` (increment 3), files over 100 MiB (increment 4), and live Kata acceptance
on the cluster ([checklist](KUBERNETES-PREP.md#14-job-sandboxes-on-kata-qualification)).

## Waiting and the watcher

`wait_jobs` takes distinct `job_ids` and `mode: all` (the default) or `first`.
The same turn stays open while the harness reads worker status; the agent's
model is never called to poll. Completion, timeout, a watcher attention note,
the loss of an attempt of a job it waits on (`reason: job_lost`, whether the job is being
restarted or ended `lost`; the other jobs keep running, and `next` says to wait again),
or a message the user sends in the session's chat ends the wait. An interruption
leaves the computations running. The message stays in the session's queue and
reaches the model as the next user message, after the wait's result, so it is
neither consumed nor repeated ([chat](WORKSPACE.md#chat)). As `wait_delegations` does, a
wait also ends when an agent message reaches the run or a delegated run it started ends
(PD-44): the result's `reason` is `agent_message` or `delegation_ended`, it lists the
jobs as they stand, which keep running, and says to call `wait_jobs` again; the message
or the outcome follows as its notice. The run's capabilities give its job tools these
wakes (`capabilities.main` and `task`, `JobTools.wakes`).

A job belongs to the run that started it (`job_origins`), and the worker's close of a
run stops its kernels but cancels no job, since a job names no run. So when a delegated
run in the session's environment closes, the orchestrator cancels the jobs its work's
runs started that still run, just before it stops their kernels: as the work completes
or stops (`AttemptFinalizer.release_work`: cancelled, failed, at a limit), or, for a
returned task, once its parent's review closes it (accepted or cancelled), since a
reopened run may still use them while it awaits review (PD-46). An interrupted run that
a Resume may continue keeps them. The parent's and a sibling's jobs are other works' and
keep running. Each job cancelled so keeps why (`job_origins.ended_by`, control store
version 91), and its completion notice gives it as `cancelled_by`
(`jobs.cancel_closed_jobs`). A child in an environment of its own has its jobs there
ended with the environment's release (CE-4).

Without a watcher model alias, waits make no watcher calls and the GUI says
check-ins are off. To enable the separate role:

```bash
uv run sandbox-science-harness run --worker http://127.0.0.1:8100 \
    --token-file worker.key --state-dir harness-state --project research --session search \
    --model-url http://127.0.0.1:4000 --model main --api-key-file gateway.key \
    --watcher-model watcher 'Run the parameter search and wait for its result.'
```

The first check is due 300 seconds after a job starts; subsequent checks are
1800 seconds apart, including across repeated waits or harness restarts.
`--watcher-first-check` and `--watcher-interval` change these intervals in seconds.
`--watcher-script FILE` supplies scripted replies for tests, with a
`--watcher-model` alias and `--state-dir` for durable accounting. The watcher's alias may
be MAIN's: its calls are still its own, with their own limits and accounting.

The watcher receives purpose, expected progress, elapsed time, available CPU
and memory measurements, and bounded output head and tail as untrusted data.
It gets no tools. The only accepted response is
`{"state":"fine"|"needs_attention","note":"one short line"}`; unknown fields,
tool calls, large answers and multiline notes are refused and wake the agent
with a check-failed notice. A job completing during the check takes precedence
when evaluating the wait's completion condition. Only the main agent decides
what to do next.

Watcher calls reserve their budget before contacting the model. The default
is 60 calls per project in a rolling hour (`--watcher-calls-per-hour`), shared
across processes and runs. Requests, answers, requested alias, resolved model,
token usage and cost when supplied by the gateway, and the
[prompt provenance](WATCHER-PROMPT-PROVENANCE.md) are kept in the control store.
A failed or interrupted request still spends its reservation. Exhaustion
stops further check-ins until reservations leave the hour; the jobs keep
running. This is a durable role call budget. The cost of each call counts in the
project's cost and its optional daily cost limit ([run service](specs/run-service.md),
RS-6a).

## Notices and persistence

With a state directory, a job's files are recorded as artifact revisions when a run
first makes its completion notice, which says how many ([artifacts](ARTIFACTS.md#recording-the-files-calls-write)).

The worker is authoritative for execution state. Job control records share
`<state-dir>/control.sqlite3` with documents, in `job_` tables. They keep the
originating run, its active lease, waits, watcher calls and acknowledged
completion IDs. While an originating run is active it owns its notices;
after it ends, the next session run receives them. A crashed run's heartbeat
lease expires after 30 seconds. A run without `--state-dir` has only in-memory
notice deduplication.

At turn start, after a tool call and before accepting a final model answer,
the harness reconciles all worker submission pages. It returns a bounded
batch of previously unseen completions, deferring the rest. On the run's last
turn it skips the check before the final answer: the answer stands, and the
next run receives the notice. Each completion
states the outcome, exit code, elapsed time and output tail. A job that ended `lost` also
says whether it was restarted and why not (`restart`), and, since nothing listed what its
last attempt left, which files may be its (`survived`: those changed since it started, if
its workspace survived). A restarted job's lost attempt has a notice of its own while the
job goes on, `<ID>#<attempt>` in the delivered and acknowledged notices, with what was
lost, the attempt that follows and the lost attempt's logs. After a tool call,
the notices ride on its result; a long result keeps its head and tail. Fetch failures
do not mark any undelivered notice seen. Notices a successful model call saw enter the run's
`delivered["jobs"]`, and are acknowledged only after `harness.save` keeps that
run's record in the control store. A failed turn or save can
repeat a notice; it cannot consume it silently. Jobs completed while the
harness was disconnected are found at the next safe point or turn.

## Workspace and current limits

The session page lists every active job and the 20 most recently ended jobs,
with live state, elapsed time, resource measurements, head and tail output,
recent watcher notes and cancellation. A restarted job's card lists its lost attempts, with
what each next attempt found of their files, and a lost job's says whether it was
restarted. It refreshes through the authenticated
workspace server every three seconds while the page is visible, backing off
after failures. Active waits show their job IDs and
check-in setting; an expired harness lease is shown as disconnected. Job text
and watcher notes are escaped plain text. Keys stay in the server.

Enable these routes with `sandbox-science-workspace serve --worker URL
--worker-token-file FILE`, alongside its usual state directory and static build.
The server derives the environment from the recorded project/session and checks
the worker response's full ownership before exposing or cancelling a job.
Cancellation requires login and the server's own Origin, like other writes.

The [sandbox daemon](WORKER.md#sandbox-daemon-and-recovery) preserves jobs under
the same execution and attempt across worker/bridge restarts; [OpenShell lab
20](../.agents/docs/agent-reports/babbage-openshell-lab-20-2026-10-12.md) records
that recovery. Confirmed daemon/VM loss reports unknown outcomes as `lost` and
resubmits the command only for a job that asked for `restart`. A managed VM must pass its lifecycle fence before
replacement work is admitted; its full log files persist across stop/start.

Still pending:

- Waking a session when a detached job ends.
  `start_job` refuses `wake_session: true` until the service can start a session
  turn for a finished job; detached completion is a next-turn notice.
- A terminal inbox for a running `run`; messages come through the workspace's chat.
- Project-persisted watcher settings. The harness takes them from CLI options.
