# Plans and delegation

Date: 2026-10-11, delegation design added 2026-10-14. **Status: Approved (2026-10-14).** The owner's answers on delegation limits and kernels are recorded (PD-14, PD-15); the delegation design on the harness (PD-32 to PD-43) and questions 1, 2 and 9 to 16 were decided on 2026-10-14 by the coordinator under the owner's autonomy mandate, each as recommended. Question 6 remains open, to be decided when questions to the user are built; question 7 was decided on 2026-10-15; question 5 was answered with the ledger's question 8 (2026-10-14). W0 behavior specification; conventions in the [index](README.md).

The planning phase and live plan progress are a confirmed requirement ([D86](../DECISIONS.md)); that does not settle every detail or open question in this draft. Built: the [plan and progress core](../SESSION-PLANS.md) (PD-1–5, PD-7, PD-9 and PD-10, with an explicit goal outcome on completion), the [MAIN plan tools and guidance](../ORCHESTRATOR-PROMPTS.md#implemented-stateful-main-planning-tools), the read-only [workspace view](../SESSION-PLAN-WORKSPACE.md) and USER [plan body editing](../SESSION-PLAN-EDITING.md). Not built: PD-6 approval, the reviewer's check of completion claims (PD-8), questions to the user and most of delegation (the run tree's parent link, PD-32, kernels per run, PD-37, and one level of check delegations are built). The design introduces no execution approval gate.

Delegation is built for one level and one kind of brief, a check of a ledger entry at a revision, in a project whose setting allows it (`project delegation --depth 1`; off by default): the delegate tools (PD-33), the delegated run's loop (PD-34), its outcome with a validated result and one correction (PD-17 to PD-19), handbacks (PD-35), a parent's answer, end and cancels while its children run (PD-36, PD-39), the end of a tree whose process died (PD-40), and the cap and the tree's budget (PD-38); a check that returned `confirmed` counts as the ledger's independent check (LG-17). Not built: general briefs and tracks (PD-9, PD-11, PD-12), the delegated role's settings (PD-27 to PD-30), files (RA-36, RA-38), the GUI's tree (PD-42) and depth 2. An earlier, unwired prototype (delegation records, a child controller and per-run worker kernels) is kept on the tag `archive/delegation-2026-10` ([archived pages](../../.agents/docs/archive/README.md)). Delegation will be rebuilt as a role of the one agent loop, with its own prompt, tool table, hooks and budget ([refactoring plan](../../.agents/refactor-plan/PLAN.md), Phase 4 item 7). The control store's plan records keep the `delegated` author role and track grants (PD-9).

## Scope

How agents organize research work in a session: versioned plans with phases, steps and tracks; progress; delegated agents that work on tracks and return structured outcomes; questions to the user; and the model settings of the three agent roles, main, delegated and reviewer (D61; the decision register leaves role settings to this spec). It also defines agent runs, which the other specs use. The user values this work organization (D04).

Out of scope: the project board and ledger ([research workspace](../RESEARCH-WORKSPACE.md)), which organize the project's research rather than one session's work; the tools ([tools](tools.md)); the reviewer's behavior ([consistency review](consistency-review.md)), apart from its model settings.

Terms:

- **Agent run**: one model loop with a role, model settings, tool grants, a skill set and a context, from its start until it stops. The main agent has one run per user turn, plus any review response round. Roles: `main` (answers the user), `delegated` (started by another run for a task or track) and `reviewer`. The proposed verification role (D27) would be a fourth.
- **Plan**: a versioned artifact owned by a session. **Step**: a unit of work in a plan. **Track**: a named group of steps that can proceed independently and can be delegated.
- **Delegation**: a parent run's request for a delegated run, and its outcome.
- **Question**: a durable request for the user's input, routed back to the run that asked.

## Behavior

### Plans

- PD-1. A plan states the goal, a feasibility assessment (what can be done, risks, capabilities needed), the desired outputs with the evidence type each should have (D29), and phases of ordered steps, which can be grouped into tracks. Text is Markdown with LaTeX (D25, D65) (CScience).
- PD-2. Steps have stable IDs and a short title unique within the plan. IDs survive revisions; a revision that drops a step keeps that step's history (new).
- PD-3. A plan is an artifact. Creating or changing it appends an immutable version that records its author (the user or an agent run) and a short reason (CScience). A change names the version it is based on; a stale base returns a conflict with the current version (D28).
- PD-4. The user can edit the plan directly. The edit is an attributed version, and an agent working from the plan is told to reread it (D23; CScience notifies agents of user edits).
- PD-5. A session has one current plan, with a status: `draft`, `active`, `completed` or `abandoned`. Earlier plans remain as artifacts (new). The `awaiting_approval` state was removed until plan approval is built (refactoring plan, decision 10); with approval off by default (PD-6), it returns only with that.
- PD-6. Approval: when the project or the agent requires it, a plan version waits for the user's approval. Meanwhile the session's agents can read and plan but cannot start executions or external changes. Approval names one exact version and comes only from the user, never from tool output or an agent; a later version needs approval again (CScience has such a mode and the pack relies on it). Approval is off by default, a project setting (decided 2026-10-14, open question 1).

### Progress

- PD-7. Progress is an append-only record, separate from plan versions: each entry names the step ID, the plan version, the status (`pending`, `in_progress`, `completed`, `blocked`, `skipped`), the actor, a note and optional evidence references. Progress carries over to new versions by step ID (new; CScience resets all step statuses when a revision is adopted, which loses history).
- PD-8. Progress entries are agent assertions. A `completed` step can link evidence (artifacts, executions, ledger entries), and the consistency reviewer checks completion claims against it.
- PD-9. A delegated agent updates only the steps of its own track (the pack expects this).
- PD-10. Marking a plan `completed` requires every step to be `completed`, `blocked` or `skipped`; otherwise the request is refused with the list of open steps (new; the pack's completion contract requires terminal steps).

### Delegation

- PD-11. A parent run starts a delegation with the task, the track (if any), a context summary, pinned input references, an output schema (the platform default, or one a skill supplies), a role profile (tools, skill set, model setting) and limits (turns, wall time, cost). The platform assigns a delegation ID that the request, the outcome and every related record carry (the pack's convention).
- PD-12. A track has at most one active delegation; starting another for it is refused while one runs (new).
- PD-13. A delegated run starts with a fresh context: its role instructions, the brief, its track of the plan, its skill catalog and the referenced inputs, not the parent's transcript (CScience; Claude Code's public subagent documentation). Skills the profile preloads are recorded as loads ([skills](skills.md), SK-21).
- PD-14. Delegated agents are logical: a delegation creates no container, and the child's tools use the session's environment ([architecture](../ARCHITECTURE.md)). Each delegated run gets its own kernels there, and sees its parent's results only through published artifacts and records (new). *Confirmed by the user on 2026-10-11 (D81): delegated agents are not separate sessions, and they stay in the session's environment; an environment of their own is a later opt-in. CScience likewise runs a session's child agents in the session's own sandbox, each with its own kernels and workspace directory.* *Answered by the owner on 2026-10-14 (open question 4): no kernel sharing. A parent never lends its kernel to a delegated run. In-memory state is handed off through saved files in the session workspace, such as Sage's `save()`/`load()` or pickle, so the handoff is explicit and recorded. Very large state stays with the parent, which does that part itself instead of delegating it.*
- PD-15. Depth and concurrency. *Answered by the owner on 2026-10-14 (open question 3; refactoring plan, Phase 4 item 7):* a session supports at least 8–10 delegated runs at once; 4–5 is the typical number. Nesting goes one level further: the main agent's delegated runs may delegate once more, and that second level may not. Depth is enforced by withholding the delegate tool at the limit, as Claude Code does (CScience children cannot delegate; Claude Code's documentation allows three layers below the main conversation by default, and 20 subagents at once). The rules, recorded with the owner's answer:
  - the default depth is 2 (the main agent's delegated runs, and one level below them), a per-project setting;
  - delegation can be switched off per delegated role;
  - one concurrency cap covers the session's whole tree of runs, not each parent;
  - a delegated run's budgets are carved from its parent's;
  - cancellation cascades down the tree (PD-21);
  - a handback goes to the direct parent (PD-20);
  - the GUI shows the runs as a tree;
  - the worker stays unaware of the tree: kernels are keyed by session and run, at any depth;
  - kernels start lazily, on a run's first cell;
  - the run tree has a parent link from the start; one level ships first, and deeper levels are enabled behind the depth limit.
- PD-16. A delegated run acts under the session's grants and the project policy, with at most its parent's tools. It MUST NOT answer questions for the user, decide permission requests, approve plans, or claim or confirm fixes to review findings (CScience reserves confirmation to the root session). Operations it causes can raise permission requests, and the decision returns to that operation.

### Outcomes

- PD-17. Every delegation ends with one outcome record that separates what the platform observed from what the child asserted (new):
  - `ended`, observed by the harness: `returned`, `failed`, `cancelled`, `limit_reached` or `interrupted`;
  - `result`, the child's structured result validated against the output schema. The default schema has a status (`complete`, `partial`, `blocked`), a summary, results as pinned references, deviations from the brief and open questions;
  - the raw returned payload, stored unchanged, and any validation errors ([external skills](../EXTERNAL-SKILLS.md));
  - *added by the owner on 2026-10-14, not built:* `result_files`, the files the child names as its results, which the platform checks and records as pinned artifact revisions, and the parent receives as references ([research artifacts](research-artifacts.md#files-from-executions-and-delegated-runs), RA-38);
  - *added by the owner on 2026-10-14, not built:* `files`, observed by the platform: the artifact revisions and tombstones the child's calls recorded, as pinned references, at most 200 with the total (RA-36).
- PD-18. If the payload does not validate, the harness asks the child once to correct it. If it still does not validate, the result is recorded as missing with the raw payload kept; defaults are never filled in (external skills).

  *Built 2026-10-15, for checks (PD-17 to PD-19):* a check's final answer is a JSON object, `sandbox-check-result-1`: the `entry` and `revision` it checked, which must be those of its brief; `status` (`complete`, `partial` or `blocked`); `verdict` (`confirmed`, only with `complete`, `refuted` or `inconclusive`); `summary`; `obstacle` (exactly when blocked); `deviations` and `open_questions`. Its texts are trimmed and checked as any agent text, with no character the user may not see, and the whole is at most 16 KiB. An answer that does not validate gets one correction, a turn with the errors (`sandbox-notice-outcome-correction-1`); if the next does not validate either, or no turn is left, the answer stands, the result is missing and the errors are kept. A result stands only for a run that ended with that answer, and is kept only for a delegation that `returned`: a run stopped by a limit, a cancel or a failure has none, whatever it answered before. The outcome keeps `ended` as observed, the result, the errors, the answer as returned (its head and tail past 16,000 characters; the child's record keeps it whole), the plan fence's refusal, whether a `ledger_read` of the child returned the entry at that revision (`read_entry`), whether its tools read the target of each of the entry's proofs at the revision its link pins (`read_proofs`: an entry by `ledger_read`, a document by `document_read`, an artifact by a `read_file` of the file with its SHA-256; control store version 32), and the model alias and the models the gateway named for the child's calls (PD-29). Every value is bounded to its table as it is written, so an outcome is never lost to its checks. The parent receives the result, or, without one, the answer and the errors. The ledger's gate counts a check only when it `returned` with `confirmed`, `complete`, no fence refusal, `read_entry` and `read_proofs`, and no check of the same material disagreed (LG-17). Result files (RA-38) come later.
- PD-19. Limits are not obstructions. A run stopped by a turn, time or cost limit ends `limit_reached` with its partial results kept. `blocked` is the child's own claim that something prevents the work, such as a missing capability, and it names the obstacle (external skills).
- PD-20. The outcome reaches the parent as the delegation's result and is stored as received; the parent's later interpretation does not change it (external skills).
- PD-21. Cancelling a parent run or the session cancels its delegations: their executions receive cancellation requests ([tools](tools.md)) and partial results are kept. Closing the browser cancels nothing (D10).
- PD-22. Plans, progress, delegations and outcomes live in the control store and survive worker loss and orchestrator restart. After a restart, a delegation that was running is `interrupted`; the parent can resume it from its saved transcript or start a new one. Tool calls that may have had effects are reconciled, not repeated ([tools](tools.md)) (new).

### Questions to the user

- PD-23. An agent can ask the user a question: text, optionally two to four options, and whether several may be chosen. The user can pick, answer freely, ask the agent to decide, or cancel (the pack's question shape). Questions are durable and survive browser closure.
- PD-24. The answer returns to the run and tool call that asked; concurrent questions never mix answers. A parent's message to a child that waits for an answer is queued and delivered after the answer (new; the pack's host notes say CScience drops such messages).
- PD-25. Questions are not permission requests. A question records a preference or research decision; a permission request decides an operation under the policy ([implementation plan](../IMPLEMENTATION-PLAN.md#shared-contracts-to-establish-first)). Neither can stand in for the other.
- PD-26. A waiting run blocks only itself. The GUI shows open questions with their age. A question does not time out unless its asker set a default and a timeout; the default is then applied and recorded as a default (new).

### Role model settings

- PD-27. Each role (main, delegated, reviewer, and the job watcher of [tools](tools.md#background-jobs), D83) has project-level settings: a LiteLLM model alias (D61), plus the reasoning effort or thinking setting where the adapter supports it, maximum output tokens, maximum turns, a cost budget per run, and the context size at which compaction starts (new).
- PD-28. For a delegated run, the brief's model choice applies if the project allows overrides and lists that alias; otherwise the delegated role's settings, then the deployment default (new; Claude Code's public documentation describes a similar order for subagents). Agents cannot choose the reviewer's settings.
- PD-29. Each agent run records its role, the alias, the provider model that LiteLLM resolved, and the effective settings. A setting the adapter cannot apply is reported, never dropped silently (new). Load evidence and lineage use these records (external skills, gate 2).
- PD-30. An unavailable alias, or one lacking a capability the role needs such as tool calling, fails the run at start with a reason. A fallback alias is used only when the project names one, and its use is recorded (new).
- PD-31. The LiteLLM virtual key's spend limit is the hard cap (D61). Per-run budgets are soft: reaching one ends the run with `limit_reached` (new).

### Delegation on the harness (design, 2026-10-14)

How the requirements above are built on what exists: the one tool loop (`run_task`) with its turn hooks, the `Harness`, and the run service ([run service](run-service.md)). A delegated run is a role of the same loop, with its own prompt, tool table, hooks and budget (refactoring plan, A-02 and Phase 4 item 7), not a second agent loop. The archived prototype (`archive/delegation-2026-10`) is reference for the records and the tests only.

- PD-32. **The run tree.** Every agent run has a role, and a delegated run names its parent run and its delegation. The run's record (`RunRecord`) and its `started` event carry `role`, `parent` (the parent's run name, null for the root) and `delegation`. *Built 2026-10-14:* the fields, kept in the control store's `runs` table (version 11) and listed with each session's runs; the main run is the root, and nothing starts delegated runs yet. A delegated run's parent is a run of the same session, kept or still running (its events), whose name sorts before the child's: names begin with the start time, so a run is never its own ancestor. The store ties a run's role to its parent and delegation; a parent's record is kept after its children's, so there is no foreign key. A development store that an earlier build of this change took to its own version 10 lacks main's version 10 (project macro revisions) and must be recreated. The root is the session's main run. It holds the session's run lease (RS-5) for the whole tree: delegated runs take no lease of their own, run in the same process as their root, and stop when the root's lease is lost (new).
- PD-33. **The delegate tools.** A run whose depth is below the project's limit, and whose role allows it, is offered:
  - `delegate`: the brief of PD-11 (task, track, context summary, pinned inputs, output schema, role profile, limits). It returns the delegation ID at once; the child runs while the parent goes on;
  - `wait_delegations`: waits on some delegations, all or the first to end, as `wait_jobs` does (D83), ending also when the user writes to the root;
  - `delegation_status` and `cancel_delegation`.

  A run at the depth limit is offered none of them (PD-15) (new; Claude Code withholds its tool the same way).

  *Built 2026-10-15, one level:* main runs of a project whose delegation depth is 1 are offered `delegate`, `wait_delegations`, `delegation_status` and `cancel_delegation`. The brief is a check of a ledger entry at a revision, which must exist, and the platform writes it (LG-17; coordinator, 2026-10-15, which the owner can reverse): the task from a fixed template, the entry's evidence at that revision, at the revisions it pins, as the inputs, and the parent's note, at most 500 characters and checked as any agent text, labelled to the child as the requester's note, not evidence. The parent gives no task, context or inputs of its own, which could carry the verdict; general briefs, when built, will. A delegated run is offered neither, and a call to a tool it is not offered, connectors' included, is refused with `not_permitted_for_role`. A `delegate` is refused with `limit_reached` while the project has spent its model budget,.
- PD-34. **A delegated run's loop.** It runs `run_task` with the delegated role's system prompt and its brief as its task, its own run name, record and events, and a toolbox of at most its parent's tools minus those PD-16 forbids. Its hooks: its own job notices, the kernel check (on its own kernels; until the worker records their births, PD-37, it has no start revision to tell about, backlog), the plan fence with progress only on its track (PD-9), math findings on what it writes (D82), and its events. Not: the user's messages, document work requests or the consistency review, which belong to the root (new).

  *Built 2026-10-15:* a check runs with the system prompt `sandbox-delegated-check-3`, which lists the brief with its untrusted data and tells it to read each proof's target, the memory its root was given (PM-8) and the brief as its first message, within 12 turns and 150,000 tokens (`run_budget` when its calls reach them; coordinator, 2026-10-15). It gets its parent's tools without the review, plan and delegate tools and connectors; track progress comes with tracks. It keeps the plan fence without the plan tools: its calls that may change something are refused while the plan is a draft, once the user has changed the plan since it started, or once the session was closed or opened again, and the outcome records the first refusal (`refused`). Its events and those calls also need its root to hold the session's lease. The parent's kernel check leaves its running children's kernels (PD-37), and a child's end waits at most 20 s for the worker to stop its kernels.
- PD-35. **Handback.** When a child ends, its outcome (PD-17) is stored, and it reaches the direct parent at the parent's next safe point, as a delegation notice, or as the result of its wait. Like every notice, it is acknowledged only after the parent's record is saved (A-02), so a parent that fails gets it again in its next run (new).

  *Built 2026-10-15:* the outcome is `ended` as the harness observed it (`returned`, `failed`, `cancelled`, `limit_reached`) with the child's final answer as its report. A notice gives each outcome's verdict and status, or without a result the head of the answer (300 characters), the notices together at most 5,000 characters; `wait_delegations` returns whole outcomes, of any ended delegation of the session, as many as fit in the run's output limit. A wait's result counts as received once a model call has seen it, and the parent's record names it in its `delegations` summary. An outcome no run acknowledged goes to the session's next main run, also once the project stopped delegating.
- PD-36. **A parent's answer waits for its children.** A run cannot give its final answer while its delegations run: its closing gets another turn with a notice of the running ones, to wait on or cancel. At its turn limit, its running delegations are cancelled first (new).

  *Built 2026-10-15:* the closing answer takes another turn with a notice naming the running children (`sandbox-notice-delegations-running-2`); with no turn or room left, the run stops without an answer (`turn_limit` or `context_limit`), and its end cancels them.
- PD-37. **Kernels per run.** A cell names its kernel by the run that runs it. The worker keys kernels by session and run, starts one on the run's first cell, and stops it when the run ends; it knows nothing of the tree (PD-15). This changes the worker protocol: `CellRequest` gains the run, and the protocol version goes up (D-01, lockstep) (new). *Built (worker protocol 4):* a main run's cells name no run and use the session's kernel, which the session's main runs share as before; a delegated run's cells, restarts and kernel notices name its run. The harness stops a delegated run's kernels when the run ends (`SessionCloseRequest.run`); the worker then refuses that run's late cells, and a session has at most 63 runs with live kernels of their own at once ([worker](../WORKER.md#behavior-worth-knowing)). A main run first closes the run kernels its session's earlier runs left, since a harness that stopped never closed them (PD-40); its kernel check does it at its first observation of a connected executor, which never wakes the environment and, if nothing is connected at the run's start, comes after the run's first call; it leaves the run's own live delegated runs' kernels: the session's runs run under its main run, so none of them is running then.
- PD-38. **Limits.** The session's tree runs at most the project's cap of delegated runs at once, 10 by default (a project setting), counted separately from background jobs (4 per session). A `delegate` beyond the cap is refused with `limit_reached`, not queued. A child's limits (turns, wall time, tokens) are at most what its parent has left; they are reserved from the parent when the child starts and what it did not use is returned when it ends. All the tree's model calls count toward the project's model budget (RS-6a) (new).

  *Built 2026-10-15, in part:* at most 10 delegated runs at once, a constant; a `delegate` past it is refused with `limit_reached`. Under the run service, a root's share of the project's budget (`serve --run-share`) bounds its whole tree: a check's tokens are reserved from what the root has left, at most 150,000 and at most what leaves the root 100,000 for its own calls, or twice its last call when that cost more, and a `delegate` that cannot reserve 50,000, or made with no turn left, is refused with `limit_reached`. A running child counts as its reservation, or as what it used once that is more; one that ended counts as what it used, so the rest returns to the root. The root stops (`run_budget`) once its own calls and its children's shares reach its share. Every call of the tree in flight counts, as an estimate of what it sends with its tool schemas and an allowance for its answer: toward the root's reservations, a child's only past its reservation, and toward the project's budget at each run's check, its own coming call included. A run's check registers its call in the same step as it passes, and the call stays counted until its `assistant` event holds its usage, so the tree's runs calling at once stay within the budget together. A run also stops before a call that would take it past its own limit. A child's turns are at most the parent's turns left. All the tree's calls count toward the project's budget through their records and events. `run` on the command line keeps no budget and reserves nothing. Not built: the cap and the children's defaults as a project setting, and wall-time limits.
- PD-39. **Cancellation.** Cancelling a run cancels its running children first, then itself: the user's cancel of the root (RS-11), a lost lease, `cancel_delegation`, or a parent's turn limit. Each child's record is saved `cancelled` and its outcome reaches its parent as `cancelled`, as PD-21 requires; their executions get cancellation requests (new).

  *Built 2026-10-15, for the parent's end:* however the parent ends (its answer, a limit, the user's cancel, a lost lease), it cancels its running children and waits up to 60 s for their records, which are saved `cancelled`, and their outcomes, `cancelled`; a child that has not ended by then is recorded `interrupted`. A run stopped by a spent budget while children run keeps `budget` or `run_budget` as its reason. `cancel_delegation` does the same for one child the run started, and returns its outcome.
- PD-40. **Restart.** When the service finds a tree's lease dead (RS-12), every run of the tree without a saved record ends `interrupted`, and each child's outcome is recorded `interrupted`. A run is not resumed from its events: PD-22's resume needs a saved transcript the run service does not keep (open question 13).

  *Built 2026-10-15:* when the run service ends a run whose lease is dead, or that stopped without its record, its delegations still running end `interrupted`, and each of its children's runs that lacks its `stopped` event gets it: from its record if it saved one, otherwise `interrupted`. Both happen in one transaction, and even when ending the root itself fails. As a main run starts, it does the same for the session: delegations still running, which a command-line run whose process died left, and any delegated run without its `stopped` event, such as the cancelled child of a command-line root that lost its lease; their outcomes then reach that run. A failed write of an outcome is tried three times. A run is not resumed (open question 13). Accepted: a root in another process that is alive but slow, whose lease looks expired for 60 s while it winds down, is reaped as dead; its running children's delegations then end `interrupted` rather than `cancelled`, and the outcomes their runs write later change nothing.
- PD-41. **The user.** The user writes to the root only (RS-7). A delegated run cannot ask the user (PD-23 is not built yet): one that needs an answer ends with result status `blocked`, the question in its outcome's open questions (open question 14).

  *Built 2026-10-15:* a check's system prompt tells it to stop with status `blocked` and its question in `open_questions`.
- PD-42. **The GUI.** The workspace lists a session's runs as a tree, from the records' and the `started` events' `parent`, and reads each run's events as it does now (RS-15). The browser half is lane G's (new).
- PD-43. **Build order.** The run tree with its parent links, the per-run kernels and one level of delegation come first; depth 2 is enabled behind the depth limit once one level works. Delegation is part of M2 only if the owner says so (refactoring plan, Phase 4 item 7).

## Contracts used

- `ArtifactRef`: plans are artifacts; briefs and outcomes carry pinned references to inputs and results.
- `Mutation`: plan versions and progress entries name the version they are based on; a conflict returns the current version.
- `Anchor`: steps are addressable blocks of a plan version, so comments, findings and progress can point at them.
- `ExecutionContext`: a delegated run's executions carry the session's context; the actor names the agent run and its role, and the origin names the delegation and step (proposed additions in the [index](README.md#proposed-contract-additions-new)).
- `Event`: `plan.version_created`, `plan.status_changed`, `progress.recorded`, `delegation.started`, `delegation.ended`, `question.asked`, `question.answered`, `agent_run.started` and `agent_run.ended`.
- `PermissionRequest`: raised by operations, never by questions; the decision is routed to the operation that caused it, including one started by a delegated run.
- `EnvironmentRecord`: the environment and revision a delegated run's tools use, which is the session's.

## Acceptance fixtures

All fixtures are `scripted`: the main agent, delegated agents and the user's actions are scripted.

**PD-F1 Plan versions and conflicts** (PD-3, PD-4)
- *Given* the main agent creates plan v1 with two phases, four steps and two tracks.
- *When* the user edits a step (v2); the agent submits a change based on v1; it rereads and submits again based on v2.
- *Then* the first submission returns a conflict naming v2; the second creates v3 containing the user's text; the history shows v1 (agent), v2 (user) and v3 (agent), with reasons.

**PD-F2 Progress across versions** (PD-2, PD-7, PD-10)
- *Given* v3 with step S2 `in_progress`.
- *When* the agent marks S2 `completed` with an artifact reference, publishes v4 (which inserts a step before S2 and removes S4), then marks the plan `completed`.
- *Then* S2's history continues in v4 under the same ID; S4's history stays readable and is marked removed; marking the plan complete is refused, listing the steps that are still `pending`.

**PD-F3 Approval** (PD-6)
- *Given* the project requires approval.
- *When* the agent submits v1 and calls the shell tool; a tool result containing "plan approved" arrives; the user approves v1; the agent publishes v2.
- *Then* the shell call is refused with `plan_not_approved`; the tool output changes nothing; after the user's approval executions run; v2 is `awaiting_approval`.

**PD-F4 Outcome kept as returned** (PD-11, PD-17, PD-20)
- *When* the main agent delegates track T1 with the default schema, and the child returns status `partial`, one artifact reference and one deviation.
- *Then* the outcome has `ended: returned`, the validated result and the raw payload byte for byte; the parent receives it; the parent's later summary leaves the stored outcome unchanged; one delegation ID appears in the request, the outcome and every event.

**PD-F5 Invalid payload** (PD-18)
- *When* the child returns malformed JSON twice. *Then* there is exactly one correction request; the result is recorded as missing; both raw payloads are kept; no default values appear.

**PD-F6 Limit or obstruction** (PD-19)
- *When* child A exceeds its turn limit and child B returns `blocked`, naming the capability `sage` as missing.
- *Then* A ends `limit_reached` with its partial artifacts linked; B ends `returned` with result status `blocked`; the plan view shows the two tracks differently.

**PD-F7 Logical delegation** (`worker` as well; PD-14)
- *When* a child runs a Python cell reading `x` while the parent's kernel holds `x = 1`.
- *Then* the cell runs in the session's environment at the same revision, in a kernel distinct from the parent's, where `x` is undefined; no container or environment was created.

**PD-F8 What delegated agents cannot do** (PD-15, PD-16)
- *Given* the default depth, a delegated run C1 of the main agent, and C1's own delegated run C2.
- *When* C2 tries to answer a pending question, decide a permission request, approve the plan and claim a fix for a finding.
- *Then* each attempt is refused with `not_permitted_for_role` and recorded; C1 was offered the delegate tool and C2 was not.

**PD-F9 Question routing** (PD-23, PD-24, PD-26)
- *Given* children C1 and C2 have each asked a question, and the browser is closed and reopened.
- *When* the user answers C2, then C1, and the parent sends C1 a message while it waits.
- *Then* each answer reaches its own run and tool call; the parent's message reaches C1 after the answer; the parent kept working meanwhile.

**PD-F10 Role settings** (PD-27, PD-28, PD-29)
- *Given* main uses alias A with high effort, delegated alias B, reviewer alias C, and overrides are allowed for alias D only.
- *When* the main agent delegates once with D and once with E, tries to change the reviewer's alias, and a checkpoint review runs.
- *Then* the D delegation runs on D; the E delegation is refused; the reviewer's setting is unchanged; every run record shows role, alias, resolved model and effective settings.

**PD-F11 Unavailable alias** (PD-30)
- *Given* the delegated alias is missing from LiteLLM. *Then* the delegation fails at start with `model_unavailable` and no fallback. With a fallback configured, it runs on the fallback and records that.

**PD-F12 Restart and cancellation** (`worker` as well; PD-21, PD-22)
- *When* the orchestrator restarts while a child's cell runs; later the user cancels the main run while two children run.
- *Then* after the restart the child is `interrupted`, its cell's attempt is reconciled and not run twice, and resuming continues its transcript. After the cancellation both children end `cancelled` with partial results kept, and their executions show cancellation requests.

## Open questions

1. Plan approval: off by default, or on? Should every new version of an approved plan need approval again (as drafted), or only substantive changes? *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* off by default, a project setting; when on, every new version needs approval again, since what counts as substantive cannot be told mechanically, and a wording-only change is a quick approval. The unused `awaiting_approval` state was removed until this is decided (refactoring plan, decision 10).
2. Progress across versions: carry over by step ID (PD-7), or reset as CScience does? *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* carry over by step ID, as built ([session plans](../SESSION-PLANS.md)); a removed step keeps its history.
3. **Answered (2026-10-14, by the owner):** delegation limits. A session supports at least 8–10 delegated runs at once, 4–5 typically; the main agent's delegated runs may delegate once more, and the second level may not, enforced by withholding the delegate tool at the depth limit. The proposed rules for nesting are in PD-15.
4. **Answered (2026-10-14, by the owner):** no kernel sharing. Delegated runs get their own kernels (PD-14); a parent never lends its kernel to a delegated run. In-memory state is handed off through saved files in the session workspace, such as Sage's `save()`/`load()` or pickle; very large state stays with the parent.
5. Should plans also exist at project level, spanning sessions, or is that the board's job? **Answered by the owner (2026-10-14), in the ledger's question 8:** the board's; a plan step may name the ledger task it advances, and there is no separate project plan ([ledger](ledger.md), LG-40).
6. Questions with a default and a timeout (PD-26): useful, or should questions always wait for the user?
7. Which role settings should the GUI expose per delegation: the alias only, or effort and budgets too? *Decided 2026-10-15 (coordinator, which the owner can reverse):* all of them, read-only, with what the run used; the user edits only the project's defaults.
8. **Settled (2026-10-11, D81):** a delegation will be able to ask for an environment of its own, made from the project's base image, for work that needs isolation, such as an independent check; it is a later opt-in, in the backlog. By default delegated runs stay in the session's environment. Measured on babbage (OpenShell 0.1.3, VM driver): the image is prepared once per digest, and each further sandbox from it adds a sparse copy-on-write overlay of about 8 MB on disk, growing with what it writes; it starts in about 7 s and its VM uses about 220 MB of memory idle. Such a run would not see the session's installed tools or workspace, only `shared/`, so it would install again what it needs.

9. **How a parent works with its children** (PD-33). *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* asynchronous: `delegate` returns at once, the parent waits with `wait_delegations` or gets the handback as a notice. A blocking `delegate` call, as Claude Code's foreground subagents, would serialize the 4–5 typical children.
10. **A parent's answer while children run** (PD-36). *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* refused until they end; at the turn limit, they are cancelled. The alternative, ending the parent and orphaning running children, leaves outcomes nobody receives.
11. **Kernels per run** (PD-37). *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* a kernel stops when its run ends, and the cell names the run in the worker protocol. A kernel kept until the session closes would hold memory for runs that cannot use it again. A babbage lab measures a kernel's memory first, which sets the per-session kernel memory budget and the VM size for delegating sessions (refactoring plan, Phase 4 item 7).
12. **The concurrency cap's default** (PD-38). *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* 10 running delegated runs per session, a project setting, with background jobs counted on their own (4 per session, 8 per environment). A `delegate` past the cap is refused rather than queued, so the model sees the limit and waits on its children.
13. **Resuming an interrupted run** (PD-22, PD-40). *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* not in the first version: an interrupted child ends `interrupted`, and its parent, in its next run, can delegate again. Resuming needs the whole transcript saved as the run goes, which the run service's bounded events are not.
14. **Delegated runs and the user** (PD-41). *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* no questions from children until questions to the user (PD-23 to PD-26) are built; a child that needs the user returns `blocked`. Messages from the user reach the root only.
15. **Which hooks a child has** (PD-34). *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* its own job notices, the kernel check, the plan fence with progress on its track, math findings on its own writing, and its events; the consistency review stays on the root's terminal answer, which covers the results its children published.
16. **Delegation in M2** (PD-43). Part of M2, or the first milestone after it? The plan makes it M2 only on the owner's word. *Decided 2026-10-14 (coordinator, owner's autonomy mandate), as recommended:* after the M2 exit lab, with the run tree's parent link and per-run kernels built first, since the exit path needs neither.

## Sources

[External skills](../EXTERNAL-SKILLS.md) (host plans, progress, delegation, questions, structured outcomes), [architecture](../ARCHITECTURE.md) (model gateway and worker allocation), [execution plan](../EXECUTION-PLAN.md), [product](../PRODUCT.md), [implementation plan](../IMPLEMENTATION-PLAN.md), decisions D04, D10, D23, D25, D27–D29, D61, D65. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md) and [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md) reports. Public: Claude Code's [subagents](https://code.claude.com/docs/en/sub-agents) and [hooks](https://code.claude.com/docs/en/hooks) documentation; crypto-analysis-skills at `4089e99` (host adapter reference and board protocol).
