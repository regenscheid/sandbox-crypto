# Background jobs as built

The worker runs long shell computations as executions of kind `job` (D83,
[tools specification](specs/tools.md#background-jobs), [worker API](WORKER.md)).
The harness offers `start_job`, `list_jobs`, `job_status`, `cancel_job` and
`wait_jobs` when the worker advertises the `jobs` feature. Jobs keep running
when the harness exits, the turn ends, or the GUI closes. [OpenShell lab 19](../.agents/docs/agent-reports/babbage-openshell-lab-19-2026-10-12.md) records worker, harness and browser acceptance checks.

`start_job` takes a script or argv, a purpose and `expected_progress`, with an
optional working directory and time limit. The worker's policy chooses an
omitted limit, bounds concurrency and provides the logs under
`.jobs/<execution ID>/stdout.log` and `stderr.log`. `job_status` returns bounded
head and tail output, resource measurements when the backend supplies them,
elapsed time, terminal outcome and failure reason. The existing `read_file`
tool reads full logs in ranges. All operations bind the job to the current
project, session and environment. Model arguments cannot replace that context.

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
   within the profile's `stage_max_bytes`;
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
   An hourly sweep releases overdue ones. Released files are gone.

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

Not yet: deleting the session environment cancels the job but does not release its
sandbox early; live head and tail output of a running job sandbox's job, checkpoints and
`resume_from` (increment 3), files over 100 MiB (increment 4), the GUI's profile and
phase columns and "release now", the process test over a real worker restart with local
daemons, and live Kata acceptance on the cluster.

## Waiting and the watcher

`wait_jobs` takes distinct `job_ids` and `mode: all` (the default) or `first`.
The same turn stays open while the harness reads worker status; the agent's
model is never called to poll. Completion, timeout, a watcher attention note,
or a message the user sends in the session's chat ends the wait. An interruption
leaves the computations running. The message stays in the session's queue and
reaches the model as the next user message, after the wait's result, so it is
neither consumed nor repeated ([chat](WORKSPACE.md#chat)).

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
states the outcome, exit code, elapsed time and output tail. After a tool call,
the notices ride on its result; a long result keeps its head and tail. Fetch failures
do not mark any undelivered notice seen. Notices a successful model call saw enter the run's
`delivered["jobs"]`, and are acknowledged only after `harness.save` keeps that
run's record in the control store. A failed turn or save can
repeat a notice; it cannot consume it silently. Jobs completed while the
harness was disconnected are found at the next safe point or turn.

## Workspace and current limits

The session page lists every active job and the 20 most recently ended jobs,
with live state, elapsed time, resource measurements, head and tail output,
recent watcher notes and cancellation. It refreshes through the authenticated
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
never resubmits the command. A managed VM must pass its lifecycle fence before
replacement work is admitted; its full log files persist across stop/start.

Still pending:

- Waking a session when a detached job ends.
  `start_job` refuses `wake_session: true` until the service can start a session
  turn for a finished job; detached completion is a next-turn notice.
- A terminal inbox for a running `run`; messages come through the workspace's chat.
- Project-persisted watcher settings. The harness takes them from CLI options.
