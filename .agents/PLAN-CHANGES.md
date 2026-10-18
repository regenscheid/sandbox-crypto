# Plan changes

Dated list of changes to the plan, decisions and conventions ([conventions](CONVENTIONS.md)), newest first, one or two lines each. Progress and lab results are in PR descriptions, [lab reports](docs/agent-reports/) and the git log. The earlier long-form log is in git: `git show 0e6a662:.agents/PLAN-CHANGES.md`.

## 2026-10-18

- Resource check-ins now support durable parent turn decisions and exact USER +10,000,000-token approvals, retaining new 20,000,000-token defaults and existing explicit settings. Live context inspection starts at recent activity with separate auxiliary-call provenance. The scientific evaluation is prepared and awaits its separate paid-run approval.
- The owner requested the remaining live-timeline, adaptive-resource and scientific-quality work. Reuse existing usage/events for bounded live context views; begin resource renewal with durable parent check-ins and turn allowances, retaining explicit USER approval for each additional 10,000,000 tokens. Validate reviewer correction and folded-context recall against a reproducible scientific oracle before claiming research quality.

- Context inspection now follows immutable same-agent continuation identity, with separate parent/child scopes, cursor-based automatic search, bounded per-click work and abortable requests. Sparse searches first bound their session index slice; unsaved attempts are labelled without broken links ([spec](../docs/specs/context-inspector.md)).
- Add actual subprocess-death acceptance at final save/ACK/work-finish and child ledger/receipt boundaries, reusing public store operations and excluding model/worker calls. Keep this distinct from OpenShell/host restart qualification.

- Lost-kernel recovery now has a narrow local replay path: journal complete opt-in literal Python cells, fence unresolved reconstruction across main work, and require an explicit decision for other state. After accepted loss, clear the lineage without starting an empty kernel; live worker qualification remains open (D87).
- The owner reaffirmed that unknown model charges must not stop local run admission. The optional daily gate compares observed costs plus explicit reservations with its unchanged cap; unknown calls remain visible and the upstream LiteLLM budget is the primary spending control (D88, RS-6a).
- **D35 reviewer effectiveness follow-up** (SNOVA audit; [consistency review](../docs/specs/consistency-review.md), CR-2 and CR-34): bounded, contiguous evidence spans replace repeated oversized windows; capped retries shrink, partial delegated spans retain provenance, and terminal holds catch up within a configured bound. Clean global certification and idle backlog draining remain separate work.
- The reviewer audit found missed boundary contradictions and failed-span retry storms. Adjacent windows now carry bounded exact prior evidence; clean prefixes can pass their own spans, but inconclusive historical spans prevent a session-wide pass. Repeated failures defer without advancing the watermark (CR-2, CR-34 amended).

## 2026-10-17

- **Research-tree steering** (the owner, D87; [PD-44](../docs/specs/plans-and-delegation.md)): the root routes user messages among its own work, a new or revised plan, child assignments, and feedback on research records. Direct parent-child messages are durable and delivered at saved-run safe points; an ended recipient causes a delivery-failure notice to the root.
- The owner requires configurable model reasoning effort by role and normally uses `max` for research; the live trial had inherited `medium`. Requested profiles are now pinned across Resume, with explicit operator effort overrides audited without rewriting original inputs (`43889b45`); live provider verification remains pending.
- Extended cryptanalysis must iterate beyond initial pilots/checklists. MAIN/planning/stock guidance now distinguishes bounded tasks from extended exploration; requested skill workflows also require reading applicable supporting references and applying their procedures, not just declaring Discover mode.
- The owner clarified that the shared $400 research figure may be tracked locally as an estimate while LiteLLM enforces spending. Unknown model charges remain visible, but no longer block explicit tree Resume by themselves; ten bounded HTTP attempts no longer depend on per-attempt local admission (D88, RS-6d/e).

- The owner requested streaming model requests and ten increasingly delayed attempts after SNOVA's 524 timeouts. The GPT Responses adapter now requests SSE and commits only a matching terminal response; a short live gateway probe and isolated `141ba43e` image deployment passed. At that point ten-attempt handling awaited per-attempt accounting; the clarification above supersedes that dependency. Long pre-response behavior still needs acceptance (D88, RS-6e).

- The owner clarified that an unknown provider price should not indefinitely block SNOVA research when the operator has made an explicit conservative reservation. A legacy fold estimate without an intent ID now requires an exact audited USER link to that reserved intent; unrelated unknowns and known charges remain counted ([run service](../docs/specs/run-service.md), [recovery controls](../docs/specs/recovery-controls.md)).

- The owner wants renewable research allowances: resource warnings go to the calling agent, which can grant extensions within policy and hard outer budgets. [Adaptive resource policy](../docs/specs/adaptive-research-resources.md) records the direction; narrow USER cumulative token renewal is implemented, while broader parent policy remains planned.

- Real SNOVA research reached the 8 MiB active-payload bound before token folding because provider responses echoed tool schemas and usage attribution. Preserve originals in the archive, project active native replay fields, and raise the per-agent active backstop to 64 MiB at the owner's request.
- Local recovery acceptance exposed suspended environments at explicit Resume: prepare the recorded worker before its retention claim, without recreating missing environments or dispatching research before readiness.
- Local Babbage acceptance may use the owner's authorized arrcodex models, with a $200 total ceiling for SNOVA planning, literature/MCP/PDF, Sage, delegation and long-context trials. Expose the existing context ceiling through the confined launcher for bounded fold/recovery tests; retain the default and provider cap ([continuity](../docs/specs/research-continuity.md)).

- **Agent context inspection (D80):** the owner requested a secondary embedded debug dialog for every agent, including the orchestrator: usage/fold timeline, cache performance, current-view provenance and searchable original history ([proposal](../docs/specs/context-inspector.md)).

- **Larger research files (D79/D80):** 100 MiB uploads with 2 GiB/project and 8 GiB/store retention; larger worker writes and shared upload copies, retaining ordinary read limits. Schema 67 and worker protocol 9 require coordinated upgrades.

- **UI review refinements (D80):** research pane drags continuously up to 50% of the window; Documents gets a searchable/filterable library and named tabs retaining drafts. The owner accepted the ledger's list/detail and optional board direction, requested persistent session navigation, and requested broader screen refinement. A connected [workspace browsers proposal](../docs/specs/workspace-browsers.md) prioritizes Skills and MCP services; production adaptation remains pending.

- **Return to the UI (D80 amended):** left project/session navigation, central chat with uploads, and an optional documents/editor/plan/agent pane. OpenScience web UI is an Apache-2.0 interaction reference; originals are immutable USER records and sandbox copies go through the worker API ([contract](../docs/specs/research-ui.md)).

- Resume claims atomically admit only their selected tree's held, unconsumed inputs; the workspace no longer globally releases other trees or terminal input ([tree Resume](../docs/specs/tree-resume.md)).

- Preserve consolidation context across Resume and service replacement; ledger changes clear the prerequisite but do not authorize paid continuation. Canonical receipts replace final-save-only delivery ([run service](../docs/specs/run-service.md)).

