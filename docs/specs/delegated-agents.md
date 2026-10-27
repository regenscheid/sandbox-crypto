# Delegated agents

Date: 2026-10-21. **Status: Draft, owner direction (2026-10-21).** Delegated agents should do nearly everything the main agent does: call connectors, read and write files, and use skills. Only the user's conversation and the main run's duties stay with the main agent. To be built after `agent-context-refactor` is merged into `dev`: tool selection by role now lives in that branch's `capabilities.py` role factories (its `docs/specs/capability-assembly.md`, which this page links once merged), and the references below are to that branch unless marked `dev`. This draft proposes amendments to [plans and delegation](plans-and-delegation.md) (PD-11, PD-16, PD-33, PD-34, the check paragraph and PD-F8) and to [skills](skills.md) (SK-8, SK-9), listed in [Amendments](#amendments). Until they are built, that spec describes the behavior. Conventions in the [index](README.md).

## Scope

What a delegated run (role `delegated`, kind `task` or `check`) may use: connector tools and their approvals, files in the session and the project's shared area, skills, and the PDF sources it reads as citation evidence. It also covers what the run is told about itself. Out of scope: the delegate tools' limits, outcomes and lifetimes (PD-15, PD-17 to PD-19, PD-38 to PD-40, D87), which do not change, and questions to the user, which stay with the main run (DA-15).

Terms:

- **Child**: a delegated run; **parent**: the run that delegated it; **root**: the main run at the top of its tree.
- **File grant**: a file the parent names in a brief, pinned by the platform at the version it had when the delegation started.
- **Write area**: the directory of the session a child's file tools may write in, `work/<delegation_id>/`.

## As built (2026-10-21)

What a child cannot do today, and where:

