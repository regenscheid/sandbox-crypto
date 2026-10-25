# Automatic consistency review

Date: 2026-10-11. **Status: Approved as drafted (2026-10-11, by the user), with CR-10 and CR-28 amended for living documents and the ledger, and `math` findings added (CR-36 to CR-38, D82), later the same day.** Open questions 2–5 and 7 are settled as proposed. The CR-10 defaults stand until the user's own CScience settings are known (question 1); questions 6 and 8 remain open. W0 behavior specification; conventions in the [index](README.md). As built: [consistency review](../CONSISTENCY-REVIEW.md).

## Scope

The automatic consistency review checks an agent's claims against recorded evidence while the agent works and when it finishes, and follows each finding through the author's response to confirmation or another outcome. It behaves like CScience's review (D35, confirmed): checkpoint and terminal scheduling, a constrained reviewer, findings with severity, author responses and claimed-fix confirmation.

Out of scope: mathematical verification and reproduction, a separate role that may run code (D27, proposed extension); action and security monitoring ([monitoring](../MONITORING.md)); the rubric and prompts, written independently (D56); promotion of results into the ledger ([research workspace](../RESEARCH-WORKSPACE.md)).

Terms:

- **Author**: the session's main agent. Work by its delegated agents counts as the session's work.
- **Review**: one reviewer run over one evidence snapshot, of type `checkpoint`, `terminal` or `confirmation`.
- **Evidence snapshot**: the pinned transcript entries, executions, artifact revisions and findings a review may read.
- **Watermark**: the position in the session's transcript and events up to which its work has been reviewed.
- **Finding**: one problem the reviewer reports, with its own lifecycle.

## Behavior

### Reviewer

