# Run service: GUI chat and live run events

Date: 2026-10-14. **Status: Approved; not built.** The owner decided the outline (refactoring plan, decision 5): a small orchestrator run service, one active run per session, messages queued to the run's next safe point, a new run carries earlier tasks and final answers, and a message may start a run within the project's model budget. The open questions below were decided on 2026-10-14 by the coordinator under the owner's autonomy mandate, each as recommended. Conventions in the [index](README.md).

## Scope

This spec covers:
- chatting with a session's main agent from the GUI;
- the process that runs the agent for the GUI;
- the run events the GUI shows while a run is in progress.

The browser's views are E-06 and E-07 in the refactoring plan. Waking a session when a detached job ends ([tools](tools.md#background-jobs), item 8) comes later, in the same process.

## Behavior

### The process

- RS-1. `sandbox-science-harness serve` is a long-lived orchestrator process for one state directory (new). It holds what `run` holds today: the worker's token, the gateway key and the model roles. It builds each run with the same `Harness` as `run`. The workspace server gets no worker or agent authority (CONVENTIONS, W4 exception).
- RS-2. The workspace and the service communicate through the control store (decided, question 1). This departs from the plan's wording, which has the workspace proxy the service: a message is durable before anything acts on it, and there is no listener and no second token. The workspace writes USER rows, as it already writes work requests and review requests. The service polls for them, at least once a second, and writes the results back. The service does not listen on any port.
- RS-3. The service renews a heartbeat row. The GUI shows whether chat is available from that row's age: more than 30 s old means no service is running.

### Messages and runs

- RS-4. A user message is a row of the session's message queue: its ID, the text (Markdown with LaTeX, at most 64 KiB), the USER author, the time and a state. The states are `queued`, `delivered` with the run ID, `refused` with a reason, and `held`: left by an interrupted run (RS-12).
- RS-5. **One active run per session.** A run holds a per-session run lease in the control store, which it renews while it runs. `run` from the command line takes the same lease. A second run for a session that holds the lease is refused (`busy`). A lease whose renewal stops for 60 s has expired; its holder died.
- RS-6. **Starting a run.** When a session has queued messages and no active run, the service starts a run. The run's task is the queued messages, oldest first. The run is refused when the project's model budget is spent (RS-6a). The messages are then `refused` with the reason `budget`, and the GUI shows it.
- RS-6a. **The model budget** (decided, question 3). Each project has a setting: the tokens MAIN may use over a rolling 24 hours, 2,000,000 by default. Spending is the input and output tokens of the project's runs that started in the window: from the saved records' `usage` (A-09), and for a run that ended `interrupted`, from the `usage` of its `assistant` events, so a crash does not hide what was spent. A call whose gateway reported no usage counts the characters it sent and received, divided by four. The budget is checked only when a run starts, so a run in progress finishes its turn.
- RS-7. **A message during a run.** A message queued while the run is active is delivered at the run's next safe point: before the next model call, or at once when the agent is waiting on jobs, which ends the wait with `user_message` ([tools](tools.md#background-jobs), item 6). It is not attached to a tool result. It goes in as a user message with the user's text as written. Unlike document text and notices, it is the user's instruction.
- RS-8. A message queued when the run is about to end is not lost. If the message is there at the run's closing, the run continues and answers it. If it arrives after the closing, it starts the next run (RS-6).
- RS-9. **Delivery follows the record.** A message counts as delivered once a successful model call has seen it. It is marked `delivered` only after the run's record is saved, the rule every notice source follows (A-02). A message that a failed or interrupted run saw stays `queued` and goes to the next run.
- RS-10. **Earlier conversation.** A new run's messages are:
  1. the system message;
  2. the session's earlier runs, oldest first: each run's task and user messages and its final answer, newest first until a character budget is spent, with tool calls and their results left out (decided, question 4);
  3. the new task.
- RS-11. **Cancel.** The user can cancel the active run. The cancel is a USER row the service reads. The run stops at once: a model call or tool call in progress is abandoned, as the CLI's interrupt does. Its background jobs keep running, since jobs outlive their run (D83). The record is saved with `stopped="cancelled"` (decided, question 5).
- RS-12. **Restart.** When the service starts, it finds every lease whose holder died. It ends each one by writing a `stopped` event with `interrupted`. No record is written for that run (decided, question 6). Its messages that no saved record delivered become `held`, so no run starts for them by itself. They are queued again when the user resumes or sends another message, and then go to that run.

### Run events

