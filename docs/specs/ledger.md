# Project ledger, board and frontier

Date: 2026-10-11. **Status: Draft, to review with the user.** The user settled open questions 2 to 6 on 2026-10-11: the main agent's decisions under the gate, frontier ranking, flags that spread to all dependents, the review rules for entries, and code's delegated authority. The rest of the draft still awaits review. W0 behavior specification; conventions in the [index](README.md).

## Scope

The project ledger holds the project's research state: open questions and issues, claims and findings, results, assumptions, decisions and tasks, each with its evidence, status and history. Every workbook reads and writes the same ledger, whichever container it uses (D24). The board, the frontier, the issue list and the history are views over it, not separate stores (D24, D33; [research workspace](../RESEARCH-WORKSPACE.md)). This spec covers entry types and statuses, evidence, attribution and authority, published and project results, frontier comparison, links to review findings, reconsideration after assumptions change, mutations, the external pack's board records (gate 5) and deliberate imports.

CScience keeps project memory with evidence categories (stated, observed, inferred) and supersession, session claims and verification records, and it separates session scratchpads from project facts; its dashboard is disabled, and no finished research board was found in it (CScience). Most of this spec is therefore new. Its research semantics come from the crypto-analysis-skills pack's board protocol and knowledge files ([external skills](../EXTERNAL-SKILLS.md)), whose content stays in the pack (D30).

Out of scope: the lifecycle of review findings ([consistency review](consistency-review.md)); a verification or reproduction role (D27); [living documents](living-documents.md); session plans and delegation ([plans and delegation](plans-and-delegation.md)); agent memory and the recall of project facts into context; the GUI's layout (D80).

Terms:

- **Entry**: one typed ledger record, a control-plane artifact (RA-8) whose revisions hold its fields and outgoing links. A scientific finding is recorded as a claim or a result; "finding" alone means a review finding (CR-17).
- **Origin**: `published` (stated by a cited source) or `project` (produced in this project).
- **Evidence link**: a pinned reference from an entry to what supports or contradicts it, with a role and an evidence type.
- **Workflow status**: where an entry stands in the project's work, separate from its evidence types and from the status of review findings.
- **Work record**: an append-only record of work on an entry: what was tried, and how it ended.
- **Flag**: a request, raised by the platform, to reconsider an entry after something it depends on changed.
- **Frontier category**: a kind of result to compare, such as the cost of an attack on a target, a bound, a parameter regime or a construction, with its measures and conditions.

## Behavior

### Entries

- LG-1. A project has one ledger (D24). Each entry has a stable ID, a short handle unique in the project with a prefix per type (`I-3`, `C-12`), a type, a title, a statement in Markdown with LaTeX (D25), saved in the dollar form and checked for math errors ([living documents](living-documents.md), LD-42, LD-53), a status, type-specific fields and outgoing links. Revisions are append-only and attributed (RA-1, RA-2, RA-16). Entries live only in the control store, under paths of their own (`ledger/…`). Unlike documents, they are not files in the environments: code reaches them through the platform API, by the document helper's route (LD-52), and no file write or sync changes them (new).
- LG-2. Entry types and their statuses (new names; research workspace; [implementation plan](../IMPLEMENTATION-PLAN.md) W4):

  | Type | Records | Statuses |
  | --- | --- | --- |
  | `issue` | An open question, an idea to pursue, a contradiction, a failed reproduction | `open`, `resolved`, `obstructed`, `split`, `withdrawn` |
  | `claim` | A statement: lemma, theorem, conjecture, observation, counterexample, negative result | Project: `proposed`, `established`. Published: `recorded`. Both: `disputed`, `refuted`, `superseded`, `withdrawn` |
  | `result` | A claim in a frontier category, with conditions and measures | As for `claim` |
  | `assumption` | A hypothesis, model or parameter choice that other entries rely on | `active`, `withdrawn` |
  | `decision` | A choice: establish a result, accept an obstruction, refute a claim, exclude a scope, choose a method or a target | `proposed`, `in_effect`, `rejected`, `reversed` |
  | `task` | Project work to do, such as an experiment to run | `todo`, `in_progress`, `blocked`, `done`, `dropped` |

  A frontier candidate is a `result` that is not yet eligible (LG-20); becoming eligible is a status change of the same entry, not a copy (new).
