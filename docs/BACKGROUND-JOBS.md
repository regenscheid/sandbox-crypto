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

## Waiting and the watcher

`wait_jobs` takes distinct `job_ids` and `mode: all` (the default) or `first`.
The same turn stays open while the harness reads worker status; the agent's
model is never called to poll. Completion, timeout, a watcher attention note,
or a message from an injected `asyncio.Queue[str]` ends the wait. An interruption
leaves the computations running. When message and completion race, the message
is either delivered or remains in the inbox. GUI chat and a CLI live message
inbox are not built yet; queue injection is the tested control API boundary.

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
`--watcher-script FILE` supplies scripted replies for tests, with a separately
named `--watcher-model` alias and `--state-dir` for durable accounting.

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
token usage when supplied by the gateway, and the
[prompt provenance](WATCHER-PROMPT-PROVENANCE.md) are kept in the control store.
A failed or interrupted request still spends its reservation. Exhaustion
stops further check-ins until reservations leave the hour; the jobs keep
running. This is a durable role call budget, not the future project-wide
monetary spend accounting.

## Notices and persistence

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
`job_notice_ids`, and are acknowledged only after the CLI saves that run's
transcript, including a synced directory entry. A failed turn or save can
repeat a notice; it cannot consume it silently. Jobs completed while the
harness was disconnected are found at the next safe point or turn.

## Workspace and current limits

The session page lists every active job and the 20 most recently ended jobs,
with live state, elapsed time, resource measurements, head and tail output,
recent watcher notes and cancellation. It refreshes through the authenticated
workspace server every three seconds. Active waits show their job IDs and
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

- A long-lived harness scheduler and project-wide spend accounting.
  `start_job` refuses `wake_session: true` until a budgeted scheduler can
  actually start a session turn; detached completion is a next-turn notice.
- GUI chat and a live CLI inbox. The tested async message boundary can already
  interrupt a wait without cancelling jobs.
- Project-persisted watcher settings. The harness takes them from CLI options.
