# Automatic consistency review

Date: 2026-10-11. **Status: Approved as drafted (2026-10-11, by the user).** Open questions 2–5 and 7 are settled as proposed. The CR-10 defaults stand until the user's own CScience settings are known (question 1); questions 6 and 8 remain open. W0 behavior specification; conventions in the [index](README.md).

## Scope

The automatic consistency review checks an agent's claims against recorded evidence while the agent works and when it finishes, and follows each finding through the author's response to confirmation or another outcome. It behaves like CScience's review (D35, confirmed): checkpoint and terminal scheduling, a constrained reviewer, findings with severity, author responses and claimed-fix confirmation.

Out of scope: mathematical verification and reproduction, a separate role that may run code (D27, proposed extension); action and security monitoring ([monitoring](../MONITORING.md)); the rubric and prompts, written independently when W4 builds this (D56); promotion of results into the ledger ([research workspace](../RESEARCH-WORKSPACE.md)).

Terms:

- **Author**: the session's main agent. Work by its delegated agents counts as the session's work.
- **Review**: one reviewer run over one evidence snapshot, of type `checkpoint`, `terminal` or `confirmation`.
- **Evidence snapshot**: the pinned transcript entries, executions, artifact revisions and findings a review may read.
- **Watermark**: the position in the session's transcript and events up to which its work has been reviewed.
- **Finding**: one problem the reviewer reports, with its own lifecycle.

## Behavior

### Reviewer

- CR-1. The reviewer is a separate agent role with its own model settings ([plans and delegation](plans-and-delegation.md), PD-27). It traces claims to evidence and does not recompute (D27; CScience).
- CR-2. Its tools read the snapshot (transcript entries, execution records and logs, artifact revisions with their extracted text and existing renders, ledger entries, open findings) and record findings. It MUST NOT have shell or kernel access, package installation, writes to files, documents, plans or the ledger, web or MCP access, delegation or permission decisions (CScience's reviewer likewise has no execution, editing, installs, web search or delegation). The harness enforces this with tool grants, not prompt text (new; OpenScience Desktop's reviewer is read-only only by instruction). A reviewer with execution would be the verification role, with its own review type and records (D27).
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
  - at least 3 new artifact revisions;
  - at least 2,000 characters of agent-written durable text (documents, reports, ledger entries, plan text);
  - a new structural block in a durable document (proposed meaning: a section, table, figure, theorem or claim);
  - kernel work: 40 cells, with a 30-minute interval (the inspected source pairs these two values; how they combine needs checking);
  - the user asks for a review.

  The minimum interval is 120 seconds. All values are project settings; the defaults are the inspected CScience source defaults, pending the user's actual settings.