- LG-3. Entries are project-wide. Notes for one workbook belong in its workbook documents, not in the ledger (CScience separates session scratchpads from project facts, with project scope the default).
- LG-4. Statuses change only through explicit, attributed mutations with a reason. A change to `established`, `obstructed` or `refuted` is made only by a decision (LG-7, LG-17); on the board, moving a card into one of these statuses asks the user for the decision's reason and records it in effect. The platform computes indicators (open findings, flags, contradicting evidence, open linked issues, newer revisions of evidence) and shows them beside the status, but never changes a status itself (new; [consistency review of the plan](../CONSISTENCY-REVIEW.md): different lifecycles are not flattened into one status).
- LG-5. Entries link to others with typed links: `answers` (a claim or result to an issue), `assumes` (pinned to an assumption revision), `builds_on` (a claim it uses), `restates` (a published claim the entry rediscovers), `compares_to` (a result it was compared with, and the author's stated difference), `supersedes` (an entry it corrects or replaces, which becomes `superseded`), `dominates` (LG-22), `split_into`, `affects` (a decision's targets) and `concerns` (what an issue is about: entries, review findings or document blocks). Outgoing links are part of the entry's revision; incoming links are computed (new).
- LG-6. Work records are append-only records on an issue or a claim: what was tried and by whom, with references (delegation outcomes, executions, artifacts, critique reports) and an ending: `limit` (time, tools, context or ideas ran out), `obstruction_claimed` (with its evidence), `produced` (the entries it created) or `assessment` (a critique of other work). Work records never conflict with each other or with the entry's revisions (new; the pack appends such records, a pattern it measured as safe with concurrent writers).
- LG-7. Limits are not obstructions. A work record that ends `limit` leaves its issue `open` and eligible for new work. Only a decision moves an issue to `obstructed`: it names the excluded scope, its evidence, and an `assessment` that accepts the claimed obstruction, produced by the user or by a delegation started to assess that claim (its brief names it) and run by an agent other than the one that claimed it. Ending a session changes no status ([external skills](../EXTERNAL-SKILLS.md); PD-19; the pack closes a lane only on a critic's acceptance and keeps other dismissals eligible) (new).

### Evidence

- LG-8. An evidence link has a role (`supports`, `contradicts`, `derivation` for the original argument of a new result, or `independent_check` naming the revision it checked), an evidence type, a pinned target, a scope note (what it covers, such as "$p = 17$ only") and attribution. Targets are artifact revisions, executions and their attempts, document blocks and math spans, PDF anchors, entry revisions, delegation outcomes and reviews. Evidence MUST be pinned (RA-19).
- LG-9. Evidence types (research workspace; D29):
  - `proof`: a mathematical argument, such as a proof block in a document revision, or a proof that a named formal system checked, linked to that execution;
  - `exact_computation`: exact arithmetic that establishes the stated fact itself, such as checking all 16 nonzero elements of GF(17);
  - `finite_test`: finitely many cases of a statement that covers more, with the cases in the scope note;
  - `numerical_experiment`: floating-point, sampled or estimated evidence;
  - `source_claim`: what a cited source states, unchecked by the project;
  - `synthetic`: an illustration on synthetic or simulated data, which never supports a measurement or a proof (D29).

  Imported links whose type the source does not settle are `unclassified` (LG-37).
- LG-10. Evidence type and workflow status are separate fields that change separately. A successful process exit, a passed review (CR-8) or agreement between agents sets neither (research workspace; [feature plan](../FEATURE-PLAN.md)). The target of an evidence link is the platform's record; the link's role, type and scope are the author's assertion, which the reviewer checks against the target (CR-4).
- LG-11. An entry with no evidence links shows as asserted only (new; the pack does not count an assertion as a source).

### Attribution and authority

- LG-12. Every revision, work record, flag clearance and decision records who made it: the user, or an agent run (role, alias, resolved model) in a session, with its tool call. Where they apply, it also names the environment; the execution whose code made the call through the platform API (SK-20), at the level of attribution the platform can establish ([MCP connectors](../MCP-CONNECTORS.md#mcp-proxy): container, sandbox or process); the skill helper revision; the review finding it responds to; the delegation whose outcome it records; and the import it came from (research workspace).
- LG-13. Authority by actor (new; CR-2, PD-16):
  - the user may make any change;
  - the main agent, through its tool calls, creates and revises entries, records work and evidence, changes statuses, clears flags and makes decisions, within LG-7 and LG-17. A project setting can reserve any of these to the user: establishing results, accepting obstructions, clearing flags and refuting published results; by default it reserves none (settled 2026-10-11, open question 2);
  - delegated agents create entries in their initial status (`open` issues; `proposed` claims, results and decisions; `recorded` published entries with their citation; `active` assumptions; `todo` tasks) and add work records and evidence links, but change no existing entry's status and clear no flag: a delegated outcome is a proposal until the main agent or the user decides (the pack's rule for child outcomes);
  - calls from worker code through the platform API (SK-20) have a delegated agent's authority, whichever run started the code, because the platform cannot always tell which run's code made a call (settled 2026-10-11, open question 6);
  - the reviewer only reads (CR-2).

  Refusals say `not_permitted_for_role`, as in PD-F8.
- LG-14. An agent MUST NOT create a record attributed to the user (RA-16), or give an entry the origin `published` without a citation (LG-15).

### Published and project results

- LG-15. A published claim or result enters from its citation: a source locator (DOI, ePrint ID, URL or an imported source revision), authors, title, version or publication date, the statement as the source gives it, its applicable assumptions and parameters, and its reported measures. Fields the source does not give are recorded as not stated, never guessed; a citation without a locator or a statement is refused, and a source with no publication or version date, such as a web page, records its retrieval date, marked as such. When the source is an imported PDF, the entry carries a page and region anchor ([PDF sources](pdf-sources.md)). A catalog record from a connector can accompany the citation but not replace it. The evidence type is `source_claim` and the status `recorded` (D34; research workspace; the pack never takes its published rows from agent results).
- LG-16. Published entries need no independent check, reproduction or review to be recorded or to be eligible for the frontier (D34). The reviewer may still check that the citation resolves and supports the statement (CR-4); its findings attach to the entry and change nothing in its status. A new version of the source is a new revision of the entry.
- LG-17. Project claims and results start `proposed` and keep their evidence and review status visible (research workspace). One becomes `established` only by a decision in effect, the user's or the main agent's; the main agent's needs the project's gate to hold (settled 2026-10-11, open question 2). The default gate:
  - the entry has evidence that counts: a `supports` or `derivation` link whose type is neither `synthetic` nor `unclassified`, and whose target's lineage shows no synthetic input (RA-17, D29);
  - a result derived by argument also has an `independent_check` link whose target was produced by the user, by a delegation started to check that candidate (its brief names the entry and revision; the delegated run starts with a fresh context, PD-13, and may later get an environment of its own, D81) and run by an agent other than the one that derived it, or by a verification review if that extension is enabled (D27). The target's attribution and lineage decide, not who added the link. A consistency review, or a main agent's run, never counts (CR-1, CR-8; external skills; the pack's promotion rule);
  - its computational evidence is pinned to the producing executions and artifacts;
  - a completed consistency review covered it, and no `fail` finding anchored to it is unresolved (CR-27); the review is necessary, never sufficient (CR-8);
  - it has no open flag (LG-28).

  A check or review names the revision it examined, and still counts for later revisions that change neither the statement, the type-specific fields, the `assumes` links nor the targets of the `derivation` links.

### Frontier

- LG-18. A frontier category names its target and kind of result, its comparison measures (each with a unit and a direction: lower or higher is better) and its condition keys, such as attack model, cost model or parameter set. Categories are versioned project records that the user or the main agent defines (D33; research workspace) (new).
- LG-19. A result in a category records a value for each condition key, and its measures as numbers in the category's units or, failing that, as given, such as an asymptotic expression (new).
- LG-20. Published results that are `recorded` and project results that are `established` are eligible for the frontier. An eligible entry that becomes `disputed` stays eligible, marked as disputed; entries in other statuses are not eligible (settled 2026-10-11, open question 3).
- LG-21. Results are compared only within a category, and only when their conditions are equal on every condition key. A dominates B when every measure of the category is a number for both, A is at least as good on each and better on one. The best known results under one set of conditions are the eligible results no other eligible result dominates. Conflicting measures, such as time against memory, therefore keep several best known entries, and results under different conditions are shown side by side with their differences. A result with a missing measure or one that is not a number is listed beside them as not compared (research workspace; D33) (new).
- LG-22. Where the rule cannot compare (different conditions, measures that are not numbers), the user or the main agent can record a `dominates` link between two eligible results, with a reason; it removes the dominated one from the best known, or from the results listed as not compared (new).
- LG-23. The frontier shows for each entry the statement, conditions, measures, origin, evidence types, status, open findings and flags, and links to its source or project artifacts. Project results that are not eligible are shown as candidates, with their status and the best known entries they would dominate if established (research workspace).
- LG-24. Results that are no longer best known stay in history. Each change to a set of best known results is recorded with its cause, so the frontier at any past time can be shown. Leaving the frontier changes no status: a published result that a stronger one dominates is still `recorded` (D33; research workspace).

### Review findings

- LG-25. Review findings can anchor to an entry revision and one of its fields (CR-17), and the entry shows the findings anchored to it with their own statuses. A finding's transitions never change an entry's status, and an entry's changes never resolve a finding (CR-8, CR-27; implementation plan W4). For CR-28, a finding's anchor on an entry field counts as replaced only when a later revision changes that field, and supersession is decided when the next review is dispatched, so an author can change the field and then claim the fix (settled 2026-10-11, open question 5; CR-28 amended; as LD-25). A `math` finding on an entry's field is confirmed or re-flagged by the math check instead (CR-38).
- LG-26. The user or the main agent can open an issue from a finding; the issue `concerns` the finding, and each keeps its own lifecycle (new).

### Assumptions and reconsideration

- LG-27. Claims, results and decisions name the assumptions they rely on with `assumes` links pinned to assumption revisions. An assumption or a claim can be bound to the document block that states it ([living documents](living-documents.md), LD-34) (new).
- LG-28. The platform flags an entry for reconsideration when ([product](../PRODUCT.md); research workspace; [architecture](../ARCHITECTURE.md)) (new; the spread to all dependents settled 2026-10-11, open question 4):
  - an assumption it relies on is withdrawn, or gets a revision that changes its statement;
  - the document block bound to it, or to an assumption it relies on, is changed or removed;
  - its evidence changes: a document block or math span it is pinned to is changed or removed by a later revision; an entry it is pinned to changes its statement or becomes `disputed`, `refuted`, `superseded` or `withdrawn`; or another artifact it is pinned to gets a newer revision;
  - an entry it builds on or assumes becomes `disputed`, `refuted`, `superseded` or `withdrawn`.

  Flags pass on to the entries that depend on a flagged entry through `assumes`, `builds_on` and evidence links. Each flag names its cause, and the reviews whose snapshots held the flagged revision.
- LG-29. A flag changes no statement, status or conclusion. It stays until the user or the main agent clears it with a reason: the entry still holds and is revised to pin the new revision, its status changes, or the change does not affect it. Flags and their clearing are ledger events that reviews read (CR-9); a flag alone dispatches no review (new).

### Mutations and access

- LG-30. Every change is a `Mutation` naming each entry it changes with its expected revision, an operation ID, the actor and the change. A change to several entries commits all or nothing: a decision with the status changes it decides, a `supersedes` link with its target becoming `superseded`, a split with its new issues. A stale expected revision returns a conflict with the current revision. Unlike document patches, ledger changes are never rebased, because an entry's fields depend on one another: evidence supports a particular statement (D28). Reversing a decision is a new decision that states its own status changes; nothing reverts by itself (new).
- LG-31. A mutation retried with the same operation ID returns the first result and creates nothing, including after an orchestrator restart. A change commits together with its event, or not at all (implementation plan; external skills, gate 5).
- LG-32. Sessions in the project's default environment and in dedicated ones reach the same ledger, through the harness's tools and from worker code through the platform API (SK-20), whose calls the executor and the worker carry as the document helper's are ([living documents](living-documents.md), LD-52). A caller from another project gets `not_found`, as the worker answers other projects' clients (D78). Write failures are reported, never dropped (external skills) (new).
- LG-33. Each change commits with its `Event` (RA-26): `ledger.entry_created`, `ledger.entry_revised` (the fields and links changed, the status before and after), `ledger.work_recorded`, `ledger.flag_raised`, `ledger.flag_cleared`, `ledger.frontier_changed` and `ledger.import_completed`. Agent-written ledger text counts toward CR-10's durable-text predicate; ledger revisions do not count there as artifact revisions (settled 2026-10-11, open question 5; CR-10 amended).

### Views

- LG-34. The board, the frontier, the issue list and the history are projections of the entries, work records, flags, links and linked findings and their events, and are rebuilt from them. Only per-user view settings, such as column order and filters, are stored for them (D24; research workspace) (new):
  - the board groups entries by type and status, with their indicators; moving a card is a status mutation that names the expected revision, or a decision for the statuses of LG-4;
  - the issue list shows issues, and open flags grouped by their cause, with what each issue concerns and its work records (settled 2026-10-11, open question 4);
  - the history lists revisions, work records, flags, decisions and frontier changes over time, for one entry or the project, by session, actor and type.

### The external pack and imports

- LG-35. The crypto-analysis-skills pack's bindings write its board and knowledge records through the platform API (SK-25), with the authority of LG-13, mapped as follows (external skills, gate 5) (new):

  | Pack record | In the ledger |
  | --- | --- |
  | Idea | An `open` issue, with `builds_on` links to the claims it starts from |
  | Child outcome | A work record on its issue holding the returned payload unchanged (PD-17). Its findings, dismissals and lemma proposals become the records of their own rows, linked to that work record, so nothing is recorded twice. No status changes |
  | Finding | A `proposed` result or claim that `answers` its issue and `compares_to` the published row it names, if any, with the stated difference and justification |
  | Dismissal | A work record: `limit` for a lack of ideas, time, tools or context; `obstruction_claimed` for a demonstrated obstruction |
  | Lemma proposal | If derived, a `proposed` claim with `derivation` evidence; if from the literature, a `recorded` published claim with its citation (LG-15). Hypotheses become `assumes` links or text, and `restates` links are kept |
  | Critique | A work record ending `assessment` on each entry it reviewed, citing the critique report |
  | Promotion | A `proposed` decision with the status change it decides, which the main agent's tool call or the user puts in effect (LG-17) |
  | Lane closure | A `proposed` decision that excludes a scope, citing its critique, with `obstructed` for the issues it closes (LG-7) |
  | Published row | A published result. The binding adds the fields LG-15 and LG-19 need, from the cited source or catalog record; a row without them is refused, naming them |
  | Knowledge entry | A claim with its evidence items as evidence links: `recorded` if cited to a paper or specification, otherwise `proposed` until a decision establishes it. For a promoted lemma, it is the claim its proposal created |
  | Gap | A `proposed` claim of the kind negative result: what was looked for, the channels searched, and what was found |
  | Lesson | Not an entry: an insertion into a project notes document ([living documents](living-documents.md), LD-16) |
  | Notebook | Not stored: rebuilt from work records |
- LG-36. A pack record's event ID is its operation ID, so a retried append is a duplicate, not a second record. A record that fails validation is refused before anything is written, with the reason (new; the pack tells validation failures from write failures).
- LG-37. A deliberate import loads existing pack data, the knowledge files and session ledgers of one pack project, from a pinned artifact revision. The user starts it. It runs as one import with an ID, maps records as LG-35 does, keeps their original IDs, times and authors as data, and attributes what it creates to the import and the user. Knowledge entries cited to a paper or specification become `recorded` published claims. The rest of what the pack had accepted is listed for the user before anything takes effect: knowledge entries supported by a computation, a person's decision or a derivation with an independent check, and promotions and lane closures. What the user confirms, all or part, keeps its standing through one decision of the user's, which the import records. Everything else stays `proposed`. Evidence whose type the record does not settle, such as a computation that may be exact or numerical, is `unclassified` (external skills; research workspace) (new).
- LG-38. An import keeps one authoritative history. Afterwards the ledger is the authority, and the pack's files are never written back. Importing the same data again creates nothing, and a later snapshot adds only the records not yet imported. A record changed on both sides is reported, not merged. Records that the pack keyed by one ID but that differ, such as two lemma proposals with one lemma ID, become separate entries, and the import's report lists the clash. Invalid records are listed with their raw content, and nothing is filled in by default (external skills; as PD-18) (new).
- LG-39. The ledger can be exported as a snapshot for archiving. An export becomes authority again only through a deliberate import (research workspace: exports do not replace the history) (new).

## Contracts used

- `ArtifactRef`: entries are control-plane artifacts whose revisions come from mutations (proposed addition 9 in the [index](README.md#proposed-contract-additions-new)); evidence links are pinned references (RA-19).
- `Anchor`: findings and issues point at entry fields (proposed addition 7); evidence points at document blocks and math spans, PDF pages and regions, and execution logs.
- `Mutation`: every change names the expected revision of each entry it changes and an operation ID, commits all or nothing, and returns the current revision on a conflict; ledger changes are never rebased.
- `Event`: the ledger events of LG-33, consumed by reviews, the views and the GUI.
- `ExecutionContext`: changes made by worker code name their execution where the platform can tell (SK-20); evidence cites executions by their context.
- `PermissionRequest`: not used. Statuses, decisions and flags grant nothing, and no ledger record changes the policy.
- `EnvironmentRecord`: not needed.

## Acceptance fixtures

Fixtures are `scripted` unless marked: agents' and the reviewer's output is scripted, and the user acts through the API. The test category is key recovery on a test scheme, with the measures `time` and `memory` (log2 of operations and of bits, lower is better) and the condition keys `model` (classical or quantum) and `success` (success probability at least 1/2).

**LG-F1 A published result from its citation** (LG-14 to LG-16, LG-21)
- *Given* the test category with no entries.
- *When* the agent records published result P1 (time 120, memory 60) citing an ePrint ID, its version date and an anchor on page 4 of the imported PDF; a review then raises a `warn` finding on P1's statement; and the agent tries to record a published result without a citation, and a claim attributed to the user.
- *Then* P1 is `recorded` with `source_claim` evidence and best known at once, with no check, reproduction or review required. The finding shows on P1, and P1's status is unchanged. Both other tries are refused.

**LG-F2 Conflicting measures and history** (LG-18 to LG-24)
- *Given* P1 and published P2 (time 110, memory 80).
- *When* published P3 (time 125, memory 65) and Q1 (quantum model; time 90, memory 90) are recorded; project result R1 (time 105, memory 80) is proposed, then established; published P4 gives its time only as $2^{0.29n}$; the user records that P1 dominates P4, with a reason; and the main agent marks P1 `disputed`.
- *Then* P3 is dominated by P1 and is never best known. The classical best known are P1 and P2, then P1 and R1 once R1 is established; before that, R1 is a candidate that would dominate P2. Q1 has a group of its own. P4 is listed as not compared until the `dominates` link removes it. P1 stays best known, marked as disputed. P2 stays `recorded`, and the history shows it best known until R1's decision.

**LG-F3 The gate for project results** (LG-13, LG-17)
- *Given* claim C1, "for every prime $p$ and every $a \not\equiv 0 \pmod p$, $a^{p-1} \equiv 1 \pmod p$", proposed by delegated run D1 with `derivation` evidence pointing at a proof block of `ff.md`; a review covered it and left an open `fail` finding on it.
- *When*:
  1. D1 tries to establish C1;
  2. the main agent adds an `independent_check` of its own and tries;
  3. a delegation whose brief names C1's revision and asks for a check starts run D2, whose report D2's `independent_check` link points at, and the main agent tries again;
  4. the main agent rebuts the finding, the next review accepts the rebuttal, so the finding is `withdrawn` (CR-26), and the main agent tries once more.
- *Then* the first try is refused with `not_permitted_for_role`. The second and third are refused, naming the unmet condition: no check that counts, then the unresolved finding. The fourth records a decision in effect, and C1 becomes `established` in the same mutation: D2's link added a revision, but the statement and the proof block that D2 checked and the reviews covered are unchanged.

**LG-F4 Evidence type is not status** (LG-9 to LG-11, LG-17)
- *Given* claim C2, "$a^{16} = 1$ for every nonzero $a \in \mathrm{GF}(17)$", with `exact_computation` evidence pinned to `ff.json` r1 and its execution; claim C3, "for every prime $p$ …", with a `finite_test` link to a run over the primes below 1000; claim C4 with only `synthetic` evidence; and claim C5 with no evidence.
- *When* the executions exit 0, a checkpoint review over them passes, and the main agent tries to establish C4.
- *Then* no status changed with the exits or the review. C2 shows `exact_computation`, C3 shows `finite_test` with its scope, and C5 shows as asserted only. Establishing C4 is refused, because synthetic evidence counts for nothing.

**LG-F5 An assumption changes** (LG-27 to LG-29)
- *Given* assumption A1, "$p = 17$", at r1; C2 `assumes` A1 r1; claim C6 `builds_on` C2; and a review whose snapshot held C2's revision.
- *When* an agent binds A1 to a block of `ff.md` (A1 r2) and edits another block; the user then edits A1's block; the main agent revises A1's statement to match (r3) and clears its flag, revises C2 to pin A1 r3 and clears its flags, and clears C6's flags with the reason "not affected".
- *Then* the binding and the other edit flagged nothing. The user's edit flagged A1, C2 and C6, each with its cause, and C2's flag names the review. No statement or status changed until the agent's revisions. Each clearance is attributed and gives its reason, and each flag and clearance has its event.

**LG-F6 Findings stay apart from status** (LG-25)
- *Given* `fail` findings F1 and F2, both anchored to C2's statement at r1.
- *When* C2 r2 adds an evidence link; the author then revises the statement (r3) and claims a fix for F1; the next review is dispatched; and a confirmation review confirms F1's fix.
- *Then* after r2 both findings are still `open` and attached. When the next review is dispatched, F2 becomes `superseded` with a link to r3 (CR-28), and F1, claimed fixed, does not. F1 ends `confirmed fixed`. C2 was `proposed` throughout.

**LG-F7 One record, both kinds of workbook** (`scripted`, `worker`; LG-13, LG-30 to LG-32; gate 5)
- *Given* sessions A (default environment) and C (dedicated environment) of one project, claim K at r1, and a client of another project.
- *When* A's agent adds an evidence link to K expecting r1; its connection drops after the change commits, and it retries with the same operation ID. The pack's helper code in C's kernel then adds an evidence link expecting r1, rereads, adds it expecting r2, and tries to mark K `disputed`. The other project's client reads K. The orchestrator restarts during a third change from C, which C retries.
- *Then* A's change is r2, and its retry returns r2 and creates nothing. C's first change gets a conflict naming r2; its second creates r3, attributed to C's session and environment, and to its execution where the worker can tell. The status change from worker code is refused with `not_permitted_for_role`. The other project gets `not_found`. The interrupted change exists exactly once after the retry.

**LG-F8 A delegated outcome on its issue** (LG-6, LG-7, LG-13; gate 4's link)
- *Given* issue I-2, `open`, and a delegation for it whose child returns status `blocked`, with one dismissal for lack of time and one claimed obstruction.
- *Then* the outcome is kept as returned (PD-17) in a work record on I-2; the dismissal and the obstruction are work records ending `limit` and `obstruction_claimed`, linked to it; I-2 stays `open`. The child's call to make I-2 `obstructed` is refused, and so is a decision of the main agent's without an assessment. After a delegation whose brief names the claimed obstruction records an `assessment` accepting it, the main agent's decision makes I-2 `obstructed`, naming the excluded scope, the evidence and the assessment.

**LG-F9 The pack's bindings** (`worker`; LG-13, LG-35, LG-36; gates 4 and 5)
- *Given* crypto-analysis-skills at its pinned commit, with bindings updated to the platform API.
- *When* its helper code, run by the main agent, appends an idea, a child outcome, a finding without its justification, the same finding with it, a lemma proposal derived by one child run, and a critique whose independent check comes from another child run, started by a delegation that names the lemma. After a review has covered the lemma, the helper appends a promotion and a published row with its catalog fields, and retries the promotion with the same event ID. The main agent then puts the proposed decision in effect with a tool call.
- *Then* the ledger holds the records LG-35 maps them to, attributed to the session, the environment, the execution where the worker can tell, and the skill revision. The finding without justification was refused before anything was written. The promotion was recorded as a `proposed` decision, and its retry created nothing; the lemma became `established` only with the main agent's tool call.

**LG-F10 A deliberate import** (LG-37, LG-38)
- *Given* a snapshot of a pack project: knowledge entries (a derived result with an independent check; a legacy one with only a derivation, which the pack's current helpers would refuse; one cited to a paper), two gaps, a lesson, and two session ledgers in which two lemma proposals share a lemma ID and one line is malformed.
- *When* the user imports it, imports it again, then imports a later snapshot with three new events.
- *Then* the first import keeps the original IDs, times and authors. It lists the checked result for the user, who confirms it, and records one decision of the user's in effect, which establishes it. The derivation-only entry is `proposed`, the cited one `recorded`, the gaps are `proposed` negative-result claims and the lesson is in a notes document. Both lemma proposals are separate claims, with the clash in the import's report, and the malformed line is listed raw. The second import creates nothing; the third adds only the records its three events map to. No pack file was written.

**LG-F11 A connector PDF behind a published claim** (`worker`; LG-15; gate 6)
- *When* the agent retrieves a paper's PDF with a crypto-research-mcp tool through the MCP proxy (RA-F6), reads page 4 with `pdf_pages`, and records published claim C7 citing line `p4:12`.
- *Then* C7's anchor resolves to the PDF revision whose SHA-256 equals that of the retrieved bytes, with page index 4, its label and the line's box, and the model's context held a reference, never the PDF's bytes.

**LG-F12 The W4 chain** (`scripted`, `worker`; LG-8, LG-15, LG-17, LG-25, LG-26, LG-28, LG-31, LG-34)
- *Given* a project on babbage with `crypto-base`; session A in the default environment and session B in a dedicated one; and a test PDF stating "$a^{p-1} = 1$ for every nonzero $a \in \mathrm{GF}(p)$" on page 2.
- *When*:
  1. in A, the agent imports the PDF and records published claim C1, anchored to that line;
  2. it runs `finite-field-identity-v1` in the Sage kernel, which writes `shared/results/ff.json` r1, and records claim C2, "$a^{16} = 1$ for every nonzero $a \in \mathrm{GF}(17)$", with `exact_computation` evidence pinned to `ff.json` r1 and its execution, and `assumes` A1, "$p = 17$";
  3. in B, the agent drafts `ff.md` with C2 pinned, the results table embedded as `latest`, and A1 bound to a block ([living documents](living-documents.md), LD-F16);
  4. the user edits A1's block, and comments on the equation for A's agent, which generalizes it to every prime;
  5. A's turn ends with a terminal review, whose `warn` finding says the generalization claims more than its evidence shows;
  6. A's agent records issue I-1, "prove for every prime $p$", which `concerns` C2 and the finding; it revises A1 and C2 and clears their flags; and it answers the finding with `fix`, rewording the equation to cite C1 for every prime and C2 for $p = 17$;
  7. the orchestrator restarts while the confirmation review of that fix runs.
- *Then*:
  - C1 is `recorded` with its page anchor and needed no check; C2 is `proposed` with `exact_computation` evidence whose scope is $p = 17$;
  - B read `ff.json` r1 and C1 through the synced shared area (RA-6) and the ledger;
  - the user's edit flagged A1 and C2 without changing them; the revisions and clearances are attributed to A's run, with reasons;
  - the finding was anchored to C2 and to the equation in the document; the confirmation review dropped by the restart was dispatched again and confirmed the fix (CR-34), so the finding ended `confirmed fixed`, and nothing was recorded twice (LG-31); C2 stayed `proposed` throughout;
  - the board shows C2 (`proposed`, no open finding or flag), C1 (`recorded`) and I-1 (`open`, concerning C2 and the finding), and the history lists every step with its actor and session.

**LG-F13 Views are rebuilt, not stored** (LG-34, LG-39)
- *When* the board, frontier, issue list and history are dropped and rebuilt from the records; the user moves the same card in two browser tabs that show the same revision; and an export of the ledger is edited by hand.
- *Then* the rebuilt views equal the originals, including the frontier's history; the second move gets a conflict naming the first's revision; and the edited export changed nothing in the ledger.

## Open questions

1. **Entry types.** Six types (LG-2): frontier candidates are results not yet eligible, experiments are tasks plus evidence, and the pack's gaps are negative-result claims. Should experiments or conjectures have types of their own? Recommended: as drafted.
2. **Settled (2026-10-11):** the main agent may establish project results under the gate (LG-17), accept an obstruction after an independent assessment (LG-7), clear flags with a reason (LG-29) and refute published results, each attributed and reversible by the user; a project setting can reserve any of these to the user, and by default reserves none (LG-13).
3. **Settled (2026-10-11):** frontier ranking is automatic only within equal conditions and on numeric measures, with explicit `dominates` links for the rest; disputed entries stay, marked (LG-20 to LG-22).
4. **Settled (2026-10-11):** flags spread to all dependents through `assumes`, `builds_on` and evidence links, and when pinned evidence changes, and the issue list groups them by cause (LG-28, LG-34).
5. **Settled (2026-10-11):** a finding on an entry field is superseded only when that field changes, decided when the next review is dispatched (LG-25, CR-28 amended), and ledger revisions count toward CR-10 only as text (LG-33, CR-10 amended).
6. **Settled (2026-10-11):** calls from worker code have a delegated agent's authority (LG-13); the pack's promotions wait for the main agent's tool call. Worker code reaches the ledger by the document helper's route ([living documents](living-documents.md), LD-52).
7. **A filesystem mode for the pack.** The pack wrote JSONL files. Drafted: the platform API only, so there is one authority (LG-35); [external skills](../EXTERNAL-SKILLS.md) leaves a filesystem mode optional. Recommended: as drafted.
8. **Project tasks and session plans.** Tasks on the board are project-wide, and plan steps can link to them. Does that answer [plans and delegation](plans-and-delegation.md)'s question 5 about project-level plans?
9. **Lessons.** Drafted: the pack's process lessons go to a project notes document, not to a ledger type (LG-35).

## Sources

[Research workspace](../RESEARCH-WORKSPACE.md) (project-wide research state, frontier and published results), [external skills](../EXTERNAL-SKILLS.md) (shared board compatibility, gates 4 to 6), [MCP connectors](../MCP-CONNECTORS.md) (attribution levels), [feature plan](../FEATURE-PLAN.md), [product](../PRODUCT.md), [architecture](../ARCHITECTURE.md) (storage), [consistency review of the plan](../CONSISTENCY-REVIEW.md) (ledger and open items), [implementation plan](../IMPLEMENTATION-PLAN.md) (W4), [roadmap](../ROADMAP.md) (first end-to-end fixture), decisions D24, D25, D27–D30, D33, D34, D78, D80, D81. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md) report (project memory with evidence categories and supersession, session claims and verification records, the disabled dashboard), the [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md) report (knowledge files, no structured board) and the [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md) report (typed project entries, kinds of evidence, dependent reviews going stale). crypto-analysis-skills at [`4089e99`](https://github.com/regenscheid/crypto-analysis-skills/tree/4089e99415f349dde406c8e57a227715002ee166): the research board protocol and the workbench knowledge files.
