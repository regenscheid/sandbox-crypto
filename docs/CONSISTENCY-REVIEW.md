# Automatic consistency review

This page describes the automatic consistency reviewer as built in `packages/orchestrator`
(`sandbox_science.orchestrator.consistency`). It implements the approved
[behavior specification](specs/consistency-review.md), with a separately authored,
versioned rubric (`sandbox-consistency-15`, D56). The reviewer finds the record of each action the author narrates (in a final answer, a thread reply or a finding response: changed, wrote, recorded, published, ran or fixed): a refused or failed tool call is no record; a narrated action with none is a `fail` of category `missing_action` anchored to the narration, and one whose record says otherwise is `contradicting_value` (CR-4, CR-18). Every review's snapshot holds the user's request as a `request` item, context that is never under review and counts toward no coverage (CR-48, `snapshot_reads.request_item`): the session's messages the user wrote, refused ones aside, and the user's answers to the agent's questions (`answer_to`: the question, its options with the one the agent recommended, what the user picked or wrote or that the user left it to the agent, and when), oldest first, within `REQUEST_BYTES` (32 KiB), the newest kept whole first and the first message always, cut short if it must be, with how many entries (`omitted`) and answers (`omitted_answers`) were left out; the rubric makes a picked option the user's instruction, with the detail it carried, and has the reviewer raise no finding that the user did not ask for or approve something when entries were left out; a session with no chat messages gives its first transcript message of role `user`, the task a run started with. A statement of the request that the session's evidence contradicts is named, with that evidence, in the final answer and the report, in words that quote it (a handle alone does not name it); one that is not is a `warn` of category `contradicting_value`, anchored to the statement and the evidence. A check the author calls independent, verified or recomputed needs a record that it computed its own values from its own inputs, ran as a check delegation or in an environment of its own, and was read before it was relayed: a task's acceptance names the files it was accepted with unread (`delegations.review_reads`, PD-46), in the window's actions item when the window holds the `accept_delegation` call (`accepted delegation …, accepted unread: …` or `its files read whole`) and, for every final review, in an `acceptances` item read from the durable record at dispatch, so that an acceptance in an earlier window or run is seen too (at most `MAX_ACCEPTANCES`, latest first, context); a result the answer relies on from a task accepted unread is at least a `warn` unless the answer says the work was not inspected; a comparison of a value with a copy of itself checks nothing; such a claim without the record is `evidence_strength`, `warn` in prose and `fail` in an artifact, document or entry. When the record cannot decide, the reviewer raises a `note` finding with assessment `missing_evidence`. To match against, each window's snapshot holds one `actions` item (CR-2 as amended for labs 40 and 42): the toolbox names each call's effect class in its record, the review controller records each call of any class but `read` (an effect, a plan change, a memory proposal or a cancel) as an `action` observation that makes no review due (the tool, its target as its arguments name it, its outcome, `ok`, `refused` when a gate, a policy or the store turned it down, or `error`, and what it made: a document or ledger revision, a work record, an artifact revision, an execution, files written, a plan's version with the step and status or the plan's status it recorded, a memory proposal), and the item lists them by run, in order, as many as 500 within half an item, with the blocks each document revision in the window changed since the revision before (document items carry them too, as `blocks`). Actions are not items one by one. A terminal review's item also lists, under `earlier`, the run's actions and its document revisions' changed blocks in its windows reviewed before, from the run's first observation to the watermark, as context for an answer that says what the run did earlier (lab 42's B5): its own and those of its delegated runs brought for its review, not another run's; the latest 500 of each, and the oldest left out first when they do not fit, before any of the window's own. The rubric has the reviewer raise no finding on an earlier action itself. Every snapshot of a session with a plan holds a `plan` item, as context: the plan as recorded in the read that fixes the review's window, with its status, goal outcome and each step's title, track and latest recorded status, by whom and when (lab 42's B2: a run that finished its plan and said so drew `fail` findings when neither held its plan changes). A delegated run has no review of its own, so its
parent's does (lab 41's B5): at each of the parent's safe points, each delegation
below it (to three levels) that has ended and whose run record is kept brings into the
parent's window, once, its outcome as a `delegation` item, the delegated run's actions
read from its run record (as many as 200, each naming its delegation), its executions
with their output from the same records (lab 42's B3), and the result files its outcome
pinned (as many as 8, copied whole as ledger pins are, `delegated` in the locator);
none makes a review due. Each execution is one item for each state its calls saw it
in, bounded as the run's own are, naming the run, the delegation and the run its work
was brought for (`for_run`). A busy run has more than a review holds: as many as 100
come in, within 2 MiB. First, for each number the outcome's result and report state
(as `numbers.py` reads numbers of 10 or more digits, at most 64) and its brief does not,
so that an input every call prints matches none, the latest execution whose output
prints it, or, when none does, the latest that prints one within a hand-copying error
of it; then the latest. An output's numbers are matched as a set, exact matches first,
and a delegation's choice makes at most 2,000 near comparisons, so it cannot stall the
parent's safe point. The outcome says how many it holds and how many were left out
(`executions`). An execution whose stdout or stderr was cut short brings the file the
harness saved its whole output to, when the run recorded it with its content, copied
whole as a ledger pin is (at most 64 KiB of text, as many as 8 a run), its locator
naming `output_of`. A delegation's observations are written in one change, the outcome
first, and a review window never splits them: one that would overflow ends before the
outcome, and the next takes the batch whole (a batch a window begins with comes whole),
so a review that holds a delegated execution holds its outcome. The rubric has the
reviewer take an outcome's summary, verdict and report as that run's claims and check
each value it states, or the author relays from it, against an execution that states
the same quantity, the latest successful one when several do; a value missing from an
output cut short, or from executions left out, is unverified, never contradicted. It
never executes code. The planning
consistency review of 2026-10-10 is kept, as history, in
[.agents/planning-consistency-review-2026-10-10.md](../.agents/planning-consistency-review-2026-10-10.md).

The control application keeps the reviewer's tables ([below](#tables)) in the existing
`control.sqlite3` ([living documents](LIVING-DOCUMENTS.md#the-control-store)).
Observations, snapshots, evidence grants, tool reads, model usage, checked items,
findings and their transitions are durable. A fenced lease permits one live reviewer
per session across harness processes. Recovery drops only expired owners. A stale owner
cannot read through a reviewer grant, spend budget, raise a finding or finalize a review.

## Running and scheduling

With `--state-dir`, the harness observes its transcript, canonical execution projections
(one item for each state and reading a tool call saw an execution in, a `wait_jobs` call's
ended jobs included, whose record keeps their whole executions for the review, as
`job_status` does: lab 41's B7; two readings of one state that differ are two items, the
same reading again is one),
agent-authored document revisions, the session's agents' ledger revisions and the flags
their changes raise or clear ([ledger](LEDGER.md))
and durable explicit-publication artifact events. A run's transcript starts after its view
of the session, which its prompt begins with ([context management](specs/context-management.md),
CM-29): each earlier run's messages are in that run's own window, and an earlier answer is
not this run's narration (lab 41). The calls of earlier runs whose results the view holds,
read from the archive by the IDs their headers name, are listed with each review of the
run's work under `earlier_runs` in its `actions` item, each naming its run, as context
(CR-2 as amended; rubric `sandbox-consistency-6`). When the run's messages are archived,
each transcript item names the archived message it copies (`archive`: its ID and
position; a platform message by its header's ID, the model's answers by their order, each
checked by its calls' IDs or its text, and none placed from the first that differs),
and a review's `review.dispatched` event names the first and last positions its
transcript items copy (CR-14 as amended); a delegated run's actions and executions name
the archived tool result each call came back as, paired while tool and arguments agree. An
earlier run's true action can still draw a false `missing_action` when its result was left
out of the view, the run predates the archive, or it was never reviewed: a false fail,
never a false pass. The reviewer uses
`--reviewer-model ALIAS` with the existing model gateway, or `--reviewer-script FILE`
plus that alias for synthetic validation. The alias may be MAIN's: the reviewer still has
its own calls, limits, budget and records. The alias is independently selectable in
`[model].reviewer`; for example, a deployment whose LiteLLM gateway serves
`arrcodex-gpt-6-luna` may choose that alias for routine reviews while MAIN uses a
different model. Compare the alias against known inconsistency fixtures and actual
review samples before switching consequential reviews to it. The adapter limits each
answer to 4,096 tokens and 256 KiB before the reviewer applies stricter response bounds.
With no reviewer alias,
reviews are off (CR-35 as amended for lab 43's B6): none is dispatched or recorded as
failed, a terminal summary says `off`, and the session's review list says so, from a
`review.off` event recorded once until a review is dispatched again. An alias whose model
is unavailable makes zero reviewer model calls; its required window remains pending
under a paced retry after repeated failures. Neither becomes a pass.

**What the observation cannot copy (CR-50).** Recording review evidence never ends a run.
An observation the feed refuses (such as a second, different reading under one key), or an
ingestion that fails (documents, ledger, artifacts, delegated work), is logged, and the
item is a gap in the review's evidence: an `unobserved` feed row in its place
(`ReviewController._gap`, keyed `unobserved:<digest>`), with nothing to read
(`omitted`: `not observed: <why>`, the original item's kind and key), which the next
review is given and names unread; a failed ingestion also leaves its coverage incomplete.
A gap the feed refuses too is only logged. A review that cannot start, or a reviewer task
that ended in an error, is logged at the safe point and its window waits for the next.

**Limits (SNOVA follow-up).** A new author-work review takes a contiguous feed prefix of at most 16 new evidence items and approximately 256 KiB by default, with the existing 256-item/8-MiB snapshot ceiling still bounding finding groups and derived context. It also carries up to eight recent evidence items from before the watermark, within 128 KiB; prior ledger claims are carried as non-promotable context and oversized prior evidence is represented by a locator marker, so a claim and observation on opposite sides of a window boundary can be compared. One oversized first new item is admitted so the cursor cannot stall. A clean prefix can pass its own span, but its `coverage_complete` remains false while later feed rows are pending, so ledger promotion cannot use it. A partial delegated batch remains incomplete. A window read is `passed` or `findings` though an item in it was cut short, held nothing to read (an artifact whose text could not be copied, an observation left out for its size, or one not observed, CR-50) or backlog waits before it (CR-49 as amended): its coverage names the items cut short or unread (`partial`) and, of these, those with nothing to read (`unread`, `ReviewStore._unread`), and its `coverage_complete` stays false, so the ledger gate (LG-17) credits no entry revision the review held, whichever artifact that entry rests on. `inconclusive` is for a review that could not do its job: one that could not decide, a window whose new items all held nothing to read, or a re-check given an item with nothing to read. The session's coverage is the union of the ranges its reviews covered with a verdict other than `inconclusive` (the coverage record's `from` to `through`; a record from before names no `from` and covers from its window's start): complete when the union leaves no row of the session's feed out (`ReviewStore.coverage`). The session's review list carries it as `coverage`: the position reviewed up to (the watermark), the feed's end, whether it is complete and the first 16 ranges left out. Positions up to `not_recorded_through` were reviewed by reviews from before coverage records: their coverage is not recorded, which the summary says, and they are neither complete nor unreviewed.

**Keeping up (CR-49).** A checkpoint whose backlog after the watermark is larger than its configured window (`max_feed_items`, 16 by default) is given a grown window (`window_config`): up to `WINDOW_GROWTH` (2) times the configured one, 32 items by default, with its calls, time, context per call, bytes and any token cap scaled in proportion, each within its ceiling (`REVIEW_CALLS_CEILING` 48 calls, `REVIEW_SECONDS_CEILING` 600 seconds, `REVIEW_CHARS_CEILING` 500,000 characters); the settings it ran with are its run record's. A window waiting for its retry is not grown: it shrinks as before. A backlog that fills a grown window makes a checkpoint due at once, before the minimum interval and whatever changed, so piled-up work is reviewed back to back. Only a checkpoint whose window starts with more than `BACKLOG_WINDOWS` (8) grown windows of new items after the watermark, and has no failed window to retry, skips: it is given the newest grown window, its dispatch moves the watermark past the rest and records that range as `review.unreviewed` with reason `backlog`, and the final review picks the range up if its budget allows. The user's messages and answers are never lost to a skip: every snapshot holds them in its request item.

The deletion of a working file, one whose every revision a run's commands reported (`execution_record`), never published, declared a result or named by a ledger entry's evidence, is nothing to read: its artifact item says so (`WORKING_FILE`) and is complete, so it names no unread item and leaves no gap. A file published or cited and deleted later stays unread, with why.

The session's coverage gives each unreviewed range with its new items (`items`) and the reasons the reviews recorded for it (`reasons`: `backlog`, `final_budget`, `deadline`, `final_inconclusive` and the like, or `not_reached`), and the session's new items as counts (`items`: `total`, `reviewed`, `unreviewed`, and `not_recorded` for those from before coverage records): the position reviewed up to is the watermark, no measure of coverage.

**Prompt cache.** Each review's calls ask the gateway for a prompt cache of their own: the Responses adapter sends the review's key (`review_cache_key`, a digest of the project, session and review), the same for every call of the review and different from another review's and from the session's. A review's calls each send the conversation the one before sent, unchanged, and more (the rubric bundle, then the opening message with the review and its budget, then the calls and their results, and the budget notices last), so each reuses the cached prefix.

**Budget and cursor (built 2026-11-03).** The reviewer knows its budget: the rubric (`sandbox-consistency-15`) and the first message (`budget.calls`) state its calls, and before each of its last two calls a user message, the notice `sandbox-review-budget-1`, gives the calls left, this one included, and the limit that sets them (`calls`, `calls_per_hour` or `tokens` as the store counts them, or `context` or `time`, estimated from the largest turn and the slowest call so far); each notice is a `budget` record of the review. The reviewer is then to dig no further: record what it checked and its findings, and give its verdict on what it covered, naming in `unreviewed` each new item it did not examine. The manifest marks the window's new items (`new`), the feed rows after the watermark the snapshot copies; carried evidence, findings, the plan and the actions item are not new. A review whose announced last call makes tool calls runs them and ends `capped`. When a review ends, the store records its `coverage` (on the run record, and the feed position in `review.ended`): the feed position it covered through, the new items covered, those unreviewed and those examined. A verdict covers the window up to the first item it names; a verdict after a notice covers only the new items examined (read, or named by a call that succeeded: a checked claim's evidence, a finding's or decision's anchors, a viewed image, a citation's source), up to the first that was not. A review without a verdict covers nothing, whatever it read. The watermark moves to the end of what was covered, and the next review continues from there. A review with a verdict keeps its `coverage_complete` for the items it covered: the ledger gate credits the entries they hold and leaves out those its coverage names unreviewed, which the next review covers and credits. A review that covered nothing, or ended without a verdict (capped, failed or dropped), has `coverage_complete` false and credits no entry. The findings cap bounds what a review reports, not what its verdict covers. A review without a verdict, or whose verdict covered nothing, keeps its exact window, so its work stays pending until a review with a verdict covers it: the next attempt takes at most one quarter as many new items/bytes; after three failures its watermark stays put and further attempts are admitted at least five minutes after the last failure. A later safe point or terminal boundary must trigger that retry; idle timer dispatch is still planned. This prevents an unavailable reviewer from repeatedly buying calls and silently skipping evidence. Each review of a configured window still has at most 24 calls, 300 seconds and 300,000 characters of context per call, and no token cap unless the operator sets one; a grown window has them scaled (Keeping up); the project reviewer has at most 240 calls per hour, which a run's final review is outside (CR-48). A review records its prompt's provenance once, with its first call (a later call's `call_provenance` reads that one's; a review whose calls hold none is legacy), and the profile its model was asked for once, as a `profile` record; a reviewer call writes no `model_profile` notice into the reviewed run's event log, and its usage counts under the reviewer's role from the model-call journal as before. The budget counts provider usage when reported and otherwise a conservative estimate. The run service accepts `--review-token-budget`, `--review-calls-per-hour` and `--final-review`, forwarded from `[model]`. A manifest page lists up to 50 items. Of one turn's tool calls, the first 16 run and the rest are answered `not_run`. The GUI shows calls, tokens, cap reason, incomplete coverage and unreviewed spans. When hourly calls are exhausted during the run, work remains pending until the cap permits another dispatch. Contradictions farther apart than the carried evidence still need a later indexed aggregate review.

Checkpoints start at live safe points after the configured minimum interval and document,
artifact, kernel-cell or manual-request thresholds, counted over what the feed gained since
the session's latest checkpoint or terminal review was dispatched (its `review.dispatched`
event records the feed's `head`): 3 artifact revisions, 2,000 agent-written durable
characters, one structural block, or 40 kernel cells once 30 minutes have passed, after
120 seconds. A backlog an earlier review left after the watermark makes none due by itself
unless it fills a grown window, which is due at once; the next review continues from the
watermark in a window grown to its backlog, or skips a backlog of more than eight grown
windows to the newest (CR-49), and the run's final review takes up what is left as its
budget allows. A request, and a window whose review covered nothing, still make one due;
a re-check moves nothing. The terminal rule classifier records
its version, completion-claim match, durable run effects, open/claimed-fixed finding
states and document/ledger/artifact ingestion coverage. A run's durable effects
(`ReviewStore.run_effects`) are its observations with effects, the calls it made that
changed something (its `action` rows: a delegation, an acceptance, a ledger, document or
plan change) and its delegated runs' work observed for it (`for_run`), so a run that only
delegated is reviewed. Ordinary conversation without those triggers
skips terminal review, and the run's summary says so (`no_final_review`, `NOTHING_TO_REVIEW`),
as it does when the project's reviews are disabled (`REVIEWS_DISABLED`) or the deployment's
end does not wait (`NOT_WAITED`); failed/interrupted author loops do not run a terminal
reviewer.

**The final review (CR-48).** When the main run gives its final answer, terminal
ingestion drains through a pinned document-event head, and a ledger-event head, in pages
of 200, at most 10,000 events per boundary, and marks remaining backlog incomplete. A
page's observations and its cursor commit in one change, as the artifact pages' do, and
the session is checked once per call, not per item. A page that fails (an observation the
store refuses) stops that ingestion as a whole: no later page is read, and the next
boundary starts again at the same cursor. The whole wait at a run's end is bounded by
`END_WAIT_SECONDS` (540 s: the final review's 480 s and a minute). A checkpoint still
running when a final review is to follow is ended at once (`SUPERSEDED`), as a deadline
ends one: its findings are kept and its window, recorded unreviewed, is the final review's
to cover with the newest work, rather than waited for and then discarded. At the end of
a conversation, which has no final review, a running checkpoint is waited for within that
minute, and ended at its deadline if it has not ended. Then the controller makes one final review, one a run (`dispatch(..., final=True)`, kind `terminal`, its
classifier marked `final`), never a walk through the backlog: it is given what no review
covered with a verdict since the run began, or since the watermark if that is earlier,
newest first (`snapshot_reads.final_items`), then the ranges the session's reviews
recorded unreviewed (`review.unreviewed` of any reason), as its budget allows, at most 48 items and about 1 MiB
(`FINAL_FEED_ITEMS`, `FINAL_FEED_BYTES`), with the run's actions item; the files the final
answer names by path and the ledger entries it names by handle (at most
`FINAL_CITED_ENTRIES`), at their latest revisions, as `artifact` and `entry` items naming
`cited_by`, context like an entry's pins; the plan; and the open findings, which take
their share first. Its settings are the project's with its own budget (`final_config`):
30 calls, 1,500,000 tokens or the operator's lower cap, and 480 seconds; the hourly calls
neither defer it nor refuse its calls, which still count toward that hour. Its dispatch
records each range its budget left out, and not recorded already, as `review.unreviewed`
with reason `final_budget`; its coverage runs newest first, back to the first item it did
not cover, the actions and raw sources between included, and its end records the rest of
its window with the same reason. A final review that ends without covering anything leaves
no retry window: its window is recorded unreviewed (reason `final_<outcome>`). A review the
end's wait outlasts, or `hold`'s limit, is ended at its deadline
(`ReviewStore.finish(deadline=True)`): `capped`, its findings kept, its window recorded
unreviewed with reason `deadline` and the watermark past it, no retry, and its task
stopped. A range a later review covers is no longer listed among the session's
unreviewed ranges. Re-checks are not
in it: queued ones wait for the next review that holds them. The reviewer's first message
says `final: {"order": "newest first"}`, and the rubric's paragraph on final reviews says
that the answer is the main item, that coverage runs from the newest back, that a claim
the answer relies on in a `cited_by` item may be checked and a finding anchored to it
(anchoring accepts any item of the snapshot), and that the answer is compared with the
request: each deliverable and constraint, in the terms of the latest message that changed
it, met, partly met or not met, with a `completion` finding where the answer does not say
so or does not state a narrowing.

Its `fail` and `warn` findings, whatever `interrupt_severity`, are offered to the run as one
response round (`Closing("respond")`); the next closing records the turn's messages for
later reviews (`review.unreviewed`, reason `answering_turn`) and takes no other round
(`Reviews.answering`); no checkpoint is dispatched in that turn. Then one re-check runs
(`ReviewController._recheck`): a confirmation of only the final review's findings the turn
claimed fixed (`ReviewStore.answered`, `dispatch(recheck_of=...)`) and of its `fail` and
`warn` findings the turn dismissed (`ReviewStore.dismissed`, `dispatch(dismissals=...)`),
whose reasons it accepts or rejects as the next review would (CR-43), with its own budget,
`FINAL_RECHECK_CALLS` (8) calls outside the hourly calls and `FINAL_RECHECK_SECONDS`
(180 s) after the end's wait, and no later than `hold`'s limit, ended at its deadline if it
outlasts it. A turn that only dismissed has its re-check too. Whatever it finds, no second
answering turn follows. The summary gains `rechecked`, the re-check's review and outcome,
and each finding of the final review with how it ended (`resolved`, `unresolved`,
`dismissal_accepted`, `dismissal_rejected`, `dismissed` by the user, `inconclusive` or
`not_rechecked`). In `hold` the answering turn's reply is held too, until the re-check
ends. A closing after a turn
another hook took (a user's message typed during the review, a math round) runs no second
final review: the turn's new work gets an ordinary checkpoint, under the hourly calls,
waited for within the end's bound. A notice is carried to a finding's new revision only
when no run holds it (`claimed_run` null or its claim expired), so a finding a live run
was offered is not offered to it again. The platform's own
transitions (queueing a re-check, recording an inconclusive one) carry a notice no run was
given to the finding's new revision (`transition(carry=True)`). `final_review`, a project
setting with the deployment's default (`--final-review`, `[model].final_review`), says when
the answer reaches the user: `after`, the default, lets the answer stand when the agent
gives it, so the record's `answer` is that text and the answering turn's reply follows as
its `final_text`; `hold` asks for the round with `hold_answer`, so the record's `answer` is
the reply after the turn, and waits for the review at most 10 minutes from its start
(`FINAL_HOLD_SECONDS`, the controller's `hold_seconds`) within the end's bound, after
which the answer goes out as given; the limit runs on through the answering turn and the
re-check, and past it whatever is held goes out. What the user sees: with `after`, the chat shows the
answer when the agent gives it, then any findings and the agent's reply to them; the
record's `answer` is the answer given and its `final_text` the reply. With `hold`, the
chat shows a notice that the answer is being reviewed in place of the answer
(`Reviews.holds_answers`, the transcript's `withheld`, which the run's events honour). When
the review delivers nothing, the answer is shown when it ends. When its findings take a
round, the answer stays out of the chat through the round (the transcript's `holding`,
which the run's events honour too): the chat shows the answering turn's work as it goes and,
once that turn's closing and its re-check end, its reply, which is the record's `answer`
and `final_text`; the answer first given is kept in the record's messages and in the final
review's snapshot, and the chat never shows it. If the round ends without an answer of its
own (its turns run out, or it fails), the answer first given is the run's `answer`, and the
chat shows it as the run ends. If `hold`'s limit passes during the round, the chat shows a
notice that the review did not finish within it (`HOLD_ENDED`), then the answer first
given, which is the run's `answer`, and later the round's reply. If the limit passes during
the final review itself, the answer goes out as given when the review is ended, as with
`after`. If the answering
turn fails, the run keeps the answer the agent gave as its `answer` (a failed record may
hold one, never a `final_text`), stops `error`, and the summary says `answered: false,
answering_failed: true`. The hook's
summary, the run record's `review_summary`, carries `final_review`
(`ReviewStore.final_summary`): `mode`, the review, its outcome, `finished`, `answered`, the
open findings by severity (`open`), `fixes_claimed`, `complete`, `reviewed_through`,
`feed_end`, the first 16 `unreviewed` ranges, each with its items and reasons, the
session's item counts (`items`), and the items its coverage names unread
(`unread`: each one's evidence, kind, what it copies, an artifact's path and why it held
nothing to read), and how many unreviewed ranges meet each reason the reviews recorded
(`unreviewed_reasons`, `not_reached` for a range no review has reached). The workspace's
run section shows it as one line, and under it, in plain words with no identifier but
file paths (`finalReviewEnd`): why no final review ran (`no_final_review`), or each unread
item by its path or kind with its reason, how many of the session's items were reviewed,
the unreviewed ranges as a count with their reasons, the open findings by severity, and whether the findings were answered and
re-checked, with how many ended each way.

`ReviewController(persistent=True, messages=asyncio.Queue())` supplies a host boundary
for immediate new user input while the detached review continues. The host must retain
the store/event loop and call `close()` at shutdown. Cancellation requeues an input
already removed from the unbounded queue. `run` is a one-shot process: it
cancels unfinished detached work on exit, records dropped/retry state, and cannot start
an idle session or stream a held final answer into a separate live chat turn. `serve`
is long-lived, but it builds a reviewer controller for each run and closes it when the
run ends. No idle
scheduler or automatic wake capability is claimed. A second process encountering a live
owner reports pending rather than taking its grant. The GUI reads current state on
Refresh and shows the review state at each saved run boundary; it has no push transport.

## Evidence and authority

A snapshot admits at most 256 items and 8 MiB of retained JSON/locator bytes; one item is
at most 256 KiB. Open findings take their items first, re-check targets and the
author's dismissals awaiting a decision before the others, then open, then stale ones, in as much as half of an author window's snapshot (all of a
re-check's), so that new work cannot crowd them out; the feed's next rows take what
is left, and a finding that did not fit that half takes what they leave. Rows the
`actions` item folds take no item, but a window lists at most 500 actions; a raw source's
row is passed over without reading its payload. The `plan` item is read in the same read
as the window and counts within the findings' share. Document reads pin an exact
immutable revision and verify its digest.
Execution output is a bounded copied projection; mutable worker paths and job log files
are references only. An agent's ledger revision brings in the artifact revisions its evidence pins, once in each review window and whichever session of the project wrote them (CR-14 as amended): text and JSON up to 64 KiB, copied whole and verified, as `artifact` items whose locator names the entry revision (`pinned_by`); they make no review due and are not the run's effects, and one that cannot be copied whole is left to the entry's evidence, so that it does not make coverage incomplete. A confirmation snapshot's changed entry revisions bring theirs too. So does, as context, what else the evidence that bears on an entry or checks it pins (lab 40's B3), at most 8 an entry revision: the document revision a proof's block is in, as a `document` item that names the blocks (`blocks`), which the reviewer reads by block; a delegation's outcome, as a `delegation` item (the brief, how it ended, the result with its verdict and status, what it read, the alias and models, and its report, its long texts at most 16,384 characters); and an entry at the revision pinned, as an `entry` item, never a `ledger` one, so that holding it is no review of it. Each names the entry revision that pins it (`pinned_by`); one that cannot be read or does not fit an item is left to the entry's evidence. The recorded sources of an entry's quotes come the same way (CR-4, `consistency/quotes.py`). When an agent's run calls `pdf_pages` or `pdf_search`, or the e-print server's `e-print-mcp_search_eprint_fulltext`, `e-print-mcp_eprint_fulltext` or `e-print-mcp_get_eprint`, or the library server's `library-mcp_read_paper` or `library-mcp_search_in_paper`, through a connector the project configures, without an error, the review controller records the call and its result as a `source` observation that makes no review due: the tool, its arguments, and the result's structure. The call is the one the run executed (its name and arguments as the run's tool records keep them), and the transcript must name its ID once, with that name and those arguments, so that duplicate IDs attribute nothing. For a PDF, the SHA-256 and path the sandbox's helper reported and each line it returned, with page index, label and number, never the text formatted for the model; for an ePrint, the paper from the result's header and the text the server returned, apart from the header (title, authors, abstract), the phrase or query it echoes and its pointer to the next pages: since the server's change of 2026-10-31, a search's passages each with its physical page (`search_eprint_fulltext`), and a page read's pages each with its number (`eprint_fulltext`); `get_eprint` gives the date stamp, from its Date line, and no text, since neither text tool prints one; `eprint_fulltext` called with a query answers as the search does, after a line that says so; and as `eprint_fulltext` answered before, which records and archives of then hold, the date stamp from its header and the passages matching its query. The text tools' header may hold the server's own lines, exactly where the server writes them (`descriptors.eprint_header`): one `NOTE:` line and a blank line before the paper's line (where the text came from, such as `NOTE: Source: stored ePrint original.`, a newer revision, a conversion queued or the fetch budget, at most 512 characters), the compatibility line before that, and one line on a PostScript original's conversion under the paper's line. The record keeps them as `notes`, never as the paper's text: none is a header, a heading or a passage, and nothing a note says confirms a quote; a line the server does not write there leaves the result without a header, or its text without pages. `get_eprint`'s date stamp comes from the record's lines before its abstract. For a library text, the paper's work label from the first line and the ledger identifier it names, and the call's `identifier` as a further one (`aliases`) when it is exactly one identifier in a ledger scheme (`papers.exact_paper`: the forms of the library's strict grammar, never a title, a phrase or the lenient forms its resolver also reads, such as `ePrint 2017/634` or a publisher's page with a DOI in its path) that names another paper key: the library resolves `identifier` to the work it reads by an identifier its catalogue holds for that work, so its answer is its word that both name one paper, as when it files an ePrint report under its published DOI; the rendition's tier, converter, page count and revision from the second, with any further `, key value` fields, and each page read or passage found with its physical page; a page's text leaves out the notes people made on it, the server's notes on lost mathematics, the placeholder of a page without text and the pointer to the next pages, and a heading's suffix such as `(corrected by a reader on 2026-11-20)` is a flag on the page; a read whose headings are not the pages its range names records nothing. The library's cards, numbered statements, notes and other extracts are not recorded: a card is a model's summary, and a statement is put together from blocks of the text, across page breaks, not a passage a page holds. A page or passage the toolbox cut in the middle (`[… N characters omitted …]`) is left out of any record, and a library read keeps none of its pages from that one on; a reading result cut between pages keeps the pages it showed, and the toolbox's last line naming those it left out is no page's text. The rubric (`sandbox-consistency-12`) tells the reviewer a quote's source may be a library text, never its people's notes. A PDF quote is found in lines `first_line` to `last_line` of its page under the PDF's hash, spanning them; an ePrint quote inside one passage or page for that paper, with the date stamp its record holds or, for a record of the text tools since 2026-10-31, which hold none, a date stamp a `get_eprint` record of the same session gave that paper (the source pinned beside the entry then says `dated_by`) (the echo is read in either quote style `repr` gives it, and is never a passage), and a quote the call sent, as its decoded strings, only in a passage that holds more than what was sent, so that a search for a phrase and then its quote works; a library quote inside a page or passage of its physical page, in a record of the paper (the reference's ePrint, DOI, arXiv or NIST identifier, in one normal form, `papers.py`, matched against the identifier the label names and the record's aliases) at the revision the quote names, with the same rule for what the call sent. White space reads as one space. A source's key begins with a digest of the PDF's hash or the paper's identifier, its mark (a library record's, of the paper's normal identifier, such as `eprint:2024/1234`; a library record with aliases is recorded under its label's mark, and under each alias's a row that names that record's key, which is read as the record, once), so each quote reads, by the feed's key index, at most the 50 newest records of its mark in the session reviewed, never the copies pinned beside entries. Raw sources are not review evidence: the transcript holds what the agent saw, and only a source a quote cites comes into a review, so a long read cut short that no quote cites leaves the coverage whole. The sources are found before the ledger's write and recorded once a window under a key that names the entry revision; a record the feed refuses is left out, and a page the feed refuses leaves the window's coverage incomplete without failing the run's safe points. A quote not found brings nothing, and its citation cannot be confirmed. So that an agent hears of a quote that cannot be found for a mechanical reason when it records it, not at a review much later, `ledger_change`'s result, and its dry run's, names each ePrint or library quote the change adds that no source record of the session holds yet (`quotes.unmatched`, at most 8 a change, as `quotes`, each with the entry's handle and the quote's start): no `get_eprint` record of the paper, the date stamps those records gave when the quote names another, or no read of the paper's text; no library read of the paper, and when the session holds library reads of other papers, up to three of the identifiers those reads are recorded under, with the word that the check finds a library quote only in a read of the reference's own paper, recorded under one of the reference's identifiers; the revisions read when the quote names another, or the pages read at its revision when its page is not among them. It reads only the session's source records, so it says nothing the session's tool results did not show; it is advice, the quote is recorded as given, and the review reads the records again. A quote whose date stamp and a read of the paper's text, or whose page at its revision, the records hold draws no notice, whatever its text, and a run without a review controller (a delegated one) gets none. The PDF tools print the PDF's full SHA-256 and its path escaped in their header.

Before the model's first call, the platform checks copied numbers (CR-39, `consistency/numbers.py`): each number of 10 or more digits the snapshot's own ledger items (statement and fields) and document blocks state, read through `\,`, `\allowbreak`, `{,}`, thin spaces and thousands commas, is compared with the numbers of its text and JSON artifact items. One within 3 edits, or 5 % of its digits, of an artifact's number, but not equal to any, is recorded as a `fail` `contradicting_value` finding of the review, anchored to the field or block and the artifact, with both values and the artifact's path and key; a neighbour differing only in its last two digits is skipped. At most 4 a review, with deterministic operation IDs, so a retry records nothing new. The reviewer's first message lists them (`platform_findings`). The workspace's finding card, when the reader opens "Compare the two numbers digit by digit", lines the two numbers up digit by digit, as few edits apart as they are, 40 places a row, and marks each place where they differ (`CopiedNumbers`); numbers too long to line up (over 250,000 digit pairs, about 500 digits each) are shown whole, one above the other. It reads them from the end of the platform's text, so a number in the artifact's key, which the text quotes earlier, cannot stand in for them. Clipped output cannot have physical line-number anchors. Rich
outputs, automatically discovered file contents and a shell artifact inventory are omitted and make
coverage incomplete. No missing-evidence assessment can become a failing missing-action
finding, and an incomplete/unchecked window cannot become passed.

The [immutable artifact inventory](ARTIFACTS.md#explicit-publication-consistency-consumer)
provides durable explicit-publication events and copied, exact raw UTF-8 source up
to 64 KiB. Per-session feed/cursor commits are atomic and bounded. Unsupported media,
non-UTF-8, oversized and unavailable content remain explicitly incomplete; unresolved
publications contribute one finite uncertainty observation rather than disappearing
on restart. Only the publishing session gets a source grant. Producer and observed
inputs remain unknown. Reproductions do not increment the new-revision trigger, and
legacy live-tool metadata does not duplicate canonical events when the consumer is active.
`review_read` slices exact decoded source characters with the original byte SHA, at
most 16,000 a read, which is also its default; its schema says that `start` and `count`
count characters, not items (lab 41);
JSON escaping is not treated as source lines. Empty source remains empty and has an
explicit audited exposure marker. Artifact anchors name the whole revision.

Re-check snapshots explicitly grant the original finding evidence and response
anchors under their original evidence IDs, scoped to the same project/session. They
also pin the fix's revisions and the later revisions that replaced what the finding
reviewed, from the durable transition payload and the finding's queued re-check, each
once, even if the process crashed before optional feed observation. Those bytes count
toward the snapshot bound. So that a fix is checked against what was done and what it
names (lab 41), a re-check also holds the `actions` item of what the session's runs
did since the reviews that raised its findings ended, with each document revision's
changed blocks, and, beside each claimed fix, the artifacts its explanation, payload
and changed document revisions name by path (as the project names it, or as the run
does from its working directory, `sessions/<session>/…` then `shared/…`), at their
latest revision, copied whole as a ledger entry's pins are, at most 8 a fix, each
naming the finding (`cited_by`). Re-check coverage describes the exact finding/fix groups and
their original and response evidence; unrelated pending author work cannot make that
scope incomplete. Re-checks consume no author feed and preserve its retry window.
A passed re-check review still requires a recorded checked claim. An omitted group remains
incomplete and is not counted as attempted.
If no re-check target fits, an incomplete author-work window can still advance its
feed; the omitted re-check stays queued.

Reviewer tools are only `review_manifest` and `review_read`, `review_checked` (a checked-item record),
`review_finding` (finding creation, optionally naming the open finding of the same work it replaces), `review_decide` (a re-check's `resolved` or `unresolved`, or the acceptance or rejection of the author's dismissal) and `review_citation` (a quote's check, CR-4: `supported`, `unsupported` or `inconclusive`, with an explanation, for a quote's `source` item the snapshot holds, once the reviewer has read all of it and of the entry revision that quotes it; written to `citation_checks` with the digest of that revision's statement, fields, assumptions and evidence, and the tool and arguments the source came from (past 8 KiB of JSON, a clipped copy and the digest of the whole; a check that cannot be stored is the tool's error, not the review's failure). The rubric tells the reviewer that a source is what a tool returned, and may be a PDF an agent made unless the PDF is vouched for). Unknown or denied calls are recorded and consume the
model-turn budget. When the reviewer's model accepts images (its alias, declared or as the gateway says; [figures](specs/figures.md#view_image-as-built)), it has `view_image` too: an artifact revision of its snapshot, named by its evidence ID (and for a PDF a page and region), shown after the turn's results in a framed message, as data. PNG and JPEG go as recorded; anything else the platform draws in the session's environment, as its own execution (actor `platform`, never the reviewer's), and the result says how far a drawing can be trusted. Each review has its own viewer, whose images go when the review ends; a call's reservation counts each image it carries by its pixels (a token for every 750) beside its characters, and the call's reported usage counts as before. The reviewer has no worker client, author toolbox, file/network tools,
policy mutation or delegation. The separate call/hour, turn, time, response/context and
soft token budgets reserve before awaiting a model. Actual provider usage is recorded;
actual usage crossing the token budget makes the outcome capped, including a final
response. These are role budgets in calls, turns, time and tokens. The cost the gateway
reports for each call is kept with it and counts in the project's cost and its optional
daily cost limit ([run service](specs/run-service.md), RS-6a); the gateway's own budget
stays the hard cap.

## Findings, delivery and workspace

A reviewer's statement, remedy, decision explanation, checked statements and summary, and the main agent's response explanation and response body, hold no character the user may not see, as it is or as a character reference (`check_agent_text`, threat model A3): the user decides on them, and agents read them again. The check runs when they are written, after their line ends become line feeds; a reviewer whose summary is refused is told why and writes it again, once. The user's own explanations are kept as written. The finding card shows the statement, the remedy and the latest explanation a reviewer or the main agent gave, with its explanation source, exactly (`ApprovalText`) above the user's decision; the explanation is read when the user opens the response. Each step of the history's reason and each response body exactly under its rendering, with a banner beside any rendering that hides a character or a link (`HiddenBanner`); stored text from before the check reads the same way. A review's summary shows hidden characters as code points.

Findings retain severity, category, Markdown/LaTeX prose, exact anchors, remedy, revision,
reflags and their attributed history. Reflags can strengthen the current
severity/prose/remedy while retaining the previous
assessment in history, and a weaker reflag cannot downgrade that current assessment.
User writes use strict expected revisions and replay-safe operation IDs.
A fix may cite changed ledger entry revisions (`entry_revisions`, a later revision of an entry the finding is anchored to that changes the anchored field, and the entry's statement, fields, assumptions or evidence unless that field is its title, status or links); the re-check's snapshot pins each as a `ledger` item, and its answer requires a whole read of it. A response without them keeps the digest it had before the field. A finding's anchor may name a field of a ledger entry revision the snapshot holds (`title`, `statement`, `status`, `fields`, `links` or `evidence`); an anchor on a ledger entry counts as replaced only when a later revision changed what a decision rests on, and the anchored field when it names one ([ledger](LEDGER.md)). `review_finding_entries` keeps the entries each finding is anchored to, written by triggers, and an index finds the review evidence that holds an entry.
Reviewer decisions compare the pinned finding revision and current head before
writing. A re-check's answer also requires audited read ranges covering the finding metadata,
original/response anchored blocks (or whole items) and the exact revisions it holds. Empty slices of a nonempty source,
out-of-range and unrelated-prefix reads cannot satisfy that guard. Exact empty artifact
reads have a separate audited exposure marker. Typed changed-artifact references must
match a relevant logical artifact and an existing canonical session grant; unavailable
artifact content cannot be resolved by reading its metadata. This records exposure, not
model understanding. Generic reflags arriving while a re-check is queued, or on a
dismissed, stale or bounded finding, append a deferred audit observation without changing
its revision or response payload.

### A finding's lifecycle

A finding stays a record of the review store (CR-40 to CR-47, decided by the owner on
2026-10-25): it is no ledger entry and its changes make no ledger revision. It has one of
five statuses, `FindingStatus` in `consistency/models.py`, which a trigger of the control
store enforces:

| Status | Meaning | Set by |
| --- | --- | --- |
| `open` | Raised, and not settled | The review controller, for a review or a check; the user |
| `fixed` | A re-check on a later revision found it resolved, or the math check passed | The review controller on the reviewer's `resolved`, the math check; the user |
| `dismissed` | Set aside with a reason | The main agent (`dismiss`); the user |
| `superseded` | Replaced by a later review's finding, which `superseded_by` names | The review controller; the user |
| `stale` | What it reviewed has a newer revision and no re-check ran in its session | The platform at the session's close; the user |

Each change is one row of `review_finding_history` and one `finding.transitioned` event:
the finding revision it made, the status, the actor and its kind (`user`, `controller` for
a review or a check, `platform`, or `agent` for the main agent), the time, the reason, and
for a re-check its answer and its record (its review, the revisions or messages it held,
its evidence anchors and the reviewer run: alias, resolved model, rubric version). The
finding keeps its latest re-check (`latest_recheck`), the author's or the user's
dismissal (`dismissal`, with the review's acceptance or rejection), a queued re-check
(`recheck`), its successor (`superseded_by`; the successor names it in `supersedes`) and
whether it is open at its re-flag bound (`at_bound`). The workspace serves all of it with
the finding and its history (`/findings`, `/findings/{finding}`).

**The re-check** (CR-42, CR-24, CR-28 as decided). At each review dispatch
(`Findings.queue_rechecks`), an open or stale finding whose anchored document block,
artifact or ledger entry field has a later revision gets its re-check queued; a replaced
revision no longer supersedes a finding by itself. A `fix` queues one too: it names the
document, artifact or entry revisions, or for a finding on messages the later archive
messages (`messages`), that address the finding, or pinned evidence of them; each must be
strictly later than what the finding reviewed (`Marks.later`): a later revision of a
document, artifact or entry its anchors name, or an archive message after its review's
snapshot. An unrelated document or the reviewed revision itself is refused, and an
execution's output counts only as the later tool-result message that holds it. The
finding stays as it is, and only a re-check or the user makes it `fixed`. A queued re-check is dispatched
with the next review, as a `confirmation` review (the review type keeps its name), whose
snapshot holds the finding, its anchors, the later revisions or messages, and the
author's explanation as the author's. The reviewer answers with `review_decide`:
`resolved`, which makes the finding `fixed`, or `unresolved`, which records a successor on
the new revision, `open`, with the same category and severity, and the finding
`superseded`. Each answer needs at least one evidence anchor, which code checks exists in
the snapshot, is strictly later than what the finding reviewed, and lies in what the
re-check holds (`_in_new_places`). A re-check the review
does not answer, because it was capped, failed or gave no answer with a valid anchor, is
recorded when the review ends as the finding's latest re-check, `inconclusive` with why,
and the finding stays `open` ("open, last re-check inconclusive"); it is tried at the next
dispatch, at most 3 times in each session (`RECHECK_ATTEMPTS`, counted from the history). Re-checks count toward the review's
budget, not toward its 10 findings. A math finding is re-checked by its check.

**Dismissals** (CR-43). The main agent's `dismiss` replaces `rebut`: a reason of at most
2,000 characters, checked as agent text (also when the reason is corrected), shown to the
user. The next review's snapshot holds the dismissed finding, and that review accepts or
rejects the reason (`accept_dismissal`, `reject_dismissal`); its run record counts the
dismissals it rejected (`dismissals_rejected`). The dismissal stands either way, and the
finding is not offered to the author again.

**Superseded and stale** (CR-44). A later review's finding may name the open finding of the
same work it replaces (`supersedes`): the controller checks that the two review the same
revision of a document, artifact or entry, or overlapping spans of archive messages, and
refuses it otherwise; a re-flag is not a replacement. At a session's close (the
`session close` command), each open finding whose reviewed work has a newer revision and
of which no re-check ran in that session becomes `stale`; a finding anchored only to
messages never does. A stale finding is not offered to the author. A dispatch in any later
session of the project queues its re-check for that session's review, holding the
revisions that replaced what it reviewed (and an earlier fix's), and the answer decides it
as for any re-check; so a stale finding, a migrated one included, can clear the gate.

**Who may do what.** The main agent may `fix` or `dismiss` an open finding, never set a
status; a delegated agent changes none (CR-23). The user may `fix` an open or stale
finding, `dismiss` any, or `set` any status from any other with a reason, from the
workspace, and for `superseded` name the successor. For the ledger's gate (LG-17;
`ledger/gate.py`), a `fail` finding is resolved when it is `fixed`, or `dismissed` by the
user or with a review's acceptance; `open`, `stale` and a dismissal by the agent alone are
unresolved, and a `superseded` finding counts as its successor does.

**The control store step.** The migration to this lifecycle
(`control/migrations.py`, `_finding_lifecycle`) adds the columns and the history table,
copies every `review_transitions` row into the history with its actor, time and reason,
and maps the eight earlier statuses: `open` and `claimed fixed` to `open` (a claimed fix's
re-check queued, holding the revisions it named and the evidence it cited that are later
than what the finding reviewed), `confirmed fixed` to `fixed` (a
review's confirmation kept as its resolved re-check), `withdrawn` to `dismissed` (by the
user, or the author's rebuttal accepted by the review), `challenged`, `unaddressed` and
`inconclusive` to `open` (a challenged finding keeps the rebuttal as a dismissal the
review rejected and, like an unaddressed one, is at its bound; an inconclusive
confirmation is carried as its last re-check marked `inconclusive`, while a finding raised
on missing evidence is open with no re-check), and `superseded` to `stale`. Every
transition becomes a history row; a migrated confirmation keeps its answer (`resolved`,
`unresolved` or `inconclusive`), the earlier status name is kept as `former_status`, and a
status no step maps refuses the store. The step changes nothing when it runs again.

Interrupt-threshold findings are delivered at main-agent safe points as bounded
untrusted data, but only those reviews of the run's own work raised or decided (the
owner, 2026-10-16; CR-21 as amended): the session's earlier findings, as one the last
review of a closing raised after the run's rounds, reach the next run as counts
(`sandbox-notice-review-counts-3`), counts of open findings by severity, which point to
`review_findings` and the review panel and say that following one up is the run's
choice, never required (CR-16 as amended). Only open findings that are not at their
re-flag bound are offered (`sandbox-notice-review-findings-2`); a dismissed or stale
one is not, and only open findings delivered to the run start a response round (CR-29 as
amended). An agent's own fix or dismissal is not delivered back to the session's runs: in
lab 40 the agent read its rebuttal as a new notice and rebutted again, seven times, until
its run ran out of turns. A run's final review takes one response round, which its `warn`
findings take too (CR-48). When the response rounds end, each open finding delivered and
not settled stays `open` at its bound (CR-32's `unaddressed` as CR-47 reads it). Other severity counts are delivered without their prose or anchors and
do not cause an extra author response turn. Notices are acknowledged only after a
successful model call received them and `ProjectStore.save_run` durably saved the run.
A failed model/save or a finding arriving on the last allowed model turn leaves it
pending. No findings automatically edit documents or weaken tool policy.

**A missing handle is a suggestion** (the owner, 2026-10-16, after context-management lab round 1; CR-4 and CR-18 as amended). The rubric tells the reviewer to raise one finding of category `handle_citation` when an answer, note or document relies on a ledger entry, or declines or reopens something because of one, and names no handle (D-3, A-1, C-4, I-3); its remedy names the handle to cite. The main agent's prompt (`sandbox-main-9`) asks for the citation too, so this is the second, weaker line. The finding is advisory by construction (`ADVISORY_CATEGORIES` in `consistency/models.py`): it is always stored as `note`, whatever severity the reviewer gave; it is left out of `finding_states` (so it does not make a conversation turn a terminal review), of the counts a run is told, of the notices offered to a run, even at an interrupt severity of `note`, and of the re-check targets; the author's `fix` or `dismiss` of it is refused, so it is never re-checked, and the user can dismiss it; a re-flag never brings it to its bound; and a review whose only findings are of this category may still end `passed`. It is listed with the other findings in the review panel and by the author's list tool.

**Cryptographic claims** (rubric `sandbox-consistency-9`, 2026-10-23). A stated security level, attack cost or security notion is checked from its form, by reading; the reviewer estimates no cost and runs nothing. Seven categories: `cost_model` (a cost with no cost model named, or enumeration compared with sieving as one measure), `preconditions` (an attack cost quoted without the structure it needs, its validity range or a plausible memory; a cost below 2^64 on a standardized set is questioned, not repeated), `unqualified_security` (secure, verified or proven, or a bare security parameter, without model, attack families and assumption), `notion` (a notion weakened or restated with another game, quantifier order or oracle access, or a level above the construction's cap), `parameters` (official parameter sets with no source in the session; a labelled sweep or unverified set is no finding), `structure_from_naming` (an attack said to transfer by name, without the structural argument) and `quantum_conflation` (classical and quantum costs mixed, or a speedup applied where none exists). Severity follows where the claim is: `warn` in prose (a final answer, a thread reply, a finding response), `fail` in a saved artifact (a document, a ledger entry, an artifact). A correct terse claim, a cost model named once and referred to after, and a standard result cited without a source are not flagged; an attack shown to apply at a stated cost under a named model is recorded with `review_checked`. The [scientific-writing skill](SKILLS.md#the-built-in-pack) asks the author for the same.

The session workspace lists recent reviews/findings, outcome/usage/configuration
identity, paged immutable evidence and audit records, original anchors and paged decision
history. User responses keep drafts and exact-payload retry IDs across refresh/reload;
a stale finding revision is an explicit conflict until the user chooses the current
revision. Routes reuse authenticated cookies and the exact same-origin POST guard.
The browser never supplies USER attribution or a reviewer owner grant. Reviewer prose
uses the shared safe Markdown/math renderer with no inherited document macros; JSON and
evidence previews remain escaped text.

Mathematical-renderability findings (CR-36 to CR-38 and CRF-14) are described
[below](#mechanical-math-findings). Browser rendering is not presented as an automatic
review verdict. Historical finding selection and supersession inspection are bounded;
omitted snapshot context is explicit incomplete coverage.

## Browser fixtures

Two scripted fixtures seed disposable, marked state directories for checking the
workspace's review views by hand, without a worker or a real model. They were used for
the [browser acceptance](../.agents/docs/agent-reports/consistency-review-2026-10-12.md)
of the review views and the [artifact review acceptance](../.agents/docs/agent-reports/artifact-review-2026-10-12.md).

The tracked synthetic browser fixture creates a new, marked state directory:

```bash
uv run python packages/orchestrator/tests/fixtures/consistency_browser_fixture.py create /tmp/ss-review-demo-new
uv run sandbox-science-workspace serve --state-dir /tmp/ss-review-demo-new --static web/workspace/dist --listen 127.0.0.1:8200
```

Open the server's temporary login URL, then `/#/projects/review-demo/sessions/main`.
The report claims 16 while an explicitly synthetic pinned output records 15. In the
document editor, change only `count was 16.` to `count was 15.` and save; claim a fix
on the contradiction with that exact document revision, which queues its re-check. It
stays open until the separate scripted re-check runs:

```bash
uv run python packages/orchestrator/tests/fixtures/consistency_browser_fixture.py confirm /tmp/ss-review-demo-new
```

The helper requires its creation marker, seeded finding identity, and exact corrected
revision text/digest before confirming. It uses only `ScriptedModel`, reads the pinned
evidence, and prints the attributed decision and role usage; it sends no model or worker
request. The fixture directory and loopback server are disposable acceptance resources.

A separate artifact fixture seeds a new marked directory with exact original and
corrected synthetic text, without a worker or a real model:

```bash
uv run python packages/orchestrator/tests/fixtures/artifact_review_browser_fixture.py create /tmp/ss-artifact-review-new
```

Serve that disposable directory with the workspace command above, replacing its
state path, then open `/#/projects/artifact-demo/sessions/main`. The old evidence must
still read `count = 16` even though the current artifact head is the corrected15.
The creation marker contains `corrected`, the exact canonical reference for a
same-origin authenticated finding API fix claim under `artifact_revisions: [ref]`.
The fixture also seeds exact empty UTF8, binary and non-UTF8 evidence, another private
session and a foreign project for scope checks. Empty source has a separate audited
exposure and an outside-source GUI label; the source `<pre>` remains empty. The existing
document-revision GUI field is unchanged. After that explicit claim:

```bash
uv run python packages/orchestrator/tests/fixtures/artifact_review_browser_fixture.py confirm /tmp/ss-artifact-review-new
```

The helper refuses unrelated state/claims, verifies both seeded byte hashes, reads
original and changed grants through ScriptedModel, and records the re-check's `resolved`. It proves
plumbing and scoped evidence exposure, not model judgment. Remove the disposable
server and directory afterwards.

## Mechanical math findings

D82 math checks use the same finding/history and authenticated USER decision surfaces, with explicit `math` origin, a real mechanical check ID and no synthetic reviewer run. Reviewer categories/grants are unchanged and exclude this mechanical authority. Generic re-check, reflag, replacement, staleness and threshold delivery paths cannot decide math findings. A separate bounded controller checks immutable document revisions and collects closing-only notices together with terminal findings, sharing at most two author response rounds. Accepted current formulas and complete parsed removal make the finding `fixed` mechanically; stale, skipped and unavailable evidence never does. USER takeover retains unresolved historical evidence while excluding new author reflags/notices. The checks cover document revisions, comment and reply bodies ([comment math](COMMENT-MATH.md)), MAIN fix and dismissal explanations and ledger statements ([ledger](LEDGER.md)); details and limits are in [document math checks](MATH-CHECKS.md).

The [MAIN explanation math checks](FINDING-PROSE-MATH.md) cover only
agent MAIN fix and dismissal explanation source (earlier rebuttals too). Its ordinary corrections preserve the
original scientific action/evidence, use exact body and finding CAS, and fence
stale reviewer snapshots independently. The workspace history is read-only.

## Tables

These are the reviewer's tables in the control store's schema
([living documents](LIVING-DOCUMENTS.md#the-control-store)). A test
(`test_docs_inventory.py`) compares this list with the schema.

| Table | Holds |
| --- | --- |
| `review_config` | Each project's numbered reviewer configurations, append-only |
| `review_windows` | Each session's review state: the author-work watermark, last dispatch, retry end, failures and manual requests |
| `review_feed` | Observations of author work (document revisions, ledger revisions, execution projections, artifact events), with what they count toward the triggers, append-only |
| `review_runs` | Each review: its kind, owner lease, feed range, model alias, rubric, configuration, classifier, usage, cap and coverage |
| `review_evidence`, `review_grants`, `review_reads` | A review's snapshot items, the evidence it may read, and each read it made, append-only |
| `review_confirmation_targets` | The finding revisions a re-check review (kind `confirmation`) holds, append-only |
| `review_calls` | Each reviewer model call's usage and prompt provenance |
| `review_records` | A review's checked items, findings, deferred reflags, re-check successors and dismissal decisions, append-only |
| `review_findings` | Findings with their current severity, category, statement, anchors, remedy, status (one of five), revision and origin (`review` or `math`), their successor, latest re-check, dismissal, queued re-check and whether they are at their re-flag bound |
| `review_finding_entries` | The ledger entries each finding is anchored to, kept by triggers as findings are written and their anchors change ([ledger](LEDGER.md)) |
| `review_transitions` | Every finding revision with its status, actor, reason and payload, append-only |
| `review_finding_history` | Each change of a finding: its revision, status, actor and actor's kind, reason, and for a re-check its answer, review and record; the earlier status name for rows the lifecycle step copied, append-only |
| `review_notices` | Findings due for delivery to the author, with their claim and delivery |
| `review_response_pins`, `review_response_discovery` | The explanation body revision each review pinned, and older responses held from checking because their author is unknown ([finding prose math](FINDING-PROSE-MATH.md)); the explanation bodies and their revisions are `text_revisions` of kind `response` ([living documents](LIVING-DOCUMENTS.md)) |

## Not built

- Shell-written artifact inventory and revisions (CRF-6), rich outputs and file contents as evidence, and ledger-specific evidence.
- Automatic idle turns, a reviewer that outlives a run, and a push view.
- Math checks of ledger fields other than statements and of reviewer, USER and platform prose (D82).