- CR-11. A terminal review is due when the main agent ends a turn that produced artifacts or executions with effects, left findings `open` or `claimed fixed`, or ends by claiming the work is complete. A classifier labels the final message, and its label and version are recorded (CScience). Other turns get no terminal review.
- CR-12. Reviewer runs never trigger reviews. A turn the user interrupted, or that failed, gets no terminal review; its work stays after the watermark for the next review (OpenScience Desktop skips such turns, and Claude Code's stop hooks do not fire on interrupts).

### Dispatch

- CR-13. Checkpoint reviews run detached while the author keeps working. A session has at most one review running; triggers that fire meanwhile merge into one pending window (CScience).
- CR-14. A review's snapshot covers the work from the watermark to its dispatch. A completed review advances the watermark to the end of its snapshot; a failed or dropped review does not, so the next review covers the same work (CScience).
- CR-15. A terminal review holds the turn's completion: the user sees the final message marked "review pending" (new). If a checkpoint review is running, the terminal review starts after it and absorbs the pending window.
- CR-16. If the user sends a message while a terminal review is pending, the new turn starts at once. The review continues, and its findings reach the author at the first safe point of the new turn (new).

### Findings

- CR-17. A finding records: ID; review ID and type; the reviewer run (alias, resolved model, rubric version); severity; category (CR-4); a statement in Markdown with LaTeX (D25); one or more anchors (transcript entry, execution log range, artifact revision and block, source page or region); pinned evidence references; an optional suggested remedy; status; and every transition with actor, time and reason.
- CR-18. Severity is `fail` for a materially false or unsupported durable claim or a claimed action without a record, `warn` for a claim that needs correction or qualification, and `note` for information (new names; CScience separates fail-level findings from warnings).
- CR-19. A review reports at most 10 findings. With more candidates it reports the most severe and records that the cap was reached (new; OpenScience Desktop caps at 8).
- CR-20. The reviewer sees the open findings and re-flags one instead of duplicating it. A new finding with the same category and anchors as an open one is turned into a re-flag (new).
- CR-21. `fail` findings from a checkpoint review interrupt the author at the next safe point: after the current tool call, before the next model request. `warn` and `note` findings are not injected; the author is told how many exist and lists them with a tool. The user sees every finding at once (CScience).

### Response and confirmation

- CR-22. The author or the user responds to a finding with `fix`, which requires an explanation and references to the changed revisions or messages, or with `rebut`, an explanation with evidence. The explanation is shown to the user (CScience).
- CR-23. Only the main agent and the user can respond. A delegated agent can propose a fix in its outcome, which the main agent then claims (new; CScience likewise keeps finding transitions with the root session).
- CR-24. A `fix` moves the finding to `claimed fixed` and queues a confirmation review. The reviewer checks the finding, its evidence, the explanation and the referenced revisions, and confirms, rejects or cannot decide (CScience).
- CR-25. Only a confirmation review or the user can mark a finding `confirmed fixed`, and the record shows which (user confirmation is new). The author MUST NOT be able to.
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
- CR-28. Supersession is not a fix. When every artifact revision a finding is anchored to has a newer revision and no fix is claimed, the finding becomes `superseded`, linked to the new revisions, and the next review checks whether the problem persists in them; if it does, the new finding links to the old one. A finding anchored only to transcript entries is never superseded (new; CScience closes such findings automatically, which D28 asks to distinguish from confirmation).

### Bounded completion

- CR-29. After a terminal review with `fail` findings, the author is resumed for a response round, followed by confirmation of claimed fixes; at most 2 rounds per turn (new defaults; CScience bounds its loop, and Claude Code caps consecutive stop-hook continuations).
- CR-30. A finding is re-flagged to the author at most twice; then it becomes `unaddressed` instead of being injected again (CScience keeps a re-flag count).
- CR-31. Each review has a turn, time and cost budget. A review that reaches it ends `capped` and keeps the findings it recorded.
- CR-32. When the bound is reached, `open` findings become `unaddressed`, `claimed fixed` findings stay unconfirmed, and the turn completes with a summary of findings by status and severity.

### Review outcomes, recovery and configuration

- CR-33. A review ends `passed` (no findings), `findings`, `inconclusive` (the main claims could not be evaluated), `capped`, `failed` (reviewer error, or invalid output after one retry) or `dropped` (lost to a restart). Only `passed` reads as a pass (feature plan).
- CR-34. An orchestrator restart drops the reviews in progress. A review that fails or is dropped is dispatched again with its window, at most twice; after the third failure the window is listed in the session as unreviewed and the watermark moves past it (CScience reconciles dropped reviews with bounded recovery).
- CR-35. Automatic review is on by default (D04, D35). Per project: thresholds, minimum interval, the hold of CR-15, bounds and the severity that interrupts. Each review records the configuration revision it ran under ([product](../PRODUCT.md): record the active configuration).

## Contracts used

- `ArtifactRef`: snapshot contents and evidence references are pinned.
- `Anchor`: transcript entries, execution log ranges, artifact blocks and source pages or regions; transcript and log anchors are proposed additions ([index](README.md#proposed-contract-additions-new)).
- `Event`: triggers consume artifact, execution, document and transcript events. Reviews emit `review.dispatched`, `review.ended` (with outcome), `finding.created` and `finding.transitioned`. Findings are the reviewer run's assertions; transitions record who made them.
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

[Feature plan](../FEATURE-PLAN.md) (reviews need distinct roles), [product](../PRODUCT.md) (automatic reviews), [execution plan](../EXECUTION-PLAN.md) (automatic review behavior), [implementation plan](../IMPLEMENTATION-PLAN.md) (W4), [external skills](../EXTERNAL-SKILLS.md), decisions D04, D25, D27–D29, D35, D56. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md) report (reviewer scope, triggers, findings and confirmation lifecycle), the [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md) report and its pinned `autoReview.ts` and `reviewer.md` (MIT), and the [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md) report. Public: Claude Code's [hooks documentation](https://code.claude.com/docs/en/hooks) (stop hooks and their loop bound).
