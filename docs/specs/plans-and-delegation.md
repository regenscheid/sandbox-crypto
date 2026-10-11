# Plans and delegation

Date: 2026-10-11. **Status: Draft, to review with the user.** W0 behavior specification; conventions in the [index](README.md).

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
- PD-5. A session has one current plan, with a status: `draft`, `awaiting_approval`, `active`, `completed` or `abandoned`. Earlier plans remain as artifacts (new).
- PD-6. Approval: when the project or the agent requires it, a plan version waits for the user's approval. Meanwhile the session's agents can read and plan but cannot start executions or external changes. Approval names one exact version and comes only from the user, never from tool output or an agent; a later version needs approval again (CScience has such a mode and the pack relies on it; the default setting is an open question).

### Progress

- PD-7. Progress is an append-only record, separate from plan versions: each entry names the step ID, the plan version, the status (`pending`, `in_progress`, `completed`, `blocked`, `skipped`), the actor, a note and optional evidence references. Progress carries over to new versions by step ID (new; CScience resets all step statuses when a revision is adopted, which loses history).
- PD-8. Progress entries are agent assertions. A `completed` step can link evidence (artifacts, executions, ledger entries), and the consistency reviewer checks completion claims against it.
- PD-9. A delegated agent updates only the steps of its own track (the pack expects this).
- PD-10. Marking a plan `completed` requires every step to be `completed`, `blocked` or `skipped`; otherwise the request is refused with the list of open steps (new; the pack's completion contract requires terminal steps).

### Delegation

- PD-11. A parent run starts a delegation with the task, the track (if any), a context summary, pinned input references, an output schema (the platform default, or one a skill supplies), a role profile (tools, skill set, model setting) and limits (turns, wall time, cost). The platform assigns a delegation ID that the request, the outcome and every related record carry (the pack's convention).
- PD-12. A track has at most one active delegation; starting another for it is refused while one runs (new).
- PD-13. A delegated run starts with a fresh context: its role instructions, the brief, its track of the plan, its skill catalog and the referenced inputs, not the parent's transcript (CScience; Claude Code's public subagent documentation). Skills the profile preloads are recorded as loads ([skills](skills.md), SK-21).
- PD-14. Delegated agents are logical: a delegation creates no container, and the child's tools use the session's environment ([architecture](../ARCHITECTURE.md)). Each delegated run gets its own kernels there, and sees its parent's results only through published artifacts and records (new). *Confirmed by the user on 2026-10-11 (D81): delegated agents are not separate sessions, and they stay in the session's environment; an environment of their own is a later opt-in. CScience likewise runs a session's child agents in the session's own sandbox, each with its own kernels and workspace directory.*
- PD-15. A delegated run cannot delegate further, and a session runs at most four delegated runs at once. Both are project settings (new defaults; CScience children cannot delegate, while Claude Code allows deeper nesting).
- PD-16. A delegated run acts under the session's grants and the project policy, with at most its parent's tools. It MUST NOT answer questions for the user, decide permission requests, approve plans, or claim or confirm fixes to review findings (CScience reserves confirmation to the root session). Operations it causes can raise permission requests, and the decision returns to that operation.

### Outcomes

- PD-17. Every delegation ends with one outcome record that separates what the platform observed from what the child asserted (new):
  - `ended`, observed by the harness: `returned`, `failed`, `cancelled`, `limit_reached` or `interrupted`;
  - `result`, the child's structured result validated against the output schema. The default schema has a status (`complete`, `partial`, `blocked`), a summary, results as pinned references, deviations from the brief and open questions;
  - the raw returned payload, stored unchanged, and any validation errors ([external skills](../EXTERNAL-SKILLS.md)).
- PD-18. If the payload does not validate, the harness asks the child once to correct it. If it still does not validate, the result is recorded as missing with the raw payload kept; defaults are never filled in (external skills).
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
- *When* a child tries to answer a pending question, decide a permission request, approve the plan, claim a fix for a finding and start a delegation.
- *Then* each attempt is refused with `not_permitted_for_role` and recorded.

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

1. Plan approval: off by default (proposed), or on? Should every new version of an approved plan need approval again (as drafted), or only substantive changes?
2. Progress across versions: carry over by step ID (proposed, PD-7), or reset as CScience does?
3. Delegation limits: depth 1 and four concurrent delegated runs per session?
4. Delegated runs get their own kernels (proposed, PD-14). Should a parent be able to share its kernel with a child deliberately?
5. Should plans also exist at project level, spanning sessions, or is that the board's job?
6. Questions with a default and a timeout (PD-26): useful, or should questions always wait for the user?
7. Which role settings should the GUI expose per delegation: the alias only, or effort and budgets too?
8. **Settled (2026-10-11, D81):** a delegation will be able to ask for an environment of its own, made from the project's base image, for work that needs isolation, such as an independent check; it is a later opt-in, in the backlog. By default delegated runs stay in the session's environment. Measured on babbage (OpenShell 0.1.3, VM driver): the image is prepared once per digest, and each further sandbox from it adds a sparse copy-on-write overlay of about 8 MB on disk, growing with what it writes; it starts in about 7 s and its VM uses about 220 MB of memory idle. Such a run would not see the session's installed tools or workspace, only `shared/`, so it would install again what it needs.

## Sources

[External skills](../EXTERNAL-SKILLS.md) (host plans, progress, delegation, questions, structured outcomes), [architecture](../ARCHITECTURE.md) (model gateway and worker allocation), [execution plan](../EXECUTION-PLAN.md), [product](../PRODUCT.md), [implementation plan](../IMPLEMENTATION-PLAN.md), decisions D04, D10, D23, D25, D27–D29, D61, D65. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md) and [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md) reports. Public: Claude Code's [subagents](https://code.claude.com/docs/en/sub-agents) and [hooks](https://code.claude.com/docs/en/hooks) documentation; crypto-analysis-skills at `4089e99` (host adapter reference and board protocol).