- **Initial recovery is explicit:** process restart and `replace-orchestrator` preserve unfinished work but do not authorize another paid call. Resume claims a new epoch, reconciles original effects and schedules descendants before parents ([contract](../docs/specs/research-continuity.md#explicit-tree-recovery-coordinator)).
- **Durable boundary and bounded history:** the normal launcher uses a separate agent VM with fixed scoped host capabilities; historical collections and exact paid-call links replace whole-run in-memory copies ([confinement](../docs/specs/orchestrator-confinement.md), [accounting](../docs/specs/auxiliary-usage.md)). Provider cache ordering remains unchanged pending live measurements.
- **Observed model usage (D87 implementation refinement):** save response usage/cost atomically with the archive and use its observation time for the daily budget, so multi-day runs keep counting. Legacy fallback retains run-start time; pre-dispatch model intent and unknown charges remain recovery work ([contract](../docs/specs/research-continuity.md#observed-model-usage-increment)).


## 2026-10-16

Decisions of the owner, later the same day (after #467):

- **Research foundations before the UI redesign:** parents retain their children until the work is finished, and interrupted work must be resumable at every level (D87). The [continuity design](../docs/specs/research-continuity.md) is a draft; bounded memory and delegated folding remain unbuilt, and automatic crash resume is undecided.
- **Additional foundation review items:** the owner asked to consider summary placement for cache reuse, trimming stored tool output, and generating the skills variable blocklist from installed tools. The continuity draft records the current implementation, proposed safeguards and acceptance measurements (RC-20 to RC-24); this is a review request, not a decision to reorder context or delete evidence.
- **Resume through the orchestrator:** the owner clarified D87: one orchestrator action must recover the selected tree without restarting each agent separately. The [OpenShell review](docs/agent-reports/openshell-foundations-review-2026-10-16.md) adds proposed work for idle suspension and uncertain creation cleanup; automatic crash recovery is still undecided.
- **Separate orchestrator sandbox (D47, accepted by the owner):** run the agent-facing orchestrator in its own restricted OpenShell sandbox, separate from research workloads, with trusted worker/OpenShell management outside both. Parent and child model loops may share that service. Add scoped service access, durable state and the UI/store boundary before launcher integration; verify orchestrator replacement and one-action tree resume. This is agreed architecture, not implemented normal-launcher behavior.

- **The figures spec is approved as it was drafted,** with its eight questions answered as recommended (#436; [figures](../docs/specs/figures.md); [specs README](../docs/specs/README.md), question 19). The build plan is in the [BACKLOG](BACKLOG.md#repository); F1 to F5, F9 to F11 are built.
- **The response-loop guard** (#489; [consistency review](../docs/specs/consistency-review.md), CR-16, CR-21, CR-26, CR-29 as amended). **A:** a rebuttal that cites recorded evidence and that the next review rejects leaves the finding `challenged`: "the author's rebuttal stands and carries more weight than the reviewer's", the reviewer's reason is visible context, not a block, no round follows, and the orchestrator is told once. **B:** the session's earlier findings reach a run as counts, and the orchestrator may follow one up; only the open findings its own work raised interrupt it.
- **K7's allow rule is option 3** (#486; [skills](../docs/specs/skills.md), SK-31, SK-33): a binding namespaced to the pack is set at once, and any other only when the project's user approves it. The deny-list stays as a backstop.
- **Context-management lab round 1:** USD 500 at most, on the gateway's `arrcodex` GPT aliases alone, with the Claude arms deferred (#481).
- **Citing the ledger handle** (CM-P1; [context management](../docs/specs/context-management.md), question 12; #509): "keep it as a prompt, and add it as a reviewer check, but do not block or reject if the agent still does not cite it." The lab's bar is the reason and the reopen condition, and the citation is reported. Built as a prompt rule (`sandbox-main-9`, `sandbox-delegated-task-6`) and an advisory reviewer category, `handle_citation`, stored as a note.
- **A personal instance on babbage,** apart from the labs, for the owner's own use (#510; [the personal instance](../docs/PERSONAL-BABBAGE.md)).

Decisions of the coordinator, each reversible by the owner:

- **Calls with no gateway cost are priced from `/model/info`** and marked `model_info`; a call past a size tier is priced at that tier (#491, #494). The same holds for the reviewer's, watcher's and import pass's calls, whose cost source is recorded (#477, migration 53). The earlier rule that an unpriced call alone exhausts the daily limit is superseded by the owner's 2026-10-18 decision above ([run service](../docs/specs/run-service.md), RS-6a).
- **No reviewer configured means reviews are off,** and the run says so once ([consistency review](../docs/specs/consistency-review.md), CR-35 as amended; #475).
- **The meaning cutoff is set by model family** (`text-embedding-3-*`: 0.34; #474; [skills](../docs/SKILLS.md)). Per-pack thresholds (SK-34) are still not built.
- **A fold proposal the user accepts keeps its own basis,** shown as "accepted by the user"; the basis `user` is only for wording the user wrote or edited (#479, review).
- **After a `research_state` stop, a new user message starts a consolidation run** (CM-45; #482, review): the short-form decisions and an instruction to propose a consolidation for the user to approve. The old queue stays held.
- **A ledger `repin` raises no flag** (FG-43; #490, review). It is a statement change on the user's entries, so agents are refused it; it needs an anchor on decided entries; it has no gate.
- **Lab round 1's shape:** after the seed (about 400,000 tokens), option 1: four `gpt-6-sol` arms and a second-model arm at 250,000, with a hard stop of USD 380 reported so that the listed rates stay within the owner's USD 500 (lab 44).
- **Questions 12 to 14 of the skills spec** are still the coordinator's acceptance (above, 2026-10-15). The owner has not confirmed them.

Conventions, with the `live` marker (#455) and the GPT adapter's ported code (#452):

- **`live` tests.** A test marked `@pytest.mark.live` calls the deployment's real model gateway, for the labs. The root `conftest.py` skips it unless `SS_LIVE_GATEWAY` is set, so CI never runs one. Added to [CONVENTIONS](CONVENTIONS.md#testing).
- **Ported Apache-2.0 files.** A file ported from an Apache-2.0 source (`orchestrator/pairing.py`, from OpenAI Codex) carries `SPDX-License-Identifier: Apache-2.0` and its source's notices, and its package declares `MIT AND Apache-2.0` ([licensing](../docs/LICENSING.md)). [CONVENTIONS](CONVENTIONS.md#licensing) says so.

Decisions of the coordinator on the context-management build, which the owner can reverse:

- **Provenance of an import** (#457; [context management](../docs/specs/context-management.md), CM-9). A ledger import of a results file is `observed` only when the file is an artifact revision that an execution wrote, with its lineage. Otherwise it is `interpreted`. A command-line task counts as the user's words, since the operator wrote it (#464).
- **The user's move back restores the user's confirmation** ([ledger](../docs/LEDGER.md); #453). When the user moves a disputed published result back to `recorded`, keeping its material, what confirmed it before confirms it again. The main agent's move back needs a new confirmation. A caveat added later unconfirms nothing, and a reversed decision settles nothing.

Decisions of the coordinator on lab 42's fixes, which the owner can reverse:

- **A review holds plan changes, memory proposals and cancels as actions** ([consistency review spec](../docs/specs/consistency-review.md), CR-2; #449, B2). The review's snapshot holds the plan as recorded at dispatch, as context. A terminal review also lists the run's earlier windows' actions, as context (B5).
- **A delegated run's output is the record; its summary is a claim** (CR-2, CR-4; #449, B3). A parent's review holds the delegated run's executions with their output, chosen by the numbers of its outcome. A value missing from output that was cut short, or from executions left out, is unverified, not contradicted.
- **A run's answer to the user is the turn that answered** ([run service](../docs/specs/run-service.md), RS-10; #448, B4). Text held back by the plan's veto, and text written in closing rounds after the answer, are not the answer. The context-management view (CM-29) replaces RS-10, and the record's `answer` still says which text answered.
- **A blank obstacle or refutation is absent** ([plans and delegation](../docs/specs/plans-and-delegation.md), PD-17, PD-18; #445, B6).

## 2026-10-15

Decisions of the owner on context management (#439):

- **The context-management spec is approved,** with questions 1 to 11 decided as recommended ([spec](../docs/specs/context-management.md); [specs README](../docs/specs/README.md), question 18). Folds start at 0.75 of the ceiling and stub old bulky tool output; the default ceiling is 600,000 tokens; a new run sees the recent raw work of earlier runs and folds of older work; no provider-native compaction in the first build; one control arm of skeleton-only folds; the geometry is option (b). **The pause on new work is lifted:** the build plan is in the [BACKLOG](BACKLOG.md#repository), and lab round 1, the continuity probes on both model families, is the acceptance condition.
- **D61 is amended** ([decision register](../docs/DECISIONS.md)). GPT models go through LiteLLM's `/v1/responses` with reasoning items kept and replayed, and Claude models through `/v1/messages`. Replaying Claude's thinking is a deployment setting, off until a gateway image with LiteLLM's thinking fixes passes a conformance fixture. Other aliases keep chat completions. Each adapter is chosen per alias and none is used by default until the labs confirm.
- **The ledger rule: weakening a settled state needs no authority; strengthening needs the user or a passing gate** ([ledger spec](../docs/specs/ledger.md); #440). The main agent reopens an obstructed question by itself, with a reason, and the ledger records a work record that ends `reopened`. This reverses the coordinator's earlier choice that leaving `obstructed` needed a decision. In general, an agent's strengthening change counts only checks, reviews and acceptances recorded after the entry was last weakened, and a flag on a settled entry is cleared by the user. On a settled claim or result the main agent may clear it after a check whose brief named the flag.

Decisions of the coordinator on the ledger's research state, from #440's reviews, which the owner can reverse:

- **Caveats are outside the gate's material and the citation check** (CR-4; CM-10). They unsettle nothing. An agent adds caveats and removes none; the user removes one from a settled entry by a decision.
- **A user's obstruction without an assessment counts as settled** (CM-13). Its label is impossible, within its excluded scope when it names one. Every new obstruction names an excluded scope; an older one reads "scope not stated".
- **A user's acceptance settles only the obstruction it was given for.** Obstructing again after a reopening needs a fresh acceptance or the user's own decision.
- **The user edits a decision the user wrote directly,** without a decision. Agents never change one the user wrote, apart from adding its missing reopen condition.

Decision of the owner on pausing work:

- **New work waits for the context-management decision.** The owner is deciding how to manage the context of long runs. Their priority is strong recall and reasoning over long research projects; auditability and budget control come second ([STATUS](STATUS.md)). Until the owner decides, no new work starts, and work in flight finishes. Waiting: skills K6a (#437, a draft, not to be reviewed or merged), the skills lab and the rest of the [BACKLOG](BACKLOG.md). The approval above lifted the pause.
- **Direction for context management.** The owner uses both Claude and GPT models, so the approach must work for both. The owner prefers an existing library known to be good for long research sessions; failing that, the project follows closely how Claude Science and OpenScience manage context, rather than designing its own. A survey of libraries and a close study of those systems is under way, and the owner decides from its result. No live evaluation is planned.

Decisions of the owner on the skills spec's questions 15 and 16, with the spec's approval (#373):

- **Built-in skills are a pack in the repository** (SK-40, SK-41). Markdown under `skills/builtin/`, imported and pinned like any pack, each skill excludable, with a CI check that the tools a skill names exist. A pack that collides only with built-in skills is refused with a hint to exclude them, and wins once they are (SK-4).
- **The index is a pinned section of the system message** (SK-7, SK-36, SK-38, SK-42): 12,000 characters, with its framing and the notice's points set in the spec. Skills loaded at a run's start sit between code markers.

Decisions of the coordinator on lab 41's fixes, which the owner can reverse:

- **A review holds the work it is about** ([consistency review spec](../docs/specs/consistency-review.md), CR-2; #422, #425). A confirmation holds what the session's runs did since the reviewed findings' reviews and the files a claimed fix names by path. A run's review holds its delegated runs' work once each delegation ends, to three levels: their actions, outcomes and result files, as context. A run's transcript for review starts after the session's earlier conversation (#414), so an earlier run's true statement is not matched against this run's calls.
- **A plan with no open step holds no answer back** ([run service](../docs/specs/run-service.md); #420, B2). A closing answer stands when the plan is finished or has no open step. When open steps remain and the run has not read the plan, the answer is held with a notice that says why, and the run then stops as `plan_unread`. Effect tools still refuse until the run reads the plan, a finished one included; that is a separate decision.
- **A failed connector listing is tried again, and said** ([MCP connectors](../docs/MCP-CONNECTORS.md); #424, B4). A run's start lists a failed server twice more. A server that still fails is unavailable, not missing: the tool index says so, the first `mcp_tools` call lists it once more, and the run's record and `started` event name it.
- **A proof file read in ranges counts as read whole** ([plans and delegation](../docs/specs/plans-and-delegation.md), PD-17 to PD-19; #427). One read shows only part of a large file, so such a proof could never satisfy a check's `read_proofs`. A file counts once the check's reads cover it from its first byte to its size, in any order, at one SHA-256.
- **Skill embeddings are made in the background** ([skills spec](../docs/specs/skills.md), SK-29, SK-36; #433). No run waits for vectors: until they exist it compares by words, and its record says why. A message too short to score is neither embedded nor waited for. The meaning threshold of 0.45 is a default until per-pack thresholds (SK-34).

Decision of the coordinator on the PDF tools, from writing the `pdf-reading` skill (#423), which the owner can reverse:

- **One line numbering for the PDF tools** ([PDF sources](../docs/specs/pdf-sources.md), tools item 3, PDF-F7). `pdf_pages` and `pdf_search` number a page's lines one way, running headers, footers and page numbers counted and marked; `pdf_pages` leaves them out unless asked, without renumbering the rest, so a quote cited by either tool's numbers is found (CR-4).
- **A render's region from `pdf_pages`' boxes** ([PDF sources](../docs/specs/pdf-sources.md), tools item 2). `pdf_pages` with `boxes` shows each line's box as `pdf_render` takes a region, and the render's description says so; it had sent the agent to boxes `pdf_pages` did not show.

Decisions of the owner on skills, after the first real-use lab. The owner's priority is skill import and discovery tools with a relevance gate like Claude Science's. They are recorded in the [skills spec](../docs/specs/skills.md), which the owner approved with #373.

- **Show, don't force** (SK-35, SK-36). The relevance gate holds the model's call, not its work. Before a run's first model call, and before the call that carries each new user message, the call waits at most 5 s for a notice of the skills that match, with their names, one-line descriptions and the pack's entry skills. Nothing else is held: the model decides, no load is forced, and no reason for skipping a skill is recorded or shown. A delegated run does its own discovery on its own task.
- **Every call carries an index only** (SK-7, SK-38, SK-39). A compact index within a budget (drafted at 12,000 characters), compressed to names grouped by topic when it does not fit, with search for the rest. Bodies load on demand, or at every run's start from a per-project list that is empty by default, within 100,000 characters per run.
- **Import fetches and pins, with no hash required** (SK-1, SK-2, SK-28). `skills import --git <url-or-path> [--ref <ref>]`, or `--archive`, runs on the user's machine with the user's git. The ref defaults to the default branch and its resolved commit is the pin; the launcher keeps its pin until the user asks to update. Platform services and sandboxes never fetch a pack.
- **Packs carry no special files** (SK-26, SK-29 to SK-34). `sandbox-pack.toml` is dropped. At import the skills are embedded through the gateway (a lexical score alone when no model is configured), and one model pass derives entry skills, topics, short descriptions and environment bindings. Its output is checked data, never instructions, and the project's skill settings override it.
- Also decided: a second source that defines a name already pinned is refused (SK-4), invalid front matter excludes the skill (SK-6), kernel helpers are not built (SK-17), and the relevance threshold is tuned per pack.

Convention, with the built-in skills' import (#426):

- **`skills/builtin/`** holds the built-in skills, a top-level directory of Markdown the harness ships and pins at a run's start (SK-40); added to "Where things live".

Decisions of the coordinator on the skills spec's questions 12 to 14, drafted values the owner did not answer; the owner can reverse them:

- **Notice size and wait** (SK-35, SK-36): a discovery notice shows at most five skills, plus the packs' entry skills, and scoring waits at most 5 s.
- **What the import's model pass reads** (SK-30): the catalog's names and descriptions, plus the lines around each mention of an environment variable, not whole bodies.
- **What is embedded** (SK-29): each skill's name, description and the first 1,500 characters of its body.

Decision of the owner on lab models:

- **Labs keep the weaker model on purpose.** Labs use the gateway alias `sandbox-orchestrator`, which a weaker model serves, because its errors (wrong labels, edits claimed but not made, stray tool calls) show how the platform handles model mistakes. The owner uses a stronger model in normal operation. A lab's model-quality failure is a finding about the platform's checks, not a reason to change the alias.

Decision of the coordinator on lane K's triage item D16 (the triage's number, not the decision register's), which the owner can reverse:

- **OpenScience's two derived skills are never imported.** OpenScience's `conducting-scientific-research` and `scientific-problem-selection` derive from Claude Science text, so they are not imported, adapted or paraphrased. NOTICE files are checked before anything is borrowed from OpenScience or Open Science Desktop, since a permissive license does not settle D56. The rule is in [licensing](../docs/LICENSING.md).

Decisions of the coordinator on lane K's triage, items P1 and P2 (#389), which the owner can reverse:

- **A refusal is a decision, and a search miss is not absence** ([orchestrator prompts](../docs/ORCHESTRATOR-PROMPTS.md); `sandbox-main-4`). A request awaiting approval is retried once it is approved. Any other refusal with a reason is the user's policy deciding: it is not resubmitted unchanged and its content is not fetched another way (another host, mirror, source, tool or encoding); the agent says what was refused and why, uses what is allowed, or asks with `ask_user`. A search with no hits shows only that this query found nothing in that source as it reads now: the agent reports the source, query and date and never concludes that no result or attack is known. The tool descriptions of `admit_hosts`, `provision`, `mcp_tools` and `pdf_search` and the literature framing carry the same points. The wording is the project's own, as D56 requires.
- **A sample is not the whole** (#389, with #388). The system message says counts, identifiers, rows and all-or-none claims come from the saved file a sample names, never from the sample.

Decisions of the coordinator on lab 40's B2 and B4, which the owner can reverse:

- **An agent's rebuttal is not offered back to it** (#397; [consistency review](../docs/CONSISTENCY-REVIEW.md)). A finding an agent rebuts stays open at a new revision, and every open review finding at the interrupt severity was queued as a notice, so a run got its own rebuttal back and rebutted again until its turns ran out. An agent's response queues no notice. The reviewer's decision on it and the user's responses still do.
- **An `mcp_tools` query lists and loads nothing** (#401; [MCP connectors](../docs/MCP-CONNECTORS.md)). A query returns at most 8 tools that match well, each with its server, purpose and whether it is loaded; the model loads by name. A query of one or two words needs every word, a longer one two thirds, and one match must be in a tool's name or purpose. When nothing fits, the answer says so, and that the harness's own tools (`ledger_*`, `document_*`, `plan_*`) need no loading.

Decisions of the coordinator on delegation's second level and its models (#375, #394, #405, #410), which the owner can reverse:

- **Depth 2** ([plans and delegation](../docs/specs/plans-and-delegation.md), PD-15, PD-43). A main run's task brief may carry `delegate: true` only at depth 2. That child delegates tasks and never checks, and its own children delegate nothing. Handbacks go to the direct parent, so a grandchild's outcome never reaches the main run; a lead that ended without receiving a child's outcome passes it up as `unreceived`. The cap counts every running delegation of the session. A delegated run's tasks take no track and get at most the tokens and turns it has left. A cancel or a dead tree's end reaches every level.
- **The delegated model** (PD-27 to PD-30). A brief may choose among the aliases the project's setting lists (none by default, so no choice), and the project may name a fallback. The delegated role's own alias is the deployment's setting, as MAIN's is, and is called even when the gateway's list leaves it out (amends PD-F11 for lab 40's B1). An alias a brief chose that the gateway does not list is replaced by the fallback, or `delegate` is refused with `model_unavailable`; so is any alias the gateway says cannot call tools. An alias the gateway does not describe, or a gateway that cannot say, is taken at its word.

Decisions of the coordinator on the verification of the frontier's fixes, which the owner can reverse:

- **An agent's condition values are printable ASCII** ([ledger spec](../docs/specs/ledger.md), LG-18). A letter of another script that looks Latin, a Unicode dash or slash, or other digits do not make a group of their own. The user's values are free, and groups that differ only in look-alike characters stay apart with a `lookalike` warning.
- **A cycle of `dominates` links made later voids them at read** (LG-22). A change that makes a cycle is refused. A cycle a later change makes (a dispute, a revision of a category or result, an entry back from set aside) voids its links when the frontier is read, and the frontier lists them with why until the user drops one.

Lab 40's findings, built by lane W:

- **Every role's calls count in the project's cost** ([run service](../docs/specs/run-service.md), RS-6a; B5). The reviewer's and the job watcher's calls keep the cost the gateway reported (control store version 40), and the project's cost and its daily limit count them with MAIN's and delegated runs' calls.

Decision of the coordinator on lab 40's narrated edits, which the owner can reverse:

- **The reviewer matches narrated actions against what each run did** ([consistency review spec](../docs/specs/consistency-review.md), CR-2; rubric `sandbox-consistency-4`). A review's snapshot holds one derived `actions` item for its window: each run's tool calls that change something, with their outcome and what each made, and the blocks each document revision changed. A narrated action with no record is a `fail` of category `missing_action`; no new category.

Decision of the coordinator on lab 40's B3, which the owner can reverse:

- **A review sees what an entry rests on** ([consistency review spec](../docs/specs/consistency-review.md), CR-2, CR-14). A review that holds a ledger entry revision also holds, as context, the document revision a proof's block is in, a delegation's outcome and an entry its evidence pins, as it holds the artifacts (#338). Context makes no review due, and holding an entry as context is no review of it.

Decision of the coordinator on a delegated task's result files (RA-38), which the owner can reverse:

- **A result file is one the child's calls made** ([research artifacts](../docs/specs/research-artifacts.md), RA-38). The platform checks each declared file against the revisions the child's own calls recorded or published, pins the latest in the outcome, and refuses any other, with the one correction; it does not record a declared file itself. A project that records results only, or a file an ignore pattern leaves out, gets its result through `publish`.

Decisions of the coordinator on delegated tasks' tracks and issues (6b-2), which the owner can reverse:

- **A task's outcome on its issue** ([ledger spec](../docs/specs/ledger.md), LG-6, LG-F8). It ends `produced` when the child created entries, `limit` when it was partial, blocked or stopped (what ran out), and `limit` with `ideas` when it completed and created nothing: `assessment` stays for critiques of other work, and a finished investigation that found nothing leaves the issue open (LG-7).
- **A claimed obstruction's evidence** is the delegation itself, cited with the child's evidence as the note.
- **A revised plan refuses a task's progress** ([plans and delegation](../docs/specs/plans-and-delegation.md), PD-9): its grant is the plan version it started under, and its outcome says the plan changed, so the parent delegates the track again.

Decisions of the coordinator on questions to the user (PD-23 to PD-26), which the owner can reverse:

- **A chat message answers the open question** ([questions to the user](../docs/QUESTIONS.md); [run service](../docs/specs/run-service.md), RS-4). While the run that holds the session waits for an answer, a message the user writes answers that run's oldest open question in the user's own words, instead of being queued; the chat box says which question it will answer. Queueing it for the run's next turn instead is a small change.
- **No defaults or timeouts** ([plans and delegation](../docs/specs/plans-and-delegation.md), open question 6). A question waits for the user, and its run with it, holding the session and making no model call.

Decisions of the coordinator on lane K's triage of the prompt research, which the owner can reverse:

- **Comments are the user's words in the earlier conversation** ([run service](../docs/specs/run-service.md), RS-10). The comments addressed to the session (work requests) that a run delivered join its chat messages in the history, in the order they were written, each after a line naming its document, and give way last as the messages do. A run started for comments alone shows them in place of its stand-in task.
- **The user's messages give way last** ([run service](../docs/specs/run-service.md), RS-10 and decided question 4, amended). When a session's earlier conversation is over its 100,000 characters, each run's task and user messages are kept as written, and the agent's answers are left out first, the newest that fit kept whole and the others replaced by a line that says so. Only when the user's messages alone do not fit are the oldest runs left out whole, with a line saying how many.
- **Long output is saved whole** ([tools spec](../docs/specs/tools.md), Shell 5, amended). When a shell call's or a cell's output is too long for its result, the harness saves it to `call-output/<execution ID>/` in the session workspace, recorded like any file the call wrote. The result shows the head and tail, calls them a sample to take no counts, identifiers, rows or all-or-none claims from, and names the file. Past the worker's 1 MiB limit, the file is a sample too, and the result says so.
- **A script stops at its first failure when asked to** ([tools spec](../docs/specs/tools.md), Shell 1). The descriptions of `shell` and `start_job` ask a script of several steps to begin with `set -euo pipefail`, or to check each step, and to check that its files exist before saying it made them. The harness does not prepend the options: they would change single commands, such as a pipe into `head` (status 141) or a `grep` that finds nothing.
- **A check changes nothing** ([plans and delegation](../docs/specs/plans-and-delegation.md), PD-34; [ledger spec](../docs/specs/ledger.md), LG-13). A delegated run that checks a ledger entry is offered the ledger's, the documents' and the memory's reads only, with its compute and file tools: no `ledger_change`, `ledger_import` or `ledger_category`, no document edits or comments, no `memory_propose`. Its prompt told it so; now the platform holds it. General delegations will keep LG-13's authority.
- **A check names its coverage, and a refutation its evidence** (PD-18). The result (`sandbox-check-result-2`) gains `coverage`, what was examined and what was not, and `refutation`, the step, value or counterexample that contradicts the entry. A check that left anything unexamined cannot confirm, and only a refuted one has a refutation.

Decision of the owner:

- **Old pack projects are not imported** ([ledger spec](../docs/specs/ledger.md), LG-37, LG-38, LG-F10). The deliberate import of a pack project's knowledge files and session ledgers is not planned, and the coordinator's decisions on its plan (work records first, evidence pinned to the snapshot, recognised identifiers only) lapse with it. LG-35, the pack's bindings writing results files when its skills run here, stays, with the skills. Work records (LG-6) go ahead on their own: delegation outcomes on issues (LG-F8) need them.

Decision of the owner on the model budget: "I would expect budget to mostly be enforced upstream by LiteLLM", and "doing it as cost instead of tokens seems better, too."

- **The gateway's budget** ([run service](../docs/specs/run-service.md), RS-6d). A call LiteLLM refuses because the key's, user's or team's budget is spent is not tried again, even as a 429: the run stops, `gateway_budget`, and the session's messages wait. Cost recording and an optional cost limit per project, in place of the default token budget, follow in their own changes.
- **No project token budget; an optional daily cost limit** ([run service](../docs/specs/run-service.md), RS-6a and RS-6c). The 2,000,000-token daily MAIN budget and its table are gone: a project has no limit of its own unless one is set, as a daily cost in the gateway's currency (`project budget --daily-cost-limit`, the launcher's `[model] daily_cost_limit`), kept when a run starts and at its safe points against what the gateway reported its calls cost. A run's part of the limit (`serve --run-share`) is optional, none by default. `delegate` refuses only while a set limit is reached.
- **No reservations for delegated runs** ([plans and delegation](../docs/specs/plans-and-delegation.md), PD-38). The run tree's token reservations from the root's share of the budget are retired: a `delegate` reserves nothing, and each child keeps its project's check tokens and turns as a bound on a runaway run.

Decisions of the coordinator on the review of delegation's checks in the gate (#355), which the owner can reverse:

- **The platform writes a check's brief** ([ledger spec](../docs/specs/ledger.md), LG-17; [plans and delegation](../docs/specs/plans-and-delegation.md), PD-33). The task comes from a fixed template, the inputs are the entry's own evidence at the revisions it pins, and the parent adds at most a 500-character note, which the checker reads as the requester's note, not evidence; the link records the brief's SHA-256 and the note. The parent's free task, context and inputs could carry the verdict. General delegations keep free briefs.
- **A check that disagreed blocks the others** (LG-17). While a delegation of the same material (statement, fields, assumptions and supporting evidence) returned `refuted`, or `inconclusive` from a complete check, linked or not, no delegation's check counts; the user's own check still does. A check that is still running blocks too, and a check is linked only while no other check of the entry runs. The statement is compared by its text, so restoring a refuted text keeps the refutation. A check also counts only once its run read the entry's whole statement, the evidence holding its proofs, and each proof's target whole at its pinned revision, and only the user or a main run of the delegation's session links it.

Decision of the coordinator on the review of CR-4 (#346, #348 to #350), which the owner can reverse:

- **A PDF quote confirms only a vouched PDF** ([ledger spec](../docs/specs/ledger.md), LG-15, LG-16). A reference's PDF can be any artifact of the project, one an agent made included, and the platform cannot yet tell a downloaded paper from it. A PDF quote confirms a result by the citation check alone only when the PDF's hash is vouched for, by the e-print server naming it in a recorded result (asked of the owner) or by the user's check of a revision of the reference that names it (`ledger check`, or the workspace's, which now take a reference; no other revision of the user's vouches); otherwise the result stays unverified until the user confirms it. An ePrint passage quote confirms by the check alone.

Decision of the coordinator on CR-4, which the owner can reverse:

- **The citation check** ([consistency review spec](../docs/specs/consistency-review.md), CR-4; [ledger spec](../docs/specs/ledger.md), LG-8). Option 1: an agent's published result quotes the page, lines and text it read (a PDF) or the passage and date stamp (an ePrint); the platform checks that the quote is in its own recorded tool output for that PDF's hash and page, or that paper, and pins it in the review; the reviewer judges whether it supports the claim (`review_citation`, `citation_checks`). One shared evidence migration (version 30, after lane D's 28 and 29) adds the `quote` and `delegation` targets for lanes S and D. An ePrint is named by its identifier and the server's date stamp until the server gives a version and a SHA-256. The PDF tools print the full SHA-256.

Decisions of the coordinator on lane D's delegation plan, which the owner can reverse:

- **Delegation is off by default, per project** (`project delegation --depth 1` turns it on). The first brief is a check of one ledger entry at one revision, the owner's first milestone after M2: its fresh-context report is to count as the `independent_check` (LG-17).
- **A check by the same model, in a fresh context, counts as another agent** for LG-17, with the alias and model recorded in the outcome and the evidence.
- **Children's limits.** A check gets 150,000 tokens and 12 turns, a general child 200,000 and 20, none more than 400,000, reserved from the parent's share of the budget. The project's daily budget stays a setting.
- **The GUI shows each delegation's settings read-only** (plans-and-delegation question 7). Question 6 is decided when questions to the user are built; skills stay out of the brief until the skills spec is settled.

Decision of the coordinator on lab 39's B6, which the owner can reverse:

- **Copied numbers** ([consistency review spec](../docs/specs/consistency-review.md), CR-39). Before the model reviews a snapshot, the platform compares each number of 10 or more digits its ledger entries and document blocks state with the numbers its artifacts hold; one that nearly matches but does not becomes a `fail` finding of the review, with both values. It needs no model.
- **A review holds what its ledger entries pin** ([consistency review spec](../docs/specs/consistency-review.md), CR-2, CR-14). A snapshot that holds a ledger entry also holds the artifact revisions its evidence pins, whichever session of the project wrote them and however long ago, so the reviewer compares the numbers an entry states with the computation it cites. Ledger pins are project-wide by design. Text and JSON up to 64 KiB come whole; others are left to the entry's evidence, which names them, so that they do not make coverage incomplete. Rubric `sandbox-consistency-2` asks the reviewer to compare them digit by digit.

Decision of the coordinator on the review of the frontier (#304, #310, #321, #323), which the owner can reverse:

- **An agent's published result is unverified until confirmed** ([ledger spec](../docs/specs/ledger.md), LG-16, LG-20). A published result an agent records shows on the frontier as unverified and counts only as a candidate, until the consistency reviewer's citation check (CR-4) confirms that the citation resolves and supports the statement, or the user confirms it; one the user records is eligible at once. Each frontier result shows its author and role. Until CR-4 confirms citations, the user confirms one as the frontier shows it: the user's latest revision of it holds the material shown, and the user wrote that material, by recording the entry or by a revision that brought it in, or checked it (`ledger check`, or the workspace's); a user's other edits, such as a title or a dropped link, confirm nothing (decided after the verification of the review fixes). A disputed result is shown as the revision before it was disputed, which must have been eligible.

Decision on lab 39 (#331), which the owner can reverse:

- **The model budget during a run** ([run service](../docs/specs/run-service.md), RS-6c). A run checks the project's MAIN budget before each model call after the first, as well as at its start, and stops there once the project has spent it (`budget`) or once its own calls have used its share of it (`run_budget`; half by default, `serve --run-share`). An answer it gave stands, and what no model call saw waits for the next run. Lab 39's comment run spent 370,000 tokens past its cap because the budget was checked only at the start.

Decision on lab 38 (#305), which the owner can reverse:

- **A comment starts a run** ([run service](../docs/specs/run-service.md), RS-6b; LD-22). A work request to an idle session starts a run as a chat message does, within the model budget, and it ends a job wait. Requests get no `held` or `refused` state, which would need a migration: where a message would be held, the run service keeps the request from starting a run until the session's next run or the service's restart, and over the budget it looks again every 15 minutes.

Decisions of the owner for the M2 exit rerun:

- **The MCP provider from the owner's file** ([MCP connectors](../docs/MCP-CONNECTORS.md#crypto-research-mcp-a-cloudflare-access-service-token)). A helper (`sandbox_science.launcher.access_provider`) registers `crypto-research-mcp` from the owner's `cloudflare.env`, the approved alternative to typing the token in. It refuses a file others may use, and passes the values only to the one OpenShell command that registers them, through its environment. "For now", it is babbage's default: `[mcp_proxy] provider_env_file` makes `up` run it whenever the gateway lacks the provider, as each lab gateway does, or on `up --refresh-provider`. The launcher never opens the file, and agents never do. After the review of #322: no tracked config names the real file, only a lab's own config outside the repository, since a config also names the OpenShell CLI that gets the values; the helper refuses a CLI that is not an absolute path to a file the user or root owns, that others may write or that sits in a git checkout, and refuses to run with the tests' `FAKE_*` variables or `LD_*` set. The token also lives in the lab gateway's encrypted store, whose database and key labs never copy.

Decisions of the coordinator on the review of the ledger's gate (#276–#281), which the owner can reverse:

- **What a check or review covered** ([ledger spec](../docs/specs/ledger.md), LG-17). A check or review counts for a later revision only if it keeps the statement, fields, `assumes` links and every evidence link that bears on the entry: `supports`, `derivation` and `contradicts` of a type that can count. LG-17 named the `derivation` targets alone, which let evidence of either sign added after a review pass as reviewed. Reviews and `refuted`, `withdrawn` or `superseded` entries are not evidence that counts, and any `proof` that counts needs the independent check.
- **The user's check** (LG-17). The user's independent check counts only as the `independent_check` link the user added themselves, at the revision they checked; anything the user wrote no longer counts because an agent cites it. The workspace (a same-origin POST that carries the revision read) and the harness's `ledger check` command record it, so proved results can wait for the owner's check until delegation exists after M2. The check is an explicit action: the GUI's "I checked this revision…" shows the exact title, statement, fields, evidence and `assumes` links first, and nothing else the user writes counts as one, even when an agent cites it.

Decision of the coordinator on the review of the project memory (#283–#291):

- **Hidden characters in text agents read**. Text the user approves and agents then read holds no character the user may not see: the server refuses, rather than strips, controls but the tab and the line feed, format characters, surrogates, private-use and unassigned code points, line and paragraph separators and every Default_Ignorable code point, written as they are or as character references (`check_agent_text`), and the GUI shows such text as its exact string, each hidden character as its code point (`ApprovalText`). The zero-width joiner and non-joiner and the left-to-right and right-to-left marks are refused too, for now: research text rarely needs them, and an agent that meets the refusal writes without them. **Reversible:** an allowlist can take them back. The project memory used it first; the other surfaces followed (below). **Narrowed (the coordinator, 2026-10-15), so legitimate text passes:** U+FE0F is text right after an emoji or in a keycap, so a warning sign or a check mark in its emoji form works, while the joiners, the marks and the other variation selectors stay refused; an agent's line ends (CRLF, a lone CR) become line feeds before the check, everywhere, as a document's always did; and blank lines count only between lines of text, so trailing ones are no longer refused. A form feed and ANSI escapes in agent prose stay refused; a code block that needs them shows them escaped.

M2's exit checkpoint was met in [lab 39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md) (#331; labs 37 and 38 are #289 and #305), as a testing checkpoint (plan decision 1). Open from it: B5, the connector's 72 tools (about 17,000 tokens) offered in every model call, and B6, numbers the model copies not checked against the artifacts. Delegation and MCP connectors for real research come next (plan items 7 and 9).

Decisions of the owner since the last sync (2026-10-14 and 2026-10-15):

- **No human has to validate claims.** The main agent establishes results through the gate ([ledger spec](../docs/specs/ledger.md), LG-17); the user may still decide or override.
- **The independent check waits for delegation.** For a result derived by argument, only a check the user produced counts until delegation exists, so the user checks proved results. The owner chose to build a narrow "check this claim" delegation right after M2, not before: a run with a fresh context whose report is the `independent_check` ([BACKLOG](BACKLOG.md)).
- **`down` suspends the research sandboxes** (#282), since otherwise nothing shows they still run; `--keep-sandboxes` leaves them running.
- **After M2** (#273, #274): questions to the user (PD-23 to PD-26), editable figures, Vega-Lite charts and Mermaid diagrams as text with checks and a skill, and a `view_image` tool. None is needed for M2.
- **Direct edits of documents stay:** the user's edits in the GUI (LD-13) are built; direct edits of document files (LD-50) are not.

Decisions of the coordinator, which the owner can reverse:

- **New worker routes do not raise the protocol** ([compatibility](../docs/COMPATIBILITY.md), #282). `PROTOCOL_VERSION` rises with a change to an existing request or response; a new route with new contracts, which no existing client sends, keeps it. The environment list and suspend routes were the first.
- **Hidden text, surface by surface** (A3, #300 to #325). An agent's write is refused, and the user's own text kept as written, in documents (a created document, its title, and a patch's new text, captured code's included), comments, reviews (statements, remedies, explanations, responses), session plans, ledger entries (`ledger_change` and `ledger_import` alike) and the project memory. Not checked: an agent's chat output, and a review's summary and checked-claim notes, since the user does not adopt them and a refusal would fail a run or a review. Where the user approves or adopts text, the GUI first shows the exact string with each hidden character as its code point: applying a patch, Move to project, Restore this revision, saving an agent's plan, a finding's statement and remedy, a memory proposal and a ledger check. Beside rendered text (comments, chat, the editor, a ledger statement) a banner names what is hidden and lists links; the rendered document gets one only when it hides something, and lists every link only under 4,000 characters.
- **`memory_propose` passes the plan fence** (#284). It has an effect class of its own, `proposal`, so a run can propose while its plan is a draft: a proposal changes nothing until the user accepts it, unlike `ledger_change`. The session's lease still gates it.
- **An agent's published result is unverified until confirmed** (the frontier review, #304 to #323; LG-16, LG-20). A published result an agent records is a candidate, not eligible for the frontier, until the consistency reviewer's citation check (CR-4) confirms the citation or the user confirms the result; one the user records is eligible at once. CR-4's citation check is not built, so for now the user's confirmation is their own revision or `ledger check`. Built in the frontier fixes; #310 records it in full.
- **A fail finding's resolution holds for the material it was decided on** (#281; LG-17). A confirmed fix or a withdrawal resolves a `fail` finding for that material only: returning an entry to failed material holds the gate again, though the user can still establish it. The gate reads the entry's history for this, at most 256 revisions.
- **Publishing makes a file a result** (#313, lab 38 B7; RA-9 and RA-37 amended). A publication or declared result that finds the bytes a call recorded still makes the artifact a result, pinning the existing revision. A call that writes the same bytes again makes nothing a result.
- **Review limits for a large model** (#314, #316, lab 38 B8). A review gets 24 calls, 300 s, 300,000 characters of context and 400,000 tokens, counted from the provider's usage, and a project 240 reviewer calls an hour. A turn's calls past 16 are answered `not_run` rather than failing the review, and a window a limit cut short is reviewed again.

Decision of the coordinator on the review of the frontier (#326), which the owner can reverse:

- **Set-aside entries come back only by a decision** ([ledger spec](../docs/specs/ledger.md), LG-4). An entry leaves `refuted`, `withdrawn` or `superseded` only in a change that puts in effect a decision that affects it, and the main agent's decision that brings a claim or result back passes the gate, whatever status it brings it back to: through `disputed` it could otherwise be `recorded` again with nothing checked. While a claim or result that was once established or recorded is `disputed`, its statement, fields, assumptions and evidence are kept, as an established one's are. Undoing a supersession takes a decision too.

## 2026-10-14, later (PRs #165–#258)

Decisions of the owner since the last sync (PRs #165–#258):

- **Reviews are by risk** (the owner, to save tokens). Security paths, storage migrations, concurrency and new features get a full adversarial review. Code moves, docs, test-only PRs and fixes that implement a reviewer's suggestion get a light check. [CONVENTIONS](CONVENTIONS.md#how-work-is-done) says so.
- **Packages after the M2 exit lab** (the owner agreed). `orchestrator/control` and the domain packages (documents, mathchecks, consistency, ledger) move out of the orchestrator into packages of their own. The orchestrator keeps thin agent tool adapters, possibly in `orchestrator/tools/`. The workspace depends on the domain packages directly, not on the orchestrator. Phase 5 of the plan.
- **A real model.** The gateway is `https://litellm-api.npages.org/v1` (only `/v1` is exposed; the root answers 404). The first live call through `OpenAICompatibleModel` worked with `arrcodex-gpt-6-luna` (25 tokens in, 8 out). The owner's alias `sandbox-orchestrator` works since the owner added it to the key's allowed models. It serves MAIN, the reviewer and the watcher until the owner sets an alias per role. The harness key is a file in the owner's secrets directory (`litellm-harness.key`); agents never read it. Nothing waits on the model any more.
- **crypto-research-mcp.** It is at `https://research-mcp-api.npages.org/mcp`, and the Access service token alone is enough: an `initialize` with the two Access headers got a normal answer (server `cloudflare-mcp-portal`, protocol 2025-11-25), and without a valid client ID the server answers 401 with an OAuth challenge. With a Service Auth policy per server, the token sees 72 tools from seven servers (attack-catalog, e-print, firecrawl, nist-csrc, paper-search, patent-search, web-fetch) and three portal tools. So the connector auth service stays off M2's path. The token is in a file in the owner's secrets directory (`cloudflare.env`) that agents never read. [MCP connectors](../docs/MCP-CONNECTORS.md#what-the-server-offers) and D66 record it.
- **MCP cost control is the services' job.** The servers are self-hosted and effectively free. A tool is escalated for its effects, never for its cost. The project policy can allow the research servers' tools and keeps denying the `portal_*` tools that configure the portal. [Threat model](../docs/THREAT-MODEL.md) A4, [MCP connectors](../docs/MCP-CONNECTORS.md), D67, [policy](../docs/POLICY.md) and `examples/policy.yaml` follow: firecrawl's agent, crawl, extract, parse and scrape are allowed, and the tools that act on the world (feedback, corpus refreshes, live browser sessions, monitors) still ask. A project policy that allows every tool escalates none, and then `--admit-escalated` is not needed. Project-side MCP cost controls are a low-priority backlog item.
- **No route from sandbox code to the platform.** Only agents act, through tool calls, as in Claude Code. LD-52 (the `ss-doc` helper) and SK-20 (helper calls to the platform API) are withdrawn, skills question 5 is moot, and the ledger question 6 settlement of 2026-10-11 is reversed: code hands results to an agent in a results file, and the agent decides to import it (LG-13, LG-41).
- **The ledger** ([spec](../docs/specs/ledger.md), approved in #250). It has seven entry types, with `reference` added to the six drafted ones. Experiments and conjectures are issues, and a proved conjecture becomes a claim that answers its issue. The ledger holds the big picture and a session plan holds the work inside a session (LG-40). Pack JSONL comes in once, by a deliberate import; the platform API is the only other way in.
- **Lessons go to a project memory,** not to a ledger `lesson` type. The [spec](../docs/specs/project-memory.md) (#251, approved, not built) makes it a memory tool with a small store of its own: agents propose, the user decides, and every agent role receives the accepted entries.
- **Living documents** ([spec](../docs/specs/living-documents.md)). Agent patches apply unless the document's switch takes them as suggestions (LD-20). The user picks the session that gets a comment's work request (LD-22). The user resolves the threads the user opened, and an agent replies in a thread when it acts on the thread's comment (LD-24). Comments go into exports only on request, as a separate file (LD-38).
- **Files that executions and delegated runs write** are recorded as artifacts by default, with ignore patterns, bounds, tombstones for deleted files and a "results only" project setting; a delegated run names its result files (RA-27 to RA-39). Approved in the [research artifacts spec](../docs/specs/research-artifacts.md#files-from-executions-and-delegated-runs) (#251, not built). The worker reports deleted files since #258 (protocol 5); the harness's recorder is next.

Decisions made while building, by the coordinator under the owner's mandate (PRs #165–#258):

- **Ledger** ([as built](../docs/LEDGER.md), #252–#255). Entries, revisions and mutations use the shared pieces: receipts with scope `ledger`, the event log with subject `ledger_entry`, and text revisions of kind `statement`. A mutation changes up to 32 entries, all or none, and a stale expected revision is a conflict, never rebased. The main agent creates and revises entries, a delegated run only creates them in their first status, and other roles and code in a sandbox are refused. Entry text and results files are untrusted data. Decisions and the gate, evidence, the frontier and flags are next.
- **Run service** (#194, #206–#209). The workspace and the service share the control store: the workspace writes message rows and the service polls them. This departs from the plan's wording that the workspace proxies the service. A project's MAIN budget is 2,000,000 tokens over a rolling 24 hours. A new run carries up to 100,000 characters of earlier tasks, messages and answers. A cancel saves a record; a run that dies ends `interrupted`, and its messages are held until the user resumes.
- **MCP client** (#227, #231, #232). Connector tools are listed at a run's start through the proxy and are `effect` tools, so the plan fence applies. The harness's proxy token is a file only it reads. An escalated call comes back as `approval_required` and is answered with the proxy's `pending` and `approve`. `compile-policy --admit-escalated` is opt-in.
- **PDF viewer** ([spec](../docs/specs/pdf-viewer.md), #228, #234, #235). A second origin for the viewer, capability URLs for PDFs, a sandboxed frame, PDF.js with its tier 1 parts only and no WebAssembly.
- **Delegation** ([spec](../docs/specs/plans-and-delegation.md), #225). It is built after the M2 exit lab, as roles of the one loop. The run tree's parent link (#233, PD-32) and kernels per run (#237, PD-37, worker protocol 4) are built; nothing starts delegated runs yet.
- **Kernels use abstract Unix sockets** instead of TCP ports (#179), which removes the kernel-start race under load. Sessions that share an environment are not isolated from each other's kernels; D41 and the threat model say so.
- **Control store.** Tables check their values when written, and reads take rows as they are (B-10, decision 3, #177–#183): migrations rebuild a table with `CHECK` constraints, and a store a migration refuses is left unchanged with a message that names the table. Checks and saves record the macro revision they used and derive the context when read (B-12, decision 7). Receipts, the event log, leased notices, feed cursors, text revisions and the math-check queue are one piece each (B-04, #240–#248).
- **Worker.** One worker per state directory, enforced by a lock (decision 9, #224). The worker store has a version and an `application_id` (#217). The daemon pushes events to its bridge, and replay stays for reconnects (#246). `manager.py` is a 543-line facade (C-03).
- **CI** (decision 11, #182, #256). Pull requests run static checks and the fast and slow tiers on Python 3.13. Main and the nightly run add the serial full suite on 3.12 and 3.13. Per-test limits and a deadline make a stalled run print what was stuck.
- **Approvals** (#188, #192, #223). A typed `PermissionRequest`, one `resolve` state machine for the middleware and the MCP proxy, and one request for concurrent identical escalations.

## 2026-10-14, earlier (PRs #141–#162)

The owner asked for autonomous work through the [refactoring plan](refactor-plan/PLAN.md), with its recommendations adopted. Each item marked *adopted* was adopted on 2026-10-14 under that instruction.

Decisions made while carrying out Phase 1 and Phase 2 (PRs #141–#162):

- **Save pattern (#162, E-05):** a browser save keeps its operation ID after every failure, a 401 or sign-out included, and forgets it only after a result, so a save whose outcome was lost is never applied twice.
- **Project lock files stay (#160, A-07)** until a plan edit checks its session in the plan's own transaction (B-17); then they go.
- **Control store re-baselined (#152, B-02; decision 6):** one migration list from a baseline at version 2, with `application_id` `0x53536373`. Stores from before it are refused, never changed, with the export path in [living documents](../docs/LIVING-DOCUMENTS.md). Project JSON files are not imported (#160).
- **Worker protocol (#148, #161; decision 8):** protocol 2 with D-01's check, then 3. Any change to what crosses the worker API raises `PROTOCOL_VERSION` ([compatibility](../docs/COMPATIBILITY.md)).
- **Kernel check (#156, A-04; decision 4):** the check keeps nothing between runs, so each run is told again about every kernel that still predates the packages.
- **Worker usability (#153, C-04):** one record per environment with each fact as its own field, not the notes' single state enum, since recovery, fencing and releases overlap.
- **No deep re-validation in the browser (#154, #157, E-04; decision 3):** the server's answers are checked only for scope; their types come from the server's models through `scripts/generate_api_types.py`.
- **No user-message hook yet (#150, A-02):** nothing writes messages during a run until the run service (A-06), so the hook waits for it (rule 1).
- **Run records (#143, #149, A-02):** `RunRecord.delivered` (notice IDs by source) and `RunRecord.summaries` (by hook) replace the per-source fields; older records load through a validator.
- **Test support (#141, C-13):** `WorkerApi`, the worker API as tests use it, lives in `packages/testing` ([CONVENTIONS](CONVENTIONS.md#repository-layout)).

- **Convention: an injected clock and one test-support package** (#137, plan Phase 1). Components with timing take a `Clock` (`contracts/clock.py`) and their timeouts as constructor parameters, defaulting to `SystemClock` and today's values; tests use `FakeClock` and `eventually()` from the dev-only member `packages/testing` instead of sleeps and patched constants.
- **Confirmed (adopted):** D84 (provisioning operations), D85 (the SQLite control store), D59 (pdf_oxide) and D43 (isolation tiers and the probe rule); PSF-2.0 and MIT-0 in tier 1 (D45). Plan decision 12.
- **Specs approved as drafted (adopted):** [tools](../docs/specs/tools.md) and [PDF sources](../docs/specs/pdf-sources.md), with their proposed answers. This settles D83's details: jobs run 24 hours by default and 7 days at most, 4 at once in a session and 8 in an environment, and closing a session cancels its jobs.
- **Kernel notices (adopted, decision 4):** the durable kernel-notice inbox is replaced by a stateless check before each turn, plus the disclosure every execution result already carries (A-04).
- **GUI chat (adopted, decision 5):** a small orchestrator run service, pulled forward from W5, which the workspace proxies; one active run per session, with messages queued to its next safe point; a new message carries earlier tasks and final answers, not transcripts, and may start a run within the project's model budget.
- **Development data (adopted, decision 6; D85 amended):** documents are exported, then pre-M2 `control.sqlite3` files may be discarded and the schema re-baselined, dropping legacy migrations and reader branches.
- **Math checks (adopted, decision 7; D82 amended):** only the current revision is checked, when the checker reaches it; one Node process per batch of formulas; a check records the macro revision it used.
- **Compatibility (adopted, decision 8):** worker and orchestrator release in lockstep until M3, with a protocol-version check when they connect and a written policy for when independent upgrades are needed.
- **Worker operations (adopted, decision 9):** one worker per state directory, enforced by a lock file; the stdio transport for development and tests only; an unreachable environment fences only itself; for M2 the whole-VM restart on daemon loss stays, and the user is told which kernels were lost.
- **Planning fence and plan approval (adopted, decision 10; D86 amended):** a policy revision does not block a running session's effect tools, since the worker enforces the current policy; the unused `awaiting_approval` state goes until plan approval's default is decided.
- **CI tiers (adopted, decision 11):** PRs run Python 3.13 fast and slow tiers; 3.12 and the serial full run move to main or nightly (Phase 1, after the tests are deterministic). New dev dependencies: pytest-xdist, pytest-timeout, pytest-repeat, prettier and prettier-plugin-svelte.
- **Short tracking files (adopted, decision 11; F-02):** STATUS (under 10 KB) and the handoff (under 8 KB) are replaced, not appended to; this file lists decisions only; the plan's working rules and the way work is done move into [CONVENTIONS](CONVENTIONS.md#working-rules); commits and PRs carry no attribution lines.
- **Other plan recommendations (adopted, decision 12):** approvals re-authenticate by re-entering the login token (D54 amended); the display-repair input descriptor is to be removed (D86); Anchor and Mutation go in `contracts`; work on passive inspectors stops until the M2 path exists.
- **The owner's answers to the plan:** M2's exit is a testing checkpoint, with delegation and MCP connectors right after it as the first milestone for real use; GUI approvals and document files in environments are deferred. The unwired delegation and document-sync code is kept on the tags `archive/delegation-2026-10` and `archive/document-sync-2026-10` and deleted from main; defenses beyond the threat model are retired.
- **Delegation's shape (the owner):** roles of the one tool loop; at least 8–10 subagents at once in a session; one extra level of nesting; no kernel sharing between parent and child. Recorded in the plans-and-delegation spec (#225).
- **Raw lab evidence out of git (the owner):** removed from all history and kept privately on babbage; reports name archived files by SHA-256, and `scripts/check_repo_hygiene.py` guards CI. This was the one exception to the rule against rewriting published history.

## 2026-10-13

- **Parallel Conda work (the user):** provisioning slices (pip license metadata, installer observations, named upgrades) run in parallel with delegated execution.
- **Overnight autonomy (the user):** continue autonomously; a heartbeat task, *Retry stalled orchestrator*, resumes stalled work after five minutes with the same model.
- **Delegation prerequisites built unwired:** records, run-kernel bindings, the child dispatcher, own-track progress, the child-attempt journal, the controller and its transport. The refactoring plan later archives them (decision 2).
- **CP-26 split:** the provisioning view ships as a read-only viewer, a two-lockfile comparison and a package and license inventory; operator permission answers stay separate.

## 2026-10-12

- **D86 (the user):** a planning phase with live plan progress, and original system and stock prompts written independently of Claude Science (D56). The user added: test scripts on a small scale before long computations.
- **More parallel agents (the user):** independent backlog items run in separate worktrees, with authors rotating onto each other's reviews.
- **D84's implementation amended:** an install that changed packages, or whose effect nobody saw, leaves sticky `needs_recovery`, and the CP-13 admission step refuses new ordinary work until recovery ([recovery fence](../docs/RECOVERY-FENCE.md)).
- **D85's store extended:** job control records, project macro revisions, export-input snapshots and operation receipts share `control.sqlite3`.
- **Document files need writer proof first:** a protected-filesystem feasibility step precedes runtime document replacement (LD-49). FUSE is a candidate, not a choice.
- **Artifacts:** explicit harness publication comes before automatic execution-output inventory and its consumers.
- **D82's checker:** the existing Node runtime runs a standalone KaTeX bundle in the control plane.
- **GUI distribution notices:** builds ship complete license notices for bundled code; OFL fonts and glyph-derived SVGs keep their terms, and the tier 1 allowlist is unchanged.
- **Workspace exception clarified:** the workspace reads harness records and makes authenticated same-origin USER changes to documents, annotations and findings through the orchestrator's stores, until W5.
- **Roadmap:** M1 passed; M0's review and M2 remain open.
- **The user resumed work** after a pause before a break (no new work had been started during it).

## 2026-10-11

- **The user's decisions:** D68–D78 confirmed; shared data across a project's environments by worker-synced copies (D79); the read-only root filesystem deferred (D48); the Conda provisioning, research-artifacts and consistency-review specs approved as drafted; D80 and D81 confirmed.
- **D81 (the user):** delegated agents are runs within their session, with kernels of their own; an environment of their own is a later opt-in.
- **D65 amended and D82 (the user):** living documents are also Markdown files under `documents/` in every environment, with direct edits merged; KaTeX-rejected math in an agent's text becomes a `math` finding for that agent.
- **Living documents and ledger drafts (the user):** their main questions approved as drafted; RA-1, RA-24, CR-10, CR-17, CR-25, CR-27, CR-28 and CR-29 amended, and CR-36 to CR-38 added.
- **D83 (the user):** long computations run as background jobs; agents usually wait on them while a low-cost watcher model checks in, a fourth role in PD-27.
- **D84 (proposed then):** provisioning operations settle the Conda spec's open questions 3, 5 and 6: one permission request per install, AGPL exceptions in the policy, a catalog only the platform extends. Orchestrators upgrade before workers.
- **D85 (proposed then):** living documents in one SQLite control store, `control.sqlite3`; projects and runs stay JSON for now. markdown-it-py and mdit-py-plugins are pinned to match the browser's parser.
- **D80 (the user):** the workspace GUI is a Svelte app served by aiohttp. Browser apps live in `web/<app>/`; the workspace may use the orchestrator's stores until W5; CI gains a web job and an npm license check.
- **Math repairs (the user):** the user's own LaTeX repair code from `cscience-crypto` is adapted under MIT; failures are kept for an hour (LD-46); 60 model repairs per browser session per hour. Open for the user: the escaped-bracket rule, macro definitions shown as written, front-matter macro limits.
- **Living documents' question 15:** an ambiguous `\(…\)` stays as written rather than becoming `$$…$$`; recommended, for the user.
- **D69 amended:** the worker, not the orchestrator, admits hosts on demand, since it holds the gateway's credentials; the compiler no longer emits a host-less endpoint.
- **D73 (proposed then):** the worker creates research sandboxes, keeping gateway credentials out of the orchestrator.
- **D74 (proposed then):** a hash-chained SQLite event store, used by the policy middleware.
- **D75 (proposed then):** the MCP proxy has its own JSON-RPC layer on aiohttp instead of the MCP SDK (D67); the policy gains `mcp.proxy.max_argument_bytes`.
- **D77 (proposed then):** the base profile image contract for `minimal` and `crypto-base`; tier 2 names conda-forge Python's runtime libraries.
- **D43 amended and D52:** the worker probes each executor's isolation tier, and evidence only lowers the declared tier; a backend conformance suite runs through the worker API.
- **W1:** proposed that shell calls keep no state and that a cell exception is its own error kind; the first slice uses its own tool loop, an aiohttp model client and jupyter_client.
- **M1's exit run passed** on babbage ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)).
- **Handoff and labs:** a handoff file for the next agent; lab scripts live in `.agents/labs/`.
- **Upstream reports (the user):** bugs found in upstream projects are described in `.agents/upstream/`; filing is the user's call.

## 2026-10-10

- **D57 amended (the user's choice):** the project policy names the hosts each provider credential may reach; the middleware refuses credentials over plain HTTP.
- **Threat model:** monitoring no longer relies on OpenShell's prover for policies with middleware or MCP endpoints (A3).
- **D71 (direction confirmed by the user):** a pull-through package cache instead of a mirror, for PyPI, conda, Cargo, Go and npm, then opam and Hackage.
- **D68 extended (the user's request):** `allow_credentialed` lets a read-only rule with `on_timeout: allow` also pass credentialed requests as alerts.
- **D69 amended (the user's choice):** the compiler names every public host the policy mentions, and admits others on demand later.
- **D69 (direction confirmed by the user):** OpenShell's proxy carries traffic and the platform's policy middleware decides each request; the separate egress proxy (D44) is dropped for HTTP and git.
- **D70 (proposed then):** the policy middleware service.
- **D68 (proposed then):** threshold alerts move to a `monitors` section; tool patterns resolve exact names first; `on_timeout` is `deny` policy-wide, with alerts only for read-only rules.
- **Policy file dialect:** policies are strict YAML: one-line ASCII values, bounded line widths, no backslash escapes, so a file reads as it is enforced.
- **Example policy:** crypto-research-mcp tools classified one by one; feedback, crawl and refresh tools ask first; portal configuration is denied.
- **Tier 1 licenses** add PSF-2.0 (typing-extensions) and MIT-0 (cffi); CI checks installed licenses and generated protobuf code.
- **Conventions adopted:** pyright strict for `packages/*/src`, standard mode for tests.
- **Plan snapshots tagged:** `plan-03-pre-review` and `plan-04-reviewed`.

- 2026-10-16: Delegated tasks and checks now share the fold engine with attempt-scoped archive reads and pinned briefs; initially skeleton-only to preserve token-budget accounting ([contract](../docs/specs/research-continuity.md#delegated-folding-contract)). Live role-continuity acceptance remains pending.

- 2026-10-16: Measured 24 offline requests through both production adapters for three context layouts. Moving changed state after history preserves a later-run byte prefix, but folds break it; no observed provider hits, so layout stays unchanged ([report](docs/agent-reports/cache-layout-2026-10-16.md)).

- 2026-10-17: The live parent-limit increment can retain the existing session/environment leases while children finish, before durable continuation identities land. Children receive their configured role turn allowance; finalization waits without model calls and Stop/interruption cleanup remains unchanged ([continuity](../docs/specs/research-continuity.md#parent-waiting-increment)).

- 2026-10-17: Add a narrowly scoped USER recovery token override for paused direct children, preserving their work identity, lifetime usage and project spending cap; parent-authorized resource requests and decisions remain the target ([adaptive resources](../docs/specs/adaptive-research-resources.md)).

- 2026-10-18: D89 proposes bounded automatic Resume for saved model-phase failures using the existing fenced tree path. Clean answers with an active plan still need a distinct durable new-root continuation ([tree Resume](../docs/specs/tree-resume.md)).
- 2026-10-18: D90 proposes one new-root input per active-plan state after a settled answer; the write rechecks current plan/root and yields to user messages, cancellations and budget gates ([tree Resume](../docs/specs/tree-resume.md)).
- 2026-10-18: Built-in scientific writing guidance now starts substantial living documents with an annotated outline, drafts by section, checks citations and claim scope, and keeps the current document self-standing; `show_outline` reads its heading hierarchy without loading the full text ([living documents](../docs/LIVING-DOCUMENTS.md)).
- 2026-10-18: Integrate the Skills browser over the existing authenticated project-skills API: catalog/source/detail views and reviewable future-run loading choices, retaining revision checks and conflict drafts ([workspace browsers](../docs/specs/workspace-browsers.md)).
- 2026-10-18: The first MCP service browser reads an authenticated project inventory of launcher-provided safe names and purposes and bounded historical run observations; it does not query the proxy, expose credentials/endpoints or assert current health ([workspace browsers](../docs/specs/workspace-browsers.md)).
- 2026-10-18: Begin the production context inspector on saved attempts with provider-reported per-call usage/cache, saved fold links and exact attempt-scoped SQL search of retained original text. Missing live/current-context measures stay explicitly unavailable rather than inferred from a run preview ([context inspector](../docs/specs/context-inspector.md)).
- 2026-10-18: Keep long-attempt inspector evidence available when inline run fields compact: page archived usage and fold summaries, bound each search scan with a progress cursor, preserve applied search filters across pages, and read large original messages in project-scoped byte chunks. Live working-context reconstruction and multi-attempt views remain separate work ([context inspector](../docs/specs/context-inspector.md)).
- 2026-10-18: Amend CM-37 so a delegated parent can retrieve the full archived history of its descendant agents through scoped history tools; independent checks remain own-run only. `history_read` pages oversized fields and saved-file names, and range reads project only visible text so a large native payload does not force extra agent calls ([context management](../docs/specs/context-management.md#layer-5-retrieval)).

- 2026-10-18: Continue child-scoped narrative folding with shared cumulative token accounting, per-attempt reporting-room admission and bounded retry diagnostics. Qualify process-loss recovery and sustained Sage computation in a fresh synthetic OpenShell lab without restarting preserved research; scientific acceptance remains separate.

- 2026-10-18: Live synthetic recovery exposed cleanup overwriting saved model failures as interrupted. Preserve paused/blocked diagnoses during process-loss cleanup; explicit Stop still supersedes unfinished work. Exact-scope automatic Resume now succeeds after a streamed EOF. Fold cancellation accounting settles before saved summaries, and confined calls remain under their canonical intent identity without duplicate estimates.
