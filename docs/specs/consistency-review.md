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
- CR-2. Its tools read the snapshot (transcript entries, execution records and logs, artifact revisions with their extracted text and existing renders, including those its ledger entries' evidence pins (CR-14), ledger entries, open findings) and record findings. It MUST NOT have shell or kernel access, package installation, writes to files, documents, plans or the ledger, web or MCP access, delegation or permission decisions (CScience's reviewer likewise has no execution, editing, installs, web search or delegation). The harness enforces this with tool grants, not prompt text (new; OpenScience Desktop's reviewer is read-only only by instruction). A reviewer with execution would be the verification role, with its own review type and records (D27).
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
- CR-14. A review's snapshot covers the work from the watermark to its dispatch. A completed review advances the watermark to the end of its snapshot; a failed or dropped review does not, so the next review covers the same work (CScience). A snapshot that holds a ledger entry also holds the artifact revisions the entry's evidence pins, even ones written before its window and by another session of the project: ledger pins are project-wide by design, so a reviewer in one session may read an artifact another session wrote when a project entry pins it (amended by the coordinator 2026-10-15 for lab 39's B6, which the owner can reverse).
- CR-15. A terminal review holds the turn's completion: the user sees the final message marked "review pending" (new). If a checkpoint review is running, the terminal review starts after it and absorbs the pending window.
- CR-16. If the user sends a message while a terminal review is pending, the new turn starts at once. The review continues, and its findings reach the author at the first safe point of the new turn (new).

### Findings

- CR-17. A finding records: ID; review ID and type; the reviewer run (alias, resolved model, rubric version), or for a `math` finding the math check; severity; category (CR-4, or `math`, CR-36; *amended 2026-10-11*); a statement in Markdown with LaTeX (D25); one or more anchors (transcript entry, execution log range, artifact revision and block, source page or region); pinned evidence references; an optional suggested remedy; status; and every transition with actor, time and reason.
- CR-18. Severity is `fail` for a materially false or unsupported durable claim or a claimed action without a record, `warn` for a claim that needs correction or qualification, and `note` for information (new names; CScience separates fail-level findings from warnings).
- CR-19. A review reports at most 10 findings. With more candidates it reports the most severe and records that the cap was reached (new; OpenScience Desktop caps at 8).
- CR-20. The reviewer sees the open findings and re-flags one instead of duplicating it. A new finding with the same category and anchors as an open one is turned into a re-flag (new).
- CR-21. `fail` findings from a checkpoint review interrupt the author at the next safe point: after the current tool call, before the next model request. `warn` and `note` findings are not injected; the author is told how many exist and lists them with a tool. The user sees every finding at once (CScience).

### Response and confirmation

- CR-22. The author or the user responds to a finding with `fix`, which requires an explanation and references to the changed revisions or messages, or with `rebut`, an explanation with evidence. The explanation is shown to the user (CScience).
- CR-23. Only the main agent and the user can respond. A delegated agent can propose a fix in its outcome, which the main agent then claims (new; CScience likewise keeps finding transitions with the root session).
- CR-24. A `fix` moves the finding to `claimed fixed` and queues a confirmation review. The reviewer checks the finding, its evidence, the explanation and the referenced revisions, and confirms, rejects or cannot decide (CScience).
- CR-25. Only a confirmation review or the user can mark a finding `confirmed fixed`, and the record shows which (user confirmation is new). The author MUST NOT be able to. *Amended 2026-10-11: for a `math` finding, the math check does it too, and the record says so (CR-38).*
- CR-26. The next review evaluates a rebuttal. If it accepts it, the finding becomes `withdrawn`; if not, the finding stays `open` with the reviewer's reason and one more re-flag (new).

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

  Only `confirmed fixed` and `withdrawn` are resolved; the rest stay visible as unresolved. The user, or a later review with new evidence, can re-open `unaddressed`, `superseded` and `inconclusive` findings, and the author can still fix `open` and `unaddressed` ones in a later turn.

  *Amended 2026-10-11: for `math` findings, the math check sets `open` and `confirmed fixed` (CR-36, CR-38), and they are never `superseded`.*
- CR-28. Supersession is not a fix. When every artifact revision a finding is anchored to has a newer revision and no fix is claimed, the finding becomes `superseded`, linked to the new revisions, and the next review checks whether the problem persists in them; if it does, the new finding links to the old one. A finding anchored only to transcript entries is never superseded (new; CScience closes such findings automatically, which D28 asks to distinguish from confirmation). *Amended 2026-10-11: an anchor on a block of a living document, or on a field of a ledger entry, counts as replaced only when a later revision changes or removes that block or field, not on every new revision; supersession is decided when the next review is dispatched, so that an author can change the block and then claim the fix ([living documents](living-documents.md), LD-25; [ledger](ledger.md), LG-25).*

### Bounded completion

- CR-29. After a terminal review with `fail` findings, the author is resumed for a response round, followed by confirmation of claimed fixes; at most 2 rounds per turn (new defaults; CScience bounds its loop, and Claude Code caps consecutive stop-hook continuations). *Amended 2026-10-11: open `math` findings for the turn's work also start a response round, which hands them over together with the terminal review's findings (CR-37).*
- CR-30. A finding is re-flagged to the author at most twice; then it becomes `unaddressed` instead of being injected again (CScience keeps a re-flag count).
- CR-31. Each review has a turn, time and cost budget. A review that reaches it ends `capped` and keeps the findings it recorded.
- CR-32. When the bound is reached, `open` findings become `unaddressed`, `claimed fixed` findings stay unconfirmed, and the turn completes with a summary of findings by status and severity.

### Review outcomes, recovery and configuration

- CR-33. A review ends `passed` (no findings), `findings`, `inconclusive` (the main claims could not be evaluated), `capped`, `failed` (reviewer error, or invalid output after one retry) or `dropped` (lost to a restart). Only `passed` reads as a pass (feature plan).
- CR-34. An orchestrator restart drops the reviews in progress. A review that fails or is dropped is dispatched again with its window, at most twice; after the third failure the window is listed in the session as unreviewed and the watermark moves past it (CScience reconciles dropped reviews with bounded recovery).
- CR-35. Automatic review is on by default (D04, D35). Per project: thresholds, minimum interval, the hold of CR-15, bounds and the severity that interrupts. Each review records the configuration revision it ran under ([product](../PRODUCT.md): record the active configuration).

### Math findings

*Added 2026-10-11 (D82).* A formula that KaTeX rejects goes back to the agent that wrote it as a finding, while the GUI shows its automatic repair meanwhile. These findings come from a mechanical check, not from a reviewer; they share the findings' records and lifecycle, with the differences below.

- CR-36. When durable text an agent wrote is saved, the platform checks its math with the GUI's KaTeX ([living documents](living-documents.md), LD-53, LD-54). A span KaTeX rejects becomes a finding of category `math` and severity `warn`, anchored to the span, with KaTeX's error, and with the repair the display found as its suggested remedy (CR-17). It is addressed to the agent run that last changed the block holding the span, and the session's main agent answers it (CR-23). Text the user wrote, a reviewer's own statements and chat messages get the display repair only. The check is the platform's observation and needs no model.
- CR-37. Math findings never interrupt the author (CR-21). The platform collects them during the turn and hands them to the author in the turn's closing response round, together with any findings of its terminal review (CR-15, CR-29). Math in text saved outside a turn reaches the session's main agent at its next turn.
- CR-38. The author fixes the source with an ordinary patch; no `fix` response is needed. The check runs again on each new revision of the block holding the span:
  - if KaTeX accepts the span, the finding becomes `confirmed fixed`, recorded as confirmed by the math check;
  - if the span still fails, the finding is re-flagged, and after two re-flags it becomes `unaddressed` (CR-30), so the loop is bounded;
  - if the formula is gone, the finding is resolved as `confirmed fixed`, and its history notes that the formula was removed rather than repaired.

  A `math` finding is never `superseded` (CR-28): the check decides instead.

### Copied numbers

*Added 2026-10-15 by the coordinator for lab 39's B6, which the owner can reverse.* A long number an agent copies by hand from a computation's output into a document or the ledger goes wrong by a few digits, and a model reviewing both rarely sees it.

- CR-39. Before the model reviews a snapshot, the platform compares each number of at least 10 digits that its ledger entries (statement and fields) and document blocks state, read through the separators that group digits, with the numbers its text and JSON artifacts hold, including those its entries pin (CR-14). A number that matches none exactly, but is within 3 edits, or 5 % of its digits if more, of one of them, becomes a `fail` finding of category `contradicting_value`, recorded by the review and anchored to the entry's field or the block and to the artifact, with both values and the artifact's key; a number that differs from one of them only in its last two digits, such as p − 1 beside p, is taken as a number of its own. The check needs no model, records at most 4 findings in a review, says that they are its own, and is named to the reviewer, which does not record them again. The findings then follow the review's findings' lifecycle.

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
- *Then* the first review ends `failed` and the watermark stays, so the next review covers its window. The second is `dropped`, queued again and completed. A window that fails three times is listed as unreviewed.

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