- **Connectors.** The harness builds connectors only for a run that is not delegated (`harness.py`, `connectors=… and not delegated`). `Toolbox` refuses them as well ("a delegated run gets no connector tools", `tools.py`). PD-33's check paragraph lists connectors among the tools a check does not get. No spec gives a reason beyond PD-16's rule on permission requests. *Superseded 2026-10-24: children get connectors (DA-3, DA-7 as built).*
- **Approvals.** A held connector call is keyed by project, caller, policy, server, method, tool and parameters (then `mcp_proxy/proxy.py`, `_escalation`; since the proxy retired, the policy middleware's request key). It is attributed to a session only, by the `Sandbox-Science-Session` header. `PendingApproval` (`workspace/approvals.py`) names the session, not the run, and the workspace lists requests by session. *Superseded: requests name the run (DA-4), and a child's are its own (DA-9, DA-10).*
- **Skills.** A skill is main-only unless listed otherwise (`skills/builtin.py`). Computation, environment setup, figures, literature search, PDF reading and recording results are main-only. Children get delegating-work, writing-documents and scientific-writing. *Since 2026-10-24 children also get computation, literature search and PDF reading.*
- **Files.** A child shares its parent's session directory (`_child_admitted` copies the session). The worker resolves `shared/…` to the project's shared area and any other path to `sessions/<session>/…` (worker `executor.py`). A child that writes `sessions/main/sources/a.pdf` therefore reaches `sessions/main/sessions/main/sources/a.pdf` and gets `not_found`, as observed on 2026-10-21. A brief's `inputs` are up to 20 free-text strings (`delegation/specs.py`, `MAX_INPUTS`), not pinned files. No path is read-only to a child, and a child may write anywhere its parent may.
- **Citations.** Only a main run has a review controller. A child's `pdf_pages` and ePrint reads are therefore never recorded as `source` observations (`consistency/controller.py`, `_source`). A quote the child found then fails the citation check, which looks sources up by project and session (`consistency/quotes.py`).
- **Prompts.** The delegated prompts say "You are a delegated research agent" and "You cannot talk to the user" (`delegation/prompt.py`). They mention neither connectors nor skills. *Since `sandbox-delegated-task-13` and `sandbox-delegated-check-13` they mention both (DA-26).*

## Behavior

### What stays with the main run

- DA-1. A task child is offered every capability its parent has, except those that act for the user or own the session's work: questions to the user (`ask_user`), the plan's authoring and status tools, the review tools (responding to findings, claiming fixes), and deciding permission requests. PD-16's prohibitions are unchanged. A check child keeps PD-33's read-only rule for the ledger, documents and memory. It also gets connector tools classed `read` (DA-7) and the reading skills (DA-21).
- DA-2. The selection is made by the trusted role factories (`main`, `task`, `check`). No agent text selects a factory or widens a scope. A parent may narrow what a child gets (DA-6, DA-23), never widen it beyond its own.

### Connectors

- DA-3. Task and check children are offered `mcp_tools` and the connector tools it loads, under the same project policy as the main run. The harness builds the run's connectors for every role whose factory offers them. `Toolbox`'s refusal for delegated runs is removed. *Built 2026-10-24 ([D99](../DECISIONS.md)):* a child's connectors are built as its root's, through the same client (the proxy's or the direct one) and the same index, `mcp_tools` and on-demand schema loading; a tool called before it was loaded is loaded and called, not refused for the role. The policy decides each call by class exactly as for the main run; a task's tools are not narrowed by class. A check is offered only the tools the proxy classes `read` (DA-1, question 3), so behind a client that classifies nothing, such as the direct one, it gets none. A task's brief may narrow them (DA-6, built 2026-10-27).
- DA-4. Every connector call names the run that made it. The run service sends a `Sandbox-Science-Run` header with the run's name next to `Sandbox-Science-Session`. The proxy accepts the header only from the run service's caller, never from a worker or sandbox caller, and records it in the escalation event and the pending request. The run is attribution only: it is not part of an approval's key or scope. Approvals stay once or for the whole project (the existing `once`, `project` and `server` decisions), never for one run (owner, 2026-10-21). *Built 2026-10-24:* every request of a run's connectors, the listings included, names its run (`connectors.RUN_HEADER`), a main run's as a child's; the MCP session with each server is shared by the runs of one client. The proxy believes the header from the run service's caller only: a worker caller's run is kept as `run_claim`. The attribution in every `mcp-call` and escalation record carries `run`, and the operator listing of waiting requests gives it (`PendingApproval.run` in the workspace). A request's key does not hold it: the same call from another run is the same request.
- DA-5. A child's MCP sessions are its own, or shared with its tree with the run named on each call; which is an implementation choice. A child's connector calls count toward the project's daily cost limit and the proxy's rate limits, together with every other run of the project.
- DA-6. A task brief may name the connector servers, or `server/tool` names, the child may use (`connectors`; default: all its parent may use). The child's `mcp_tools` index lists only those, and loading any other is refused with `not_permitted_for_role`. *Built 2026-10-27 (`delegation/narrowing.py`):*
  - **Entries.** At most 32, each a configured server (`crypto-research-mcp`), a server behind it as the index names it (`crypto-research-mcp/e-print-mcp`, or `e-print-mcp` alone when the parent's index has it under one configured server, recorded with the server), a tool by its full name (`SERVER/TOOL`), or a shell-style pattern of tool names (`SERVER/e-print-mcp_search_*`). A check takes none: its connectors are the platform's (DA-1), and a check brief that names some is refused with `not_permitted_for_role`.
  - **Only narrowing.** At delegation, an entry whose server no configured connector is, or that is malformed, refuses the brief with `invalid_request` (`unknown_connector`). An entry that admits nothing the parent is offered refuses it with `not_permitted_for_role`: a server the parent lacks (the user turned it off, or the parent's own brief left it out), or, where the parent's listing of the server came, a tool, a server behind it or a pattern none of the parent's tools matches. Where the parent's listing failed, an entry of that server is taken and the child's own listing decides.
  - **Recorded.** The delegation's brief keeps the entries as normalized (`connectors`) and every narrowing above them (`inherited_connectors`, a narrowed task's children's), and the child's connectors are built from that record, so a resumed or reopened run gets the same tools. A record the platform cannot read narrows to nothing.
  - **Enforced where the child's connectors are built** (`Connectors`, with the root's client and index settings): a tool is offered only when every level admits it, the brief's own and each inherited one, on top of the child's default (the servers the user did not turn off; for a check, the tools classed `read`); a server no level names is not listed. The index opens with the narrowing, `mcp_tools` answers a load of a listed tool it leaves out under `not_permitted`, a call of one is refused with `not_permitted_for_role`, and the run's record keeps the narrowing (`summaries.connectors.narrowing`). Tests: `test_connector_narrowing.py`.
  - The delegated task prompt does not mention it yet (B7 owns the prompts).
- DA-7. A connector tool is an effect, as today, unless the project policy classes it `read`. The policy can class a whole server `read` and name the tools that are effects. Most research servers' methods only read (search, fetch, records, full text), so a server is usually classed `read` with a few exceptions. A server's own `readOnlyHint` is not trusted. The plan fence (`ChildFence`) lets read-classed connector calls through while the plan is a draft or after the user changed it, as it does other reads. *Amended 2026-10-23 ([D96, D99](../DECISIONS.md)):* a tool the classification does not mention takes its `readOnlyHint`; the fence gates outward effects only, and a plan in draft is a UI signal, not a block. *Built 2026-10-23 (D99, interim until D96's classes):* under a draft or user-edited plan `ChildFence` refuses only outward calls. The built-in tools whose effects stay in the session pass (`delegation/effects.py`: `shell`, `python`, `sage`, `restart_kernel`, `write_file`, `edit_file` (added 2026-10-24), `pdf_render`, `start_job`, `provision`), `pdf_info`, `pdf_pages` and `pdf_search` are `read`, as `read_file`, `list_files`, `glob` and `grep` are, and every other `effect` tool is outward: connector tools, `publish`, `admit_hosts`, `delegate`, ledger and document writes. A session closed or opened again still refuses both. HTTP from a child's code is gated where it leaves the sandbox (the middleware's escalations), not by the fence. The main run's plan gate is unchanged. *Built 2026-10-24 (D96's classes):* the fence reads each connector tool's class and escalation mark as the MCP proxy listed them (`sandbox-science/effect`, `sandbox-science/escalates`; a client that does not classify gives neither). A tool the proxy classes `read`, which no argument can make more, passes as computation does. A tool the proxy says asks a person first also passes under a draft or user-edited plan: the proxy holds the call, and the user decides it from the child's card (DA-9), so the plan is a signal there, not a block. Any other connector call, one the policy would let through unasked, is refused under a draft or user-edited plan. A session closed or opened again refuses all of them.

### Approvals

- DA-8. A child's call that needs approval takes the main run's path. The proxy refuses it with `approval_required`, the run service retries it while the request is pending (`Connectors.call`), and the workspace shows the card. Only the user decides (PD-16, D76). No agent, child or parent, can answer, relay or restate the request: an attempt to answer one is refused with `not_permitted_for_role`.
- DA-9. The card says who asked. `PendingApproval` gains `run` and `delegation` (null for a main run). The card names the agent (its brief's title, kind and depth), links to its activity, and takes the call's arguments from that run's transcript. Cards stay in the session's chat with the main run's, oldest first. *Built 2026-10-23 ([D99](../DECISIONS.md)), backend first:* a child's wait (`Connectors`' `on_approval`, `delegation/requests.py`) records the request in `child_requests`: the proxy's request number, delegation, run, server, tool and the child's own words (2,000 characters). The parent's brief, its task or its check and note (2,000 characters), is read from the delegation. The workspace's listing adds `child` (delegation, run, kind, brief, words), and the card says a delegated agent asked and shows its brief and words as agent text. Attribution comes from that record; the proxy's record also names the run (DA-4). *Since 2026-10-24* children have connectors, and a child's held call takes this path end to end: the card, the parent's notice, its withdrawal and the proxy's denial.
- DA-10. The parent is told, not asked. While a child's call waits, the parent's context gets a notice like a job notice: the delegation, the server and tool, and since when. A second notice says when the request was answered, denied or timed out. The parent may go on, cancel the child, or send it a message (`message_child`, PD-44). The delegation's status reads `waiting_for_user` in its record and in the Agents pane. *Built 2026-10-23 (D99):* the parent is told through the agent mailbox, one platform notice per request, sent as the child's. A run that may message its children is offered `withdraw_child_request` (class `cancel`), for its direct children's requests only. A withdrawal only refuses: the workspace stops showing the card, refuses to approve it, and denies that one call at the proxy, so the child's call ends as a denied one. No agent tool approves (DA-8). Not built: the settled notice, `waiting_for_user`, and a withdrawal-specific message to the child (the middleware reports a denial). *On the direct path (2026-10-27):* the same record, notice and withdrawal, since the wait is the host's (`Connectors.call` with the run's `ChildApproval`, whatever its client): a child's call the policy middleware holds is recorded under the middleware's request number, once the host confirmed the request for that very call; the parent is told; a withdrawal hides the card, the workspace refuses to approve it, and its relay denies the request at the middleware, so the child's retry ends `approval_denied`. A session that spans the proxy's retirement can hold an older record of the same number for another call: that wait tells no parent and cannot be withdrawn, and its card names the child from the delegation alone. A detection hold of the session (D103) is not a child's request: it refuses the call (`session_held`), and only the user clears it. Tests: `test_mcp_direct.py` and the workspace's `test_routes_direct_approvals.py`, with the real middleware and worker.
- DA-11. Time spent waiting for the user does not count toward the child's turn, token or wall-time limits. A parent's `wait_delegations` is unaffected.
- DA-12. A denial or timeout returns the proxy's refusal to the child's call, as for a main run. The child goes on without it, and its outcome lists the refused calls.
- DA-13. Approvals the user remembered for the project or a server cover every run of the project, children included, so a child is not asked again for what the user already allowed. *Built (D97 grants):* a child's call is the agent's (caller kind `agent`) in its root's session, so a class grant for the project covers it as it covers the main run's, and a session grant covers the session's children.
- DA-14. *Alternative considered:* routing a child's request through its parent agent, which would decide or ask the user itself. Rejected:
  - A parent deciding is forbidden by PD-16.
  - A parent asking would put an agent's restatement in front of the user, the self-escalation the threat model names (A3: "Agent text never authorizes anything").
  - The parent is often waiting or working, and relaying costs model calls.

  DA-10 gives the parent what it needs to plan, without the authority.

### Questions to the user

- DA-15. Unchanged. `ask_user` is main-only (PD-41). A child that needs the user's input tells its parent, either with `message_parent` while it goes on (PD-44) or by ending with result status `blocked` and its `open_questions` (PD-17). The parent decides whether to ask the user, in its own question, which the user reads as the parent's. *Amended 2026-10-24 by the owner:* a child that lacks context asks its parent the same way, and the parent's answer may carry `references` to its own records (PD-44, CM-37b), which widen what the child's state block holds and its `history_read` may read; the parent need not retell them. *Built 2026-10-24 (`sandbox-delegated-task-14`):* the task prompt says so; a check takes no references.

### Files

- DA-16. A path a child passes to a file, PDF or artifact tool that starts with `sessions/<its own session>/` is refused with `path_outside_session`. The message names the path relative to the session. The worker does not guess, since `sessions/` may be a real subdirectory.
- DA-17. **File grants.** A brief's `inputs` may name files, by path in the session or `shared/…`, as well as free text. At delegation the platform resolves each named file to its path, sha256 and size, and records it as a recorded file's revision (RA-27) if it is not one already. The child reads a granted file at that revision, even after the parent changes it. A grant is not a copy: an unchanged file is the same content-addressed revision. A directory grants the files in it, up to the brief's 20 inputs counted per file. A path that does not exist refuses the brief with `invalid_input`.
- DA-18. A child may still read any file in the session and in `shared/`, as today. A grant pins a version and tells the child what to read first. It is not the only file the child can see.
- DA-19. **Write area.** A child's file tools (`write_file` and `edit_file`) write only in its write area, `work/<delegation_id>/`, and in `shared/` with the existing `expected_sha256` or `create_only` guard. A write elsewhere in the session is refused with `outside_write_area`. Its parent and the rest of the session can read the write area. The results a child declares in its outcome (RA-36, RA-38) are paths there or in `shared/`. A check gets a write area for scratch work, and its read-only rule for ledger, documents and memory is unchanged.
- DA-20. A child's shell, Python and Sage executions run in the session's environment with their working directory in its write area, and `$RUN_WORK` names it. DA-19 binds the file tools. Holding a child's processes to their write area needs per-run mounts in the sandbox. The owner requires it, after this draft's first build (question 1). A draft or user-edited plan does not hold these executions (DA-7, D99).

### Skills

- DA-21. **Most skills, on demand.** A child is offered the project's pinned skills as a main run is, not a set chosen for its task. What a child will need is not known when it starts (owner, 2026-10-21).
  - **Imported skills** go to every main and delegated run, checks included. Packs do not say which role a skill is for, and the platform reads no role from them (owner, 2026-10-21). A skill whose workflow needs a tool the child lacks is still offered; the call is refused (`not_permitted_for_role`, SK-14).
  - **Built-in skills** are the only ones the platform limits by role, by the list in `skills/builtin.py`. A built-in skill about a main run's duties (the plan, reviews, the user's questions) stays main-only. Delegating-work goes only to runs with the delegate tools, as today, and recording-results only to task children.

  This reverses today's default, under which a skill not listed is main-only. Of the built-in skills, children gain:
  - computation (built, D99), environment-setup and figures;
  - literature-search, with DA-3 (built 2026-10-24, tasks and checks);
  - pdf-reading, with DA-24 (built 2026-10-24, tasks and checks, before the source recorder);
  - recording-results, task children only, within LG-13's authority.
- DA-21a. **Found while working.** "Offered" means the skill is reachable for the child's whole run, by the same means as for a main run:
  - it is in the child's skill index;
  - the `skills` tool searches, loads and reads it at any point;
  - discovery notices name the skills that match the child's brief, and each message it receives (`message_child`, PD-44).

  A loaded skill is held through folding as a main run's is (CM-29b).
- DA-22. **Text written for a main agent.**
  - *Built-in skills* offered to children speak to "the agent", not "the main agent". They do not tell their reader to ask the user, change the plan or respond to the review, except in a section marked for the main run, which a child is not shown. Literature-search's line that the main run does the searching and hands a task its results is removed. A test fails any built-in skill offered to children that names a main-only tool (`ask_user`, the plan and review tools) outside such a section.
  - *Imported skills* are used as written, and many will address an agent that talks to the user. The child's prompt says how to read them (DA-26): where a skill says to ask the user, the child tells its parent; where it says to change the plan or answer the review, the child reports to its parent instead.
- DA-23. A task brief may name skills to load at the child's start (`skills`; only skills the child is offered). They are loaded as a main run's pinned skills are. This only preloads skills: it never limits what the child may find and load later (DA-21a).

### Citations

- DA-24. Task and check children get a source recorder: a read-only hook that writes the same `source` observations the review controller's `_source` writes, for `pdf_pages`, `pdf_search` and ePrint reads, under the root's session, naming the child run. The citation check's lookup is unchanged and finds them, since it looks sources up by session.
- DA-25. A child's outcome lists the sources it read: path, sha256 and the pages or records. Its parent can cite them without reading them again, and the reviewer sees them with the child's actions (`consistency/delegated.py`).

### What a child is told

- DA-26. The delegated prompts (`sandbox-delegated-task`, `sandbox-delegated-check`, new revisions) add:
  - its tools include connectors and skills;
  - a connector call may pause for the user's approval, and the child waits for it, never working around a refusal by another route;
  - it reports to its parent, which alone hears from it (its outcome, or `message_parent` for a finding, question or blocker on the way, PD-44), and it never addresses the user;
  - it may delegate only when offered the delegate tools (PD-15);
  - a skill may be written for an agent that talks to the user or owns the plan. Where it says to ask the user, change the plan or answer the review, the child tells its parent instead (DA-22).

  Each prompt keeps its first line, "You are a delegated research agent".

  *Built 2026-10-24 (`sandbox-delegated-task-13`, `sandbox-delegated-check-13`):* both say what `mcp_tools` loads (a check's: the tools that only read), that a call needing approval waits while the user decides it from a card naming the child and its brief, with its parent told, that a denial is not worked around, and that a skill's advice to ask the user, change the plan or answer a review goes to the parent with `message_parent`. The task prompt adds that connector reads go on under a draft or changed plan and other outward calls wait for the user or are refused. The rest of DA-26 was already said.
- DA-27. <a id="b9-3"></a>*Decided 2026-10-25 by the owner (B9); built 2026-10-25:* **Tool lessons travel to children.** A rule for the prompts, not a new field of the brief.
  - **The parent** (the MAIN prompt, and the task prompt for a child that may delegate, PD-15): a brief's `context` (PD-11) carries what the parent learned operating the tools in this session that the child would otherwise learn again. For example: a query form that works on a connector; a fetch that stalled, and what worked instead; an approval the user already granted; a limit it hit, such as a search's 200 results or a server's rate limit. Each is stated as an observation, such as "the NIST server answered 429 after about 20 requests in a minute", with its evidence named by `references` (PD-11, B8), which the platform materializes for the child (CM-37b), rather than retold. A later `message_child` does the same (PD-44).
  - **The child** (the task prompt): its result may give `lessons`, at most 5, each at most 200 characters on one line: what it learned operating the tools that a later run could use, stated as observations. The parent sees them in the outcome, and may pass them on to later children, in a brief's `context` or a `message_child`, as its own text.
  - **What they are.** Lessons, and a context's observations, are agent text: checked as any agent text is (`check_agent_text`), and framed to their reader as the parent's or the child's notes, data and not instructions (CM-49). They never state policy and never claim a permission. "An approval already granted" tells the child that the user approved something; the policy and the user's grants (D97, DA-13) still decide each call. No call is allowed or refused because a lesson says so, and no code reads them. They are not project memory: a lesson worth keeping for later sessions is proposed to the user (PM-2).
  - **The records.** Lessons are kept in the task's result (`sandbox-task-result-3`, PD-45) in the `delegations` table's `result`, and the context in the brief, as today. No migration is needed.
  - **The workspace** shows a child's lessons in its outcome block (B10), as the child's words.
  - *As built (2026-10-25):* `sandbox-main-17` and `sandbox-delegated-task-15` carry the rules; a task's result takes `lessons` (`delegation/outcome.py`), and the outcome a parent reads gives them apart from the result as `lessons`, `{framing, notes}`, the framing saying they are the child's notes, data and not instructions. A check's result with `lessons` is invalid. Tests: `test_delegation_deliverables.py`.
  - **What stays the same.** A check gets neither. Its brief is the platform's (LG-17), and the parent's only words in it are its note of at most 500 characters, the requester's note, not evidence; `sandbox-check-result-2` has no `lessons`. The brief's limits are unchanged: `context` at most 8,000 characters, `references` at most 40. Building this moves `sandbox-main-16` and `sandbox-delegated-task-14`.

## Amendments

When this draft is built, [plans and delegation](plans-and-delegation.md) changes as follows:

- **PD-11**: "pinned input references" are file grants (DA-17). The role profile includes the connectors and skills of DA-6 and DA-23.
- **PD-16**: unchanged rule. Add: "A permission request raised by a delegated run's operation names that run. The user decides it, and the run's parent is told while it waits (DA-9, DA-10). No run decides or relays it."
- **PD-33**: `delegate` takes `connectors` and `skills` for a task, and `inputs` naming files as grants. The check paragraph's "without the review, plan and delegate tools and connectors" becomes "without the review, plan and delegate tools, and with only the connector tools the policy classes `read`".
- **PD-34**: the toolbox is "at most its parent's tools minus those PD-16 forbids", which now includes connectors (DA-3). Its hooks add the source recorder (DA-24) and approval notices to its parent (DA-10). Its file tools write in its write area (DA-19).
- **PD-F8**: add that C2's attempt to answer a pending connector approval is refused, and that C2's own connector call waits for the user, with a card naming C2.
- Capability assembly (`docs/specs/capability-assembly.md`, on `agent-context-refactor`): "Main agents may receive reviews, plans, questions and connectors" becomes "Main agents may receive reviews, plans and questions; main and delegated runs receive connectors, narrowed for a task by its brief."

[Skills](skills.md) changes too:
- **SK-9**: a delegated run's skill set is every imported skill, and the built-in skills the platform's list gives it (DA-21). The reviewer's set is unchanged.
- **SK-8**: a `roles` requirement in an imported skill's front matter is not read for main and delegated runs. Packs do not say which role a skill is for.
- **SK-37**: unchanged. A child runs its own discovery, and on each message it receives (DA-21a).

To record in the [decision register](../DECISIONS.md) when approved: delegated runs get connectors, skills by capability and file grants. Their permission requests go to the user directly, attributed to the run, and their parent is told.

## Contracts used

- `PermissionRequest`: gains `run` and `delegation`. The decision is routed to the operation that caused it (PD-16).
- `ArtifactRef`: a file grant is a recorded file's revision with its sha256 (RA-27).
- `Event`: `approval.waiting` and `approval.settled` to a child's parent; `delegation.status` with `waiting_for_user`.
- `ExecutionContext`: a child's executions name its run and write area.

## Acceptance fixtures

All fixtures are `scripted`: the agents, the proxy's servers and the user's actions are scripted.

**DA-F1 A child's approval** (DA-4, DA-8 to DA-10)
- *Given* a task child C1 and a policy that asks before `e-print/eprint_fulltext`.
- *When* C1 calls it, C1 tries to answer its own request, and then the user approves once.
- *Then* the card names C1 and shows its arguments, C1's answer is refused with `not_permitted_for_role`, the parent got a waiting and a settled notice, and C1's call returns the result.

**DA-F2 Remembered approvals and denials** (DA-12, DA-13)
- *Given* the user approved `paper-search` for the project.
- *When* child C1 calls a `paper-search` tool, then calls an `e-print` tool that the user denies.
- *Then* the first call is not asked again; the second returns the refusal, and C1's outcome lists it.

**DA-F3 Narrowed connectors** (DA-6)
- *Given* a brief with `connectors: ["e-print"]`.
- *When* the child loads a `paper-search` tool.
- *Then* it is refused with `not_permitted_for_role`, and the child's `mcp_tools` index lists only `e-print`.

**DA-F4 File grants and the write area** (DA-16 to DA-19)
- *Given* a brief granting `sources/a.pdf`.
- *When*:
  - the parent overwrites `sources/a.pdf`, then the child reads it;
  - the child writes `work/<id>/notes.md` and then `sources/b.md`;
  - the child reads `sessions/main/sources/a.pdf`.
- *Then*:
  - the child reads the granted revision;
  - the first write succeeds and the second is refused with `outside_write_area`;
  - the third read is refused with `path_outside_session`, naming `sources/a.pdf`.

**DA-F5 A child's quote** (DA-24, DA-25)
- *Given* a child that reads page 5 of a granted PDF and quotes it in a ledger entry it records.
- *When* the consistency review runs.
- *Then* the citation check finds the source and confirms the quote, and the outcome lists the source.

**DA-F6 Skills for children** (DA-21, DA-22)
- *Given* a project with an imported pack whose skill `cryptanalysis-notes` says "ask the user which parameter set to use", a task child with connectors, and a check child.
- *When* each lists its skills, and the task child loads `cryptanalysis-notes`.
- *Then*:
  - both children are offered `cryptanalysis-notes`;
  - the task child is offered literature-search, pdf-reading and recording-results;
  - the check child is offered pdf-reading but not recording-results;
  - neither is offered a built-in skill marked main-only;
  - the task child sends the parameter question to its parent with `message_parent`, and calls no `ask_user`;
  - the lint test passes on the built-in pack.

**DA-F7 The fence and read-classed tools** (DA-7, D99)
- *Given* a draft plan and a policy that classes `e-print/search_eprint` as `read`.
- *When* a child runs Python, writes a file in its write area, reads a PDF, calls `search_eprint`, and then calls a connector tool classed `effect`.
- *Then* all but the last run, and the last is refused by the fence.

**DA-F8 A skill found mid-run** (DA-21a)
- *Given* a task child whose brief is about reading a paper.
- *When* its parent sends it a message asking for a figure of a result, and the child then searches its skills for plotting.
- *Then* a discovery notice names the figures skill, the search finds it, and the child loads it, though the brief named no skills.

## Questions

All decided by the owner on 2026-10-21.

1. **Holding a child's processes to its write area.** DA-19 binds the file tools only; a child's shell can still write anywhere the session's sandbox can. *Decided:* required, but after the first build. It needs per-run mounts, with the session read-only and the write area writable, once the confinement work allows it. Until then, the write area is a convention that the file tools enforce.
2. **Per-run approval scopes**, such as an approval for one child only. *Decided:* no. Approvals stay once, for the session or project-wide (grants, D97), and the run is attribution (DA-4).
3. **Check children and connectors.** *Decided:* only tools classed `read` (DA-1, DA-7). Most connector methods only read.
4. **A connector budget per delegation.** *Decided:* no separate budget. Calls count toward the project's cost limit, and the outcome reports how many the child made.

## Order of work

1. Path refusal, file grants and the write area (DA-16 to DA-19). Worker, brief schema, recorded-file revisions.
2. Run attribution through the proxy, `PendingApproval` and the card, plus the parent's notices and `waiting_for_user` (DA-4, DA-8 to DA-11).
3. Connectors in the task and check factories, the brief's narrowing, and policy `read` classes with the fence (DA-3, DA-5 to DA-7).
4. The source recorder and outcome sources (DA-24, DA-25).
5. Skills by capability, the skills' role-neutral text and lint, and the prompts (DA-21 to DA-23, DA-26).
6. The PD and SK amendments, the capability-assembly line, [MCP connectors](../MCP-CONNECTORS.md) approvals and the decision record.
7. Later, required: per-run mounts that hold a child's processes to its write area (question 1).

The agent runtime runs in the orchestrator image, so steps 2 to 5 need an image rebuild.

## Sources

- [Plans and delegation](plans-and-delegation.md): PD-11, PD-15 to PD-17, PD-33, PD-34, PD-41, PD-44, PD-F8.
- Capability assembly, `docs/specs/capability-assembly.md` on `agent-context-refactor`.
- [MCP connectors](../MCP-CONNECTORS.md), "Approvals in the workspace"; D76 in the [decision register](../DECISIONS.md).
- [Threat model](../THREAT-MODEL.md): A3 (agent self-escalation) and T2 (prompt injection from consumed content).
- [Questions](../QUESTIONS.md).
- Owner direction and observations of 2026-10-21:
  - a child's `not_found` on `sessions/main/…` paths;
  - the main run reading every paper itself, because children lacked connectors, the PDF skills and citation sources.