- CR-1. The reviewer is a separate agent role with its own model settings ([plans and delegation](plans-and-delegation.md), PD-27). It traces claims to evidence and does not recompute (D27; CScience).
- CR-2. Its tools read the snapshot (transcript entries, execution records and logs, artifact revisions with their extracted text and existing renders, including those its ledger entries' evidence pins, and the document revisions, delegation outcomes and entries that evidence pins, as context (CR-14), and the platform's list of what each run did in the window (its tool calls that change something, each with its target, its outcome, `ok`, `refused` or `error`, and what it made, and the blocks each document revision changed since the revision before), against which the reviewer matches the actions the author narrates (amended by the coordinator 2026-10-15 for lab 40, which the owner can reverse); a confirmation's holds what the runs did since the findings' reviews, and the artifacts a claimed fix names by path (amended by the coordinator 2026-10-15 for lab 41, which the owner can reverse); and a run's review holds its delegated runs' work once each delegation ends: their actions in that list, each naming its delegation, their outcomes and the result files the outcomes pinned, as context, delegations of delegations included (amended by the coordinator 2026-10-15 for lab 41's B5, which the owner can reverse), ledger entries, open findings) and record findings. It MUST NOT have shell or kernel access, package installation, writes to files, documents, plans or the ledger, web or MCP access, delegation or permission decisions (CScience's reviewer likewise has no execution, editing, installs, web search or delegation). The harness enforces this with tool grants, not prompt text (new; OpenScience Desktop's reviewer is read-only only by instruction). A reviewer with execution would be the verification role, with its own review type and records (D27). *Amended 2026-10-15 by the owner, with the [context management](context-management.md) spec; built 2026-10-16 but for the folds, which do not exist yet:* the list of what each run did also holds, as context, the actions of earlier runs that the reviewed run's view held, in its raw tail and in its folds' skeleton lines, each naming its run, so that an answer narrating earlier work is matched against them rather than flagged. *Known limit (the coordinator, 2026-10-16):* an earlier run's true action can still draw a false `missing_action` finding when its result was left out of the view, the run predates the archive, or the run was never reviewed and so recorded no action; this errs toward a false fail, never a false pass.

  *Amended 2026-10-16 by the coordinator for lab 42, which the owner can reverse:* the list of what each run did holds every call that may change something, of any effect class but `read`: plan changes (the plan tools' and `track_progress`, each with the step and status, or the plan's status, it recorded), memory proposals and cancels as well as effects (B2). Every snapshot holds, as context, the session plan as recorded when the review's window was read: its status, its goal outcome and each step's latest recorded status (B2). A terminal review's list also holds, as context, the run's actions and its document revisions' changed blocks in its windows reviewed before, its delegated runs' included and no other run's, so that an answer narrating the run's earlier work is matched against them, and no finding is raised on an earlier action itself (B5). A run's delegated runs' work includes their executions with their output, each in each state their calls saw it, bounded as the run's own are; when a run has more than a review holds, first, for each number its outcome states and its brief does not, the latest whose output holds it, or nearly, then the latest, with a bounded number of comparisons, the outcome saying how many were left out; and, for an output cut short, the file its whole output was saved to, when the run recorded it. A delegation's work is recorded with its outcome first. A large batch may cross bounded review windows; each later piece brings the same pinned outcome as context, so a review holding a delegated execution also holds its outcome. A partial piece may yield findings but has incomplete coverage and cannot pass the whole outcome (B3, amended 2026-10-18). Open findings take their place in a snapshot before new work, in as much as half of it, so that new work cannot crowd them out of the review that weighs them.

  *Amended 2026-10-16 by the owner, with the [figures](figures.md) spec (FG-32, FG-39); built (F11; [consistency review as built](../CONSISTENCY-REVIEW.md)):* the reviewer's reads include `view_image`, offered when its alias accepts images, for image revisions in its snapshot (CR-3). An SVG, a PDF page or a region is drawn for it by the platform in the session's environment, as the platform's own call, which writes nothing in the workspace; the review's record names the call, and no execution has the reviewer as its actor (CR-F8).
- CR-3. Every read resolves to a revision that existed when the review was dispatched, and the review record lists each revision read (D28).

### What the reviewer checks

- CR-4. The rubric, in the project's own words (D56), covers at least:
  - actions the author claims that have no matching execution, tool call or artifact;
  - values and results that contradict recorded output;
  - claims stronger than their evidence type, such as a finite check presented as a proof or synthetic data as a measurement (D29);
  - citations, source references and anchors that do not resolve or do not support the claim;
  - artifacts that contradict each other, such as a caption and its data;
  - methods that cannot support the stated claim;
  - progress and completion claims without the deliverables they name.

  *Amended 2026-10-15 by the coordinator, which the owner can reverse:* the check that a citation resolves and supports the claim is recorded. A claim or result quotes its reference (LG-8): a PDF's page and lines, or an ePrint passage. The platform looks for the quote in its own record of the tool results an agent read, the PDF tools' results under the PDF's SHA-256 or the e-print tools' results for the paper and date stamp, and pins the one that holds it beside the entry in the review; a quote it does not find cannot be confirmed. The source is what a tool returned, with the tool and its arguments: it may be a PDF an agent made, unless the e-print server or the user vouches for the PDF (LG-16). The reviewer reads all of that source and of the entry, and records a citation check, `supported`, `unsupported` or `inconclusive`, bound to the entry revision's statement, fields, assumptions and evidence, with the tool and arguments the source came from. An ePrint is named by its identifier and the server's date stamp, not by exact bytes (LG-8).

  *Amended 2026-10-16 by the coordinator for lab 42's B3, which the owner can reverse:* a delegation's outcome, its summary, verdict and report, is the delegated run's claim, not recorded output. A value it states, or the author relays from it, is checked against an execution of the delegated run that states the same quantity (CR-2), the latest successful one when several do, and where they differ the output is the record. A value missing from an output cut short, or from executions the review left out, is unverified, never contradicted, and no other claim is judged by it.
  *Amended 2026-10-16 by the owner, after context-management lab round 1 (CM-P1):* one more check, a suggestion only. When an answer, note or document relies on a ledger entry, or declines or reopens something because of one, and names no handle (D-3, A-1, C-4, I-3), the reviewer may raise one finding of category `handle_citation`, severity `note`, anchored to that text, with the handle it should cite as its remedy. It is advisory by construction (CR-18's amendment): it never blocks, holds, rejects or challenges, and the agent is not made to act on it.
- CR-5. Durable artifacts and final reports are held to a stricter standard than passing remarks in conversation (CScience).
- CR-6. Missing evidence is not proof of fabrication. When an earlier value is unavailable, such as a truncated log or a record outside the snapshot, the finding is `inconclusive`, not `fail` (CScience).
- CR-7. Each review records what it checked and found consistent, not only its findings (feature plan; OpenScience Desktop records checked items).
- CR-8. A passed review means no inconsistency was found in the reviewed evidence. It is not a proof, a reproduction or a promotion into the ledger, and review records cannot grant permissions or change policy ([feature plan](../FEATURE-PLAN.md), [external skills](../EXTERNAL-SKILLS.md), monitoring).

### Triggers

- CR-9. Triggers come from durable events: artifact revisions, including files written by shell commands and reported by the worker ([research artifacts](research-artifacts.md)), executions, document and ledger changes, and transcript entries. Agent narration and GUI callbacks are not triggers (feature plan; OpenScience Desktop misses shell-written files).
- CR-10. A checkpoint review is due during a turn when the minimum interval since the session's last dispatch has passed and, since the watermark, any of these holds (CScience has these kinds of triggers):
  - at least 3 new artifact revisions, not counting revisions of documents and ledger entries, which count through the next two predicates;
  - at least 2,000 characters of agent-written durable text (documents, reports, ledger entries, plan text);
  - a new structural block in a durable document (proposed meaning: a section, table, figure, theorem or claim);
  - kernel work: 40 cells, with a 30-minute interval (the inspected source pairs these two values; how they combine needs checking);
  - the user asks for a review.

  The minimum interval is 120 seconds. All values are project settings; the defaults are the inspected CScience source defaults, pending the user's actual settings.

  *Amended 2026-10-11: an agent's edits to a document or a ledger entry count as text toward the 2,000-character trigger, and its new structural blocks toward the third, not as artifact revisions, so that a few small edits do not dispatch a review by themselves ([living documents](living-documents.md), LD-40; [ledger](ledger.md), LG-33).*
- CR-11. A terminal review is due when the main agent ends a turn that produced artifacts or executions with effects, left findings `open` or `claimed fixed`, or ends by claiming the work is complete. A classifier labels the final message, and its label and version are recorded (CScience). Other turns get no terminal review.
- CR-12. Reviewer runs never trigger reviews. A turn the user interrupted, or that failed, gets no terminal review; its work stays after the watermark for the next review (OpenScience Desktop skips such turns, and Claude Code's stop hooks do not fire on interrupts).

### Dispatch

- CR-13. Checkpoint reviews run detached while the author keeps working. A session has at most one review running; triggers that fire meanwhile merge into one pending window (CScience).
- CR-14. A review's snapshot covers the work from the watermark to its dispatch. A completed review advances the watermark to the end of its snapshot; a failed or dropped review does not, so the next review covers the same work (CScience). A snapshot that holds a ledger entry also holds the artifact revisions the entry's evidence pins, even ones written before its window and by another session of the project: ledger pins are project-wide by design, so a reviewer in one session may read an artifact another session wrote when a project entry pins it (amended by the coordinator 2026-10-15 for lab 39's B6, which the owner can reverse). It also holds, as context, what else the evidence that bears on the entry or checks it pins: the document revision a proof's block is in, naming the block; a delegation's outcome, its texts bounded; and an entry at the revision pinned. Context makes no review due and is not under review: a review that holds an entry only as context has not reviewed it (amended by the coordinator 2026-10-15 for lab 40's B3, which the owner can reverse). *Amended 2026-10-15 by the owner, with the [context management](context-management.md) spec (CM-3); built 2026-10-16, each transcript item and each dispatch naming its archive positions:* the snapshot and the watermark name archive positions, and the reviewer reads the original messages from the archive, never fold records or stubs. *Amended 2026-10-16 by the owner, with the [figures](figures.md) spec (FG-41); not built:* a snapshot that holds a text with figures, a document revision, a ledger entry or a chat message, also holds, as context, the files and chart data its reference map pins, at their pins, even ones written before its window; the figure check's latest results for them are given to the reviewer as the platform's observations, as CR-39's numbers are.
- CR-15. A terminal review holds the turn's completion: the user sees the final message marked "review pending" (new). If a checkpoint review is running, the terminal review starts after it and absorbs the pending window.
- CR-16. If the user sends a message while a terminal review is pending, the new turn starts at once. The review continues, and its findings reach the author at the first safe point of the new turn (new).

  *Amended 2026-10-16 by the owner (the response-loop guard):* a finding the turn never delivered, as one the last review of its closing raised, reaches the session's next run as a count with a pointer to `review_findings` and the review panel, not as an interruption; that run goes on with its own task, and may follow the finding up (a check, a fix, a plan step) without being made to.

### Findings

- CR-17. A finding records: ID; review ID and type; the reviewer run (alias, resolved model, rubric version), or for a `math` finding the math check; severity; category (CR-4, or `math`, CR-36; *amended 2026-10-11*; or `figure`, the [figures](figures.md) spec's FG-22, *amended 2026-10-16 by the owner*); a statement in Markdown with LaTeX (D25); one or more anchors (transcript entry, execution log range, artifact revision and block, source page or region); pinned evidence references; an optional suggested remedy; status; and every transition with actor, time and reason.
- CR-18. Severity is `fail` for a materially false or unsupported durable claim or a claimed action without a record, `warn` for a claim that needs correction or qualification, and `note` for information (new names; CScience separates fail-level findings from warnings).

  *Amended 2026-10-16 by the owner:* a finding of the category `handle_citation` is only a suggestion. Whatever severity the reviewer gives it, it is stored as `note`. It is not counted among the session's unresolved findings (so it adds nothing to the counts a run is told, and does not make a conversation turn a terminal one), is never offered to a run as a notice, is not a confirmation target, and takes no `fix` or `rebut` response (the user may dismiss it), so it is never `claimed fixed` or `challenged`. A re-flag never makes it `unaddressed`. A review whose only findings are of this category may still end `passed`. The user sees it in the review panel with the others, and the author's list tool returns it.

- CR-19. A review reports at most 10 findings. With more candidates it reports the most severe and records that the cap was reached (new; OpenScience Desktop caps at 8).
- CR-20. The reviewer sees the open findings and re-flags one instead of duplicating it. A new finding with the same category and anchors as an open one is turned into a re-flag (new).
- CR-21. `fail` findings from a checkpoint review interrupt the author at the next safe point: after the current tool call, before the next model request. `warn` and `note` findings are not injected; the author is told how many exist and lists them with a tool. The user sees every finding at once (CScience).

  *Amended 2026-10-16 by the owner (the response-loop guard):* a run is interrupted only by the `fail` findings that reviews of its own work raised or decided; the session's earlier findings reach it as counts (CR-16).

### Response and confirmation

- CR-22. The author or the user responds to a finding with `fix`, which requires an explanation and references to the changed revisions or messages, or with `rebut`, an explanation with evidence. The explanation is shown to the user (CScience).
- CR-23. Only the main agent and the user can respond. A delegated agent can propose a fix in its outcome, which the main agent then claims (new; CScience likewise keeps finding transitions with the root session).
- CR-24. A `fix` moves the finding to `claimed fixed` and queues a confirmation review. The reviewer checks the finding, its evidence, the explanation and the referenced revisions, and confirms, rejects or cannot decide (CScience). *Decided 2026-10-25 by the owner (B9); not built:* a `fix` no longer moves a finding to `claimed fixed`. It names the revision, or for a finding anchored only to messages the later message, that addresses the finding, and queues its re-check (CR-42); the finding stays `open` until the re-check decides. Only the platform, on a `resolved` re-check, or the user makes it `fixed`.
- CR-25. Only a confirmation review or the user can mark a finding `confirmed fixed`, and the record shows which (user confirmation is new). The author MUST NOT be able to. *Amended 2026-10-11: for a `math` finding, the math check does it too, and the record says so (CR-38).*
- CR-26. The next review evaluates a rebuttal. If it accepts it, the finding becomes `withdrawn`; if not, the finding stays `open` with the reviewer's reason and one more re-flag (new).

  *Amended 2026-10-16 by the owner (the response-loop guard):* when the next review rejects a rebuttal that cites recorded evidence, the finding becomes `challenged` instead: both sides are recorded, the author's rebuttal stands and carries more weight than the reviewer's, and the reviewer's reason is visible context, not a block. No re-flag is counted and no round follows; a later review's matching finding adds to its record. The orchestrator is told, as context, at its next turn, and the user sees both sides in the review panel whenever they like; the user is not asked to decide. A rebuttal without evidence that is rejected stays as above.

### Outcomes

- CR-27. Finding statuses ([feature plan](../FEATURE-PLAN.md#reviews-need-distinct-roles); `withdrawn` is new):

  | Status | Meaning | Set by |
  | --- | --- | --- |
  | `open` | Raised, or re-opened by a rejected fix or rebuttal | Reviewer, user |
  | `claimed fixed` | The author or user says it is fixed; not yet checked | Author, user |
  | `confirmed fixed` | A confirmation review or the user agreed that the fix resolves it | Confirmation review, user |
  | `unaddressed` | The bounded loop ended without a confirmed fix or accepted rebuttal | Harness |
  | `superseded` | The revisions it is anchored to were replaced; nothing was checked | Harness |
  | `inconclusive` | The reviewer could not decide from the evidence | Reviewer |
  | `withdrawn` | A rebuttal was accepted, or the user dismissed it | Reviewer, user |
  | `challenged` | A rebuttal that cites recorded evidence was rejected by the reviewer: both sides stand, with no further round (amended 2026-10-16 by the owner) | Reviewer |

  Only `confirmed fixed` and `withdrawn` are resolved; the rest stay visible as unresolved. The user, or a later review with new evidence, can re-open `unaddressed`, `superseded` and `inconclusive` findings, and the author can still fix `open` and `unaddressed` ones in a later turn.

  *Amended 2026-10-11: for `math` findings, the math check sets `open` and `confirmed fixed` (CR-36, CR-38), and they are never `superseded`.*

  *Decided 2026-10-25 by the owner (B9); not built:* these eight statuses become five, `open`, `fixed`, `dismissed`, `superseded` and `stale`, kept on the finding's record in the review store (CR-40 to CR-47; CR-47 maps the old to the new). Today's `superseded` is closest to the new `stale`; the new `superseded` means replaced by a later review's finding.
- CR-28. Supersession is not a fix. When every artifact revision a finding is anchored to has a newer revision and no fix is claimed, the finding becomes `superseded`, linked to the new revisions, and the next review checks whether the problem persists in them; if it does, the new finding links to the old one. A finding anchored only to transcript entries is never superseded (new; CScience closes such findings automatically, which D28 asks to distinguish from confirmation). *Amended 2026-10-11: an anchor on a block of a living document, or on a field of a ledger entry, counts as replaced only when a later revision changes or removes that block or field, not on every new revision; supersession is decided when the next review is dispatched, so that an author can change the block and then claim the fix ([living documents](living-documents.md), LD-25; [ledger](ledger.md), LG-25).* *Decided 2026-10-25 by the owner (B9); not built:* a replaced revision no longer supersedes a finding by itself: it queues the finding's re-check (CR-42), and only an unresolved re-check, or no re-check within the session, ends it (`superseded` or `stale`, CR-44).

### Bounded completion

- CR-29. After a terminal review with `fail` findings, the author is resumed for a response round, followed by confirmation of claimed fixes; at most 2 rounds per turn (new defaults; CScience bounds its loop, and Claude Code caps consecutive stop-hook continuations). *Amended 2026-10-11: open `math` findings for the turn's work also start a response round, which hands them over together with the terminal review's findings (CR-37).*

  *Amended 2026-10-16 by the owner (the response-loop guard):* only the open `fail` findings delivered to the run take it into a response round; a challenged finding (CR-26) and the session's earlier findings (CR-16) take none.

  With `terminal_hold` enabled, an already active checkpoint review settles before
  the main run ends even when the final answer is conversational, such as saying
  that the requested review is pending. Its findings can use the same bounded
  response rounds. A failed or capped review remains pending without an immediate
  replacement dispatch at closing. User input and cancellation can still interrupt
  the wait; disabling `terminal_hold` reports pending without waiting.
- CR-30. A finding is re-flagged to the author at most twice; then it becomes `unaddressed` instead of being injected again (CScience keeps a re-flag count).
- CR-31. Each review has a turn, time and cost budget. A review that reaches it ends `capped` and keeps the findings it recorded.
- CR-32. When the bound is reached, `open` findings become `unaddressed`, `claimed fixed` findings stay unconfirmed, and the turn completes with a summary of findings by status and severity.

### Review outcomes, recovery and configuration

- CR-33. A review ends `passed` (no findings), `findings`, `inconclusive` (the main claims could not be evaluated), `capped`, `failed` (reviewer error, or invalid output after one retry) or `dropped` (lost to a restart). Only `passed` reads as a pass (feature plan).
- CR-34. An orchestrator restart drops the reviews in progress. A failed, dropped or capped author-work review retains its exact window and watermark. After three failures further attempts are admitted at least five minutes after the last failure, when a later safe point or terminal boundary asks for review; failure never silently moves the watermark past evidence. Confirmation attempts remain separately bounded (amended 2026-10-18 after the SNOVA review audit).

  *Amended 2026-10-18 after the SNOVA audit:* a large author-work window is partitioned into contiguous, reviewable evidence spans. A cap or failure shrinks the next attempted span to at most one quarter of its prior item and byte allowance. Each later span carries bounded exact recent evidence for adjacent cross-window checks. A clean prefix may pass its own span while whole-feed coverage remains incomplete; prior inconclusive, incomplete finding or unreviewed spans prevent a later terminal result from certifying the session. A partial delegated batch keeps its pinned outcome as context and cannot claim complete coverage. Terminal closing catches up at most 24 windows, then reports pending work rather than treating the last answer as fully reviewed.
- CR-35. Automatic review is on by default (D04, D35). Per project: thresholds, minimum interval, the hold of CR-15, bounds and the severity that interrupts. Each review records the configuration revision it ran under ([product](../PRODUCT.md): record the active configuration).

  *Amended 2026-10-16 by the coordinator for lab 43's B6, which the owner can reverse:* with no reviewer configured (no reviewer alias), automatic review is off: no review is dispatched or recorded as failed, and the session's review list says so, recorded once until a review is dispatched again. A configured reviewer alias whose model is unavailable still fails its windows (CR-F10).

### Math findings

*Added 2026-10-11 (D82).* A formula that KaTeX rejects goes back to the agent that wrote it as a finding, while the GUI shows its automatic repair meanwhile. These findings come from a mechanical check, not from a reviewer; they share the findings' records and lifecycle, with the differences below.

- CR-36. When durable text an agent wrote is saved, the platform checks its math with the GUI's KaTeX ([living documents](living-documents.md), LD-53, LD-54). A span KaTeX rejects becomes a finding of category `math` and severity `warn`, anchored to the span, with KaTeX's error, and with the repair the display found as its suggested remedy (CR-17). It is addressed to the agent run that last changed the block holding the span, and the session's main agent answers it (CR-23). Text the user wrote, a reviewer's own statements and chat messages get the display repair only. The check is the platform's observation and needs no model.
- CR-37. Math findings never interrupt the author (CR-21). The platform collects them during the turn and hands them to the author in the turn's closing response round, together with any findings of its terminal review (CR-15, CR-29). Math in text saved outside a turn reaches the session's main agent at its next turn.
- CR-38. The author fixes the source with an ordinary patch; no `fix` response is needed. The check runs again on each new revision of the block holding the span:
  - if KaTeX accepts the span, the finding becomes `confirmed fixed`, recorded as confirmed by the math check;
  - if the span still fails, the finding is re-flagged, and after two re-flags it becomes `unaddressed` (CR-30), so the loop is bounded;
  - if the formula is gone, the finding is resolved as `confirmed fixed`, and its history notes that the formula was removed rather than repaired.

  A `math` finding is never `superseded` (CR-28): the check decides instead.

  *Amended 2026-10-16 by the owner, with the [figures](figures.md) spec (FG-19 to FG-22); not built:* CR-36 to CR-38 apply to findings of category `figure` as to `math` findings: the figure check (a chart or diagram the pinned library rejects, a chart that draws nothing, or a reference to a recorded file that does not resolve) makes them for the agent run that last changed the block or statement, they never interrupt and are handed over in the closing response round with the math findings, and the check of the block's next revision confirms, re-flags or resolves them. A figure in an agent's chat message makes no finding; the agent is told in a notice at the turn's close (FG-22). The [figures](figures.md) spec's FG-F5 is their fixture.

### Copied numbers

*Added 2026-10-15 by the coordinator for lab 39's B6, which the owner can reverse.* A long number an agent copies by hand from a computation's output into a document or the ledger goes wrong by a few digits, and a model reviewing both rarely sees it.

- CR-39. Before the model reviews a snapshot, the platform compares each number of at least 10 digits that its ledger entries (statement and fields) and document blocks state, read through the separators that group digits, with the numbers its text and JSON artifacts hold, including those its entries pin (CR-14). A number that matches none exactly, but is within 3 edits, or 5 % of its digits if more, of one of them, becomes a `fail` finding of category `contradicting_value`, recorded by the review and anchored to the entry's field or the block and to the artifact, with both values and the artifact's key; a number that differs from one of them only in its last two digits, such as p − 1 beside p, is taken as a number of its own. The check needs no model, records at most 4 findings in a review, says that they are its own, and is named to the reviewer, which does not record them again. The findings then follow the review's findings' lifecycle.

### A finding's lifecycle in the review store

<a id="b9-4"></a>This section is decided by the owner and not built (B9, item 4). It changes how findings end and what the review store keeps about them, not where they are kept or what the reviewer checks: the rubric (CR-4, `sandbox-consistency-9`), severities (CR-18), the bounds (CR-19, CR-29 to CR-31) and who may respond (CR-23) stay as they are.

- CR-40. *Decided 2026-10-25 by the owner (B9); not built:* **A finding stays a record of the review store.** Each finding that a review, the math check, the figure check or the copied-numbers check records remains a record of the review store (today's `review_findings`), with the fields of CR-17. It is not a ledger entry and has no ledger type or handle; the owner's reason is that the ledger is the research record people read, and review bookkeeping would clutter it. The store gains what the lifecycle needs:
  - a revision history with attribution: each change of status is a record of its own, saying who set which status (the user, the review controller for a review or a check, or the platform), when, and why (the reason, or the re-check's answer, CR-45);
  - the anchors to the exact revision reviewed (CR-17), which the store already keeps;
  - a link from a finding to the one that supersedes it (CR-44).

  The review controller writes a finding, on the reviewer's behalf or as the check's observation; the reviewer still writes nothing and only reads (CR-2). A ledger entry shows the findings anchored to it, read from the store, as LG-25 says; a change to a finding makes no ledger revision.
- CR-41. *Decided 2026-10-25 by the owner (B9); not built:* **Statuses.**

  | Status | Meaning | Set by |
  | --- | --- | --- |
  | `open` | Raised, and not yet settled | The review controller (for a review or a check); the user |
  | `fixed` | A re-check of this finding, on a later revision of what it reviewed, found it resolved | The platform, on the reviewer's `resolved` (CR-42) or the math or figure check's pass (CR-38); the user |
  | `dismissed` | The author or the user set it aside, with a reason | The main agent (CR-43); the user |
  | `superseded` | A later review of the same work replaced it with a finding of its own, which it links to | The review controller (CR-44); the user |
  | `stale` | What it reviewed is no longer current, and no re-check ran within its session: neither fixed nor open | The platform, at the session's close (CR-44); the user |

  A finding moves from `open` to any of the other four. A `stale` finding goes back to `open`, or on to `fixed` or `superseded`, only through a later re-check or the user. The user may set any status from any other, with a reason, from the workspace. The working agent's statement never makes a finding `fixed` (CR-25), and a delegated agent changes no finding (CR-23, [ledger](ledger.md), LG-13).

  For the gate (LG-17, unchanged), a `fail` finding is resolved when it is `fixed`, or `dismissed` by the user or with a review's acceptance (CR-43). `open`, `stale`, and a dismissal by the agent alone, are unresolved; a `superseded` finding counts as its successor does. So no agent's statement resolves a finding, and LG-7's asymmetry holds: making a finding `open` or `stale` weakens the work it concerns and needs no authority, while `fixed`, or an accepted dismissal, strengthens it and needs the reviewer's re-check or the user. LG-17's "confirmed fix or accepted rebuttal" reads as `fixed` or an accepted dismissal.
- CR-42. *Decided 2026-10-25 by the owner (B9); not built:* **The re-check.** A finding is re-checked when a later revision of the artifact, the document block or the ledger entry's field it reviewed is recorded, or when the author's `fix` names one; for a finding anchored only to messages, when the author's `fix` names the later message that corrects it (CR-22). The controller queues the re-check for the next review dispatch, when CR-28 decides supersession today. It gives the reviewer the finding, its anchors at the old revision, the same places at the new one, and the author's explanation if there is one, labelled as the author's. It asks for one answer, `resolved` or `unresolved`, with at least one evidence anchor in the new revision or message: a block, a field, a line range of an artifact, or an archive message. Code checks that each anchor exists and lies there. `resolved` makes the finding `fixed`. `unresolved` makes the controller record a successor finding on the new revision, `open`, with the same category and severity, and the old one `superseded` (CR-44). A re-check that cannot decide, because it is capped, fails or gives no answer with a valid anchor, leaves the finding `open` and is recorded as the finding's latest re-check, with its answer (`inconclusive`, and why), so that the card can show "open, last re-check inconclusive" (CR-46). It is tried at the next dispatch, at most 3 times; after that the finding waits for the user or a review that holds it. Re-checks count toward the review's budget (CR-31), not toward its 10 findings (CR-19). A math or figure finding is re-checked by its check, as CR-38 says.
- CR-43. *Decided 2026-10-25 by the owner (B9); not built:* **Dismissals.** The main agent dismisses an open finding with `dismiss` and a reason of at most 2,000 characters, checked as agent text; `dismiss` replaces `rebut`. The dismissal is recorded in the finding's history and shown to the user, and the next review's snapshot holds it among the open findings' share (CR-2). That review accepts or rejects the reason, and its record counts the author's dismissals it rejected. A dismissal stands either way, as a challenged rebuttal does today (CR-26), and the finding is not injected again. For the gate (CR-41), a `fail` finding that the agent alone dismissed stays unresolved until a review accepts the reason or the user dismisses it: setting a finding aside strengthens the work it was raised on, which needs the user or a gate (LG-7's general rule). Today only the user dismisses (`withdrawn`, CR-27); the author's dismissal is new, and changes what is shown and injected, not the gate.
- CR-44. *Decided 2026-10-25 by the owner (B9); not built:* **Superseded and stale.** A finding becomes `superseded` when a later review round of the same work replaces it: an unresolved re-check (CR-42), or a later review of the same work (the same revision of the artifact, block or field, or an overlapping archive span) whose new finding names it as the one it replaces, which the controller checks. The old finding links to its successor, and the successor to it (CR-40). A re-flag of the same finding (CR-20) is not a replacement. A finding becomes `stale` at its session's close when what it reviewed has a newer revision and no re-check of it ran in that session. A finding anchored only to messages never becomes stale. A stale finding is not injected, counts as unresolved for the gate, and is re-checked when a later review's snapshot holds what it reviewed.
- CR-45. *Decided 2026-10-25 by the owner (B9); not built:* **What the controller records.** On each finding: the review that raised it; what it reviewed, as an artifact path and revision, a document block at a revision, an entry handle with its field and revision, or archive message IDs; for each re-check, its review, the new revision, the answer, the evidence anchors and the reviewer run (alias, resolved model, rubric version); for a dismissal, who made it, when and its reason, and the next review's acceptance or rejection; for `superseded`, the successor's handle. Each change is one record of the finding's history, with its `finding.transitioned` event; none is a ledger mutation.
- CR-46. *Decided 2026-10-25 by the owner (B9); not built:* **The workspace's card** (B10) shows the finding's identifier, severity, category and status (`open`, `fixed`, `dismissed`, `superseded` or `stale`), its statement as the reviewer's text, its anchors with links, the revision it reviewed and the current one, the latest re-check's answer with its evidence ("open, last re-check inconclusive" when it could not decide), a dismissal's reason and who gave it, the link to a successor, and the user's controls to set any status with a reason. The B10 backlog item names four status chips; `stale` is a fifth.
- CR-47. *Decided 2026-10-25 by the owner (B9); not built:* **The model, the records and the mapping.** For the author, the review tools keep their shape: the list tool returns findings by the new statuses, `fix` queues a re-check, `dismiss` replaces `rebut`, and the counts and notices of CR-16 and CR-21 count `open` findings. The migration stays within the review store, in one control store step; nothing moves into the ledger. It adds a table of status changes (the finding, the new status, the actor, the time, the reason or answer, and for a re-check its review, revision, evidence anchors and reviewer run), a successor link on the finding (`superseded_by`), and columns for the finding's latest re-check and the author's dismissal. Today's transitions (`review_transitions`) become the history's first rows, with their actors, times and reasons kept. The statuses map:
  - `open` and `claimed fixed` to `open`, with a claimed fix's re-check queued;
  - `confirmed fixed` to `fixed`;
  - `withdrawn` to `dismissed`, by the user or by the review that accepted a rebuttal;
  - `challenged`, `unaddressed` and `inconclusive` to `open`, with their history kept: a `challenged` finding keeps both sides' records and the author's rebuttal as a dismissal that the review rejected, and, like an `unaddressed` one, is at its bound, so it is not injected again and takes no response round (CR-26, CR-30); an `inconclusive` one carries its last re-check marked inconclusive (CR-42);
  - `superseded`, today's replaced-revisions status, to `stale`.

  CR-32's `unaddressed` becomes "`open` at the re-flag bound". Building this moves the reviewer's bundle (`sandbox-reviewer-bundle-2`), the MAIN prompt and the tool pins; the rubric `sandbox-consistency-9` is unchanged.

## Contracts used

- `ArtifactRef`: snapshot contents and evidence references are pinned.
- `Anchor`: transcript entries, execution log ranges, artifact blocks and source pages or regions; transcript and log anchors are proposed additions ([index](README.md#proposed-contract-additions-new)). `math` findings anchor to a math span in a document block, a ledger entry's field or a comment (proposed addition 7).
- `Event`: triggers consume artifact, execution, document and transcript events. Reviews emit `review.dispatched`, `review.ended` (with outcome), `finding.created` and `finding.transitioned`. Findings are the reviewer run's assertions; transitions record who made them. `math` findings and their mechanical transitions are the platform's observations.
- `Mutation`: a finding transition names the status it expects, so concurrent responses conflict instead of overwriting.
- `ExecutionContext`: findings cite attempts by their context; the reviewer starts none.
- `EnvironmentRecord`: the snapshot names the environment revisions of the executions it covers.
- `PermissionRequest`: not used; reviews grant nothing.

## Acceptance fixtures

All fixtures are `scripted`: the author's and the reviewer's model output is scripted, and time comes from the injectable clock.

**CR-F1 Claimed action without a record** (CR-4, CR-11, CR-15, CR-29)
- *Given* a turn in which the author writes `report.md` r1 saying the GF(17) check ran and all assertions passed, without any execution.
- *When* the turn ends.
- *Then* a terminal review is dispatched with r1 and the turn's transcript in its snapshot. The scripted reviewer's `fail` finding is anchored to r1's paragraph and the transcript entry. The author is resumed for a response round, and the turn shows "review pending" until the round ends.

**CR-F2 Value contradicting output** (CR-4, CR-17)
- *Given* an execution whose output records `nonzero_ok = 15`, while r1 states 16.
- *Then* the finding's anchors are the block in r1 and the log line range of that attempt, with pinned evidence; a later r2 does not change what the finding shows.

**CR-F3 Claimed fix confirmed** (CR-22, CR-24, CR-25)
- *When* the author tries to set the finding `confirmed fixed` directly, then responds `fix` with an explanation and r2, and the confirmation review confirms.
- *Then* the direct attempt is refused; the transitions are `open` → `claimed fixed` (author run) → `confirmed fixed` (confirmation review ID); the explanation is visible to the user.

**CR-F4 Rejected fixes reach the bound** (CR-29, CR-30, CR-32)
- *When* the confirmation review rejects two successive fixes.
- *Then* the re-flag count is 2 and the status `unaddressed`; there is no third injection; the turn completes with one unaddressed `fail` in its summary.

**CR-F5 Supersession is not a fix** (CR-27, CR-28)
- *Given* an open finding on r1. *When* the author writes r2 changing the paragraph, without responding.
- *Then* the finding is `superseded` with a link to r2, never `confirmed fixed`. The next snapshot includes r2 and the superseded finding; the reviewer finds the problem persists, and the new finding links to the old one.

**CR-F6 Shell-written files trigger a review** (`worker` as well; CR-9, CR-10)
- *When* the author runs one shell command that writes three files in its workspace.
- *Then* the worker reports three artifact revisions, and a checkpoint review whose snapshot contains them is dispatched once the minimum interval has passed. No file tool was used.

**CR-F7 Interval, merging and watermarks** (CR-10, CR-13, CR-14)
- *When* triggers fire at 0 s (that review takes 60 s), 30 s and 50 s.
- *Then* there are exactly two reviews, the second dispatched at 120 s with the merged window; the two snapshots are contiguous, without gap or overlap.

**CR-F8 The reviewer cannot act** (CR-2)
- *When* the scripted reviewer calls the shell, a kernel, a document write, a web fetch and a delegation.
- *Then* each call is refused with `tool_not_granted` and recorded; the review completes; no execution has the reviewer as its actor.

**CR-F9 Missing evidence is inconclusive** (CR-6)
- *Given* a claim citing a value from a log truncated by the output limit.
- *Then* the finding is `inconclusive`, does not interrupt the author, and appears among unresolved findings.

**CR-F10 Failure and restart** (CR-14, CR-33, CR-34)
- *When* the reviewer alias is unavailable for one review, and the orchestrator restarts during another.
- *Then* the first review ends `failed` and the watermark stays, so the next review covers its window. The second is `dropped`, queued again and completed. A window that fails three times remains pending under paced retries.

**CR-F11 Which turns get a terminal review** (CR-11, CR-12)
- *Given* three turns: conversation only; a turn whose last message claims the task is complete; and a turn the user interrupts after it writes an artifact.
- *Then* the first gets no terminal review; the second gets one, with the classifier's label recorded; the third gets none, and the next review covers its artifact.

**CR-F12 Warnings and roles** (CR-21, CR-23)
- *Given* an open `warn` finding and an open `fail` finding.
- *When* a delegated agent responds `fix` to the `fail` finding, then the main agent does.
- *Then* the `warn` finding was never injected, and the author's list tool returns it; the delegated agent's response is refused and the main agent's accepted.

**CR-F13 New message during a pending review** (CR-16)
- *When* the user sends a message while a terminal review is pending, and the review then reports a `fail` finding.
- *Then* the new turn starts at once, and the finding reaches the author at the first safe point of that turn.

**CR-F14 Math findings** (CR-36 to CR-38; added 2026-10-11)
- *Given* a turn in which the author's patch saves a document revision containing `$\frac{a}{$`, and the user's save adds `$\sqrt{$` to the same document.
- *Then* exactly one `math` finding exists: `warn`, for the author's run, anchored to the span, with KaTeX's error and the suggested remedy `\frac{a}{}`. The user's formula has none. The GUI has shown the span repaired, marked as such, since the save. The finding never interrupted the turn; when the turn ends, the author is resumed for a response round that hands it over with the terminal review's findings.
- *When* the author patches the span to `\frac{a}{b}`.
- *Then* the finding is `confirmed fixed`, recorded as confirmed by the math check, and no confirmation review ran.
- *When*, in a later turn, the author writes another broken formula, and two patches in a row leave it failing.
- *Then* the first re-flags its finding; after the second it is `unaddressed`, and the turn completes without a third round.

**CR-F15 A missing handle is a suggestion** (CR-4, CR-18 as amended 2026-10-16)
- *Given* an answer that declines an approach because of rejected decision D-3 and names no handle.
- *When* the reviewer raises a `handle_citation` finding of any severity.
- *Then* it is stored as `note` with the handle in its remedy; the author's counts and notices do not include it, no confirmation review is queued for it, a `fix` or `rebut` response to it is refused, and a review with no other finding still ends `passed`.

**CR-F16 A finding's lifecycle** (CR-40 to CR-44) *Decided 2026-10-25 by the owner (B9); not built:*
- *Given* three open findings on `report.md` r1: F1 (`fail`) on paragraph 2, F2 (`warn`) on paragraph 4 and F3 (`warn`) on paragraph 6.
- *When* the author writes r2, changing paragraphs 2 and 4, and tries to set F1 `fixed` itself; the next review re-checks F1 and F2, and the re-check of F2 cannot decide at first; a later dispatch tries it again and the reviewer answers `unresolved`, so the controller records F4 on r2; the author dismisses F4 with a reason; r3 changes paragraph 6, and the session closes before another review.
- *Then* the author's direct change is refused. F1 is `fixed`, with the reviewer's `resolved` and an anchor in r2's paragraph 2. Until the second re-check, F2 is `open`, shown as "last re-check inconclusive"; then F2 is `superseded` by F4, and F4 is `dismissed`, its reason shown, and unresolved for the gate. At the close F3 is `stale`: r3 replaced what it reviewed and no re-check of it ran. Every change is a record in the finding's history, attributed to its actor, and the ledger holds none of it.

## Open questions

1. Which review settings does the user's CScience actually run with? The defaults in CR-10 are inspected source defaults. How the two kernel-work values combine, and what counts as a structural block, also need checking against the user's experience.
2. **Settled (2026-10-11):** the terminal review holds the turn's completion (CR-15).
3. **Settled (2026-10-11):** `withdrawn` is a status, for accepted rebuttals and user dismissals (CR-27).
4. **Settled (2026-10-11):** the user may mark a fix `confirmed fixed` directly, and the record shows it (CR-25).
5. **Settled (2026-10-11):** severities are `fail`, `warn` and `note` (CR-18).
6. Should a second reviewer pass validate `fail` findings before they interrupt the author, as Claude Code's public review command validates candidate issues, or keep one pass as CScience does?
7. **Settled (2026-10-11):** 2 response rounds per turn, 2 re-flags and 10 findings per review, as project settings (CR-19, CR-29, CR-30).
8. Should the reviewer also check text the user wrote, or only agent work unless the user asks?

## Sources

[Feature plan](../FEATURE-PLAN.md) (reviews need distinct roles), [product](../PRODUCT.md) (automatic reviews), [execution plan](../EXECUTION-PLAN.md) (automatic review behavior), [implementation plan](../IMPLEMENTATION-PLAN.md) (W4), [external skills](../EXTERNAL-SKILLS.md), [living documents](living-documents.md) and [ledger](ledger.md) (the amendments of 2026-10-11), decisions D04, D25, D27–D29, D35, D56, D82. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md) report (reviewer scope, triggers, findings and confirmation lifecycle), the [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md) report and its pinned `autoReview.ts` and `reviewer.md` (MIT), and the [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md) report. Public: Claude Code's [hooks documentation](https://code.claude.com/docs/en/hooks) (stop hooks and their loop bound).