- RS-13. **Events as they happen.** While a run runs, the harness appends events to the control store, numbered from 1 within the run. Each event is committed before the run goes on to the next model or tool call. The final `RunRecord` is written once at the end, as now. Events are observations for display. Acknowledgements still follow the saved record (RS-9).
- RS-14. **The event format.** Each event is a row `(run_id, seq, at, kind, data)`, with `data` a JSON object of at most 64 KiB. Longer text keeps its head and tail with a marker; the record keeps the whole text. The kinds are:

  | Kind | `data` |
  | --- | --- |
  | `started` | `project`, `session`, `run_id`, `message_ids` (the messages it answers), `prompt_provenance` (the pins) |
  | `user` | `message_id` (null for a CLI task), `text` |
  | `model` | `turn`: a model call started (the GUI shows that the agent is thinking) |
  | `assistant` | `turn`, `text`, `tool_calls`: `[{id, name, arguments}]`, `usage`: `{model, input_tokens, output_tokens}` as the record keeps it (RS-6a) |
  | `tool` | `turn`, `call_id`, `name`, `result` (the text the model read, bounded), `outcome` (state, error kind and exit code when there is one) |
  | `notice` | `source`, `ids`: notices added to the conversation, such as jobs or reviews |
  | `waiting` | `job_ids`, `mode` while the agent waits on jobs; `null` when the wait ends |
  | `stopped` | `stopped` (`answered`, `turn_limit`, `context_limit`, `user_message`, `cancelled`, `interrupted` or `error`), `failure`, `final_text`, `record` (the saved record's name, or null) |

- RS-15. **Reading events.** The workspace reads a run's events by cursor, `after=seq` and a `limit`, from the control store, as it reads run records now. Its route returns them with the run's state: active, or ended with the `stopped` event. The browser polls with the generic `Poller`. SSE can come later without changing the format.
- RS-16. **Retention.** A run's events stay after its record is saved. The GUI shows the record for ended runs and the events for the active one and for interrupted ones. Interrupted runs have no record.

## Contracts used

- `RunRecord` gains `cancelled` as a `stopped` value. Its other fields are unchanged.
- The project record gains the MAIN token budget (RS-6a).
- The control store gains tables for messages, run leases, cancel requests, the service heartbeat and run events, each in one migration step (B-02).

## Acceptance fixtures

All are `scripted`, through the workspace's routes and the service, with the scripted gateway.

1. **A first message.** A message to an idle session starts a run. Its events arrive in order (`started`, `user`, `model`, `assistant`, `stopped`). The record names the message as delivered.
2. **A message during a tool call.** The second model call sees the message as a user message, after the tool result, and its answer covers it. Both messages end `delivered` to the one run.
3. **A message during a job wait** ends the wait with `user_message`. The agent answers it and waits again. The job is not disturbed.
4. **A message after the last answer** starts a second run. That run's conversation holds the first run's task and answer, and none of its tool calls.
5. **Busy.** `run` on the command line for a session whose run is active is refused, and the active run is unaffected.
6. **Cancel** during a tool call stops the run. A `cancelled` record is saved with the partial conversation, and a job it started keeps running.
7. **Restart.** The service is killed during a model call. On restart, the run ends `interrupted`. Its message is `held`, and nothing it saw is acknowledged. Resuming queues it, and a new run answers it.
8. **Budget.** With the project's budget spent, a message is `refused` with `budget`, and no model call is made.

## Decided questions

Each was decided on 2026-10-14 by the coordinator, under the owner's autonomy mandate, as recommended in the draft.

1. **How the workspace reaches the service.** The plan says the workspace proxies the service. *Decided 2026-10-14:* they share the control store instead (RS-2). The workspace writes messages and cancels as USER rows, as it already queues review requests, and reads events as it reads records; the service polls. This departs from the plan's wording on purpose: a message is durable before anything acts on it, so it survives a crash of either process, and there is no listener, no second token and no proxy route. The cost is up to a second of latency, and a refusal arrives on the message row instead of as the reply to the POST. A proxied HTTP API (loopback, a token file only the workspace reads) can come with W5's control API.
2. **Messages and document work requests.** E-06 suggests one table for both. *Decided 2026-10-14:* separate tables. Work requests carry anchors and are answered with `document_reply`; chat messages are plain. Both are turn hooks with the same delivery rule (RS-9).
3. **The project's model budget.** MAIN had none; the gateway key's spending limit was the only monetary cap. *Decided 2026-10-14:* tokens per project over a rolling 24 hours, 2,000,000 by default, a project setting, checked when a run starts (RS-6a). Runs that end `interrupted` count too, from the per-call usage their `assistant` events keep.
4. **The earlier conversation's size.** *Decided 2026-10-14:* 100,000 characters, a quarter of the run's input budget, without tool calls (RS-10). Runs that do not fit are left out, newest kept, and a line says how many were left out. Compaction comes later.
5. **Is a record saved on cancel?** Today an interrupted CLI run saves nothing. *Decided 2026-10-14:* yes, with `stopped="cancelled"` and no failure, so the GUI shows what happened (RS-11). Notices a successful model call saw are acknowledged, as for any saved record.
6. **Recovering an interrupted run.** *Decided 2026-10-14:* it ends as RS-12 says, without a record, and its messages become `held`. They are not run again automatically: a run that died once may die again, and each try spends budget. The GUI offers "resume", which queues the held messages again.
7. **Who may start runs.** *Decided 2026-10-14:* the workspace's one USER (E-17), any logged-in user of it. Spending is bounded by the project budget and the turn limit.

## Sources

Refactoring plan: decision 5, A-06, E-06 and E-07 in the [notes](../../.agents/refactor-plan/notes/). Repository documents: [tools](tools.md#background-jobs) (D83), [workspace](../WORKSPACE.md#run-records) and [projects and sessions](../PROJECTS-AND-SESSIONS.md).
