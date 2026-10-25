# Automatic consistency review

This page describes the automatic consistency reviewer as built in `packages/orchestrator`
(`sandbox_science.orchestrator.consistency`). It implements the approved
[behavior specification](specs/consistency-review.md), with a separately authored,
versioned rubric (`sandbox-consistency-9`, D56). The reviewer finds the record of each action the author narrates (in a final answer, a thread reply or a finding response: changed, wrote, recorded, published, ran or fixed): a refused or failed tool call is no record; a narrated action with none is a `fail` of category `missing_action` anchored to the narration, and one whose record says otherwise is `contradicting_value` (CR-4, CR-18). To match against, each window's snapshot holds one `actions` item (CR-2 as amended for labs 40 and 42): the toolbox names each call's effect class in its record, the review controller records each call of any class but `read` (an effect, a plan change, a memory proposal or a cancel) as an `action` observation that makes no review due (the tool, its target as its arguments name it, its outcome, `ok`, `refused` when a gate, a policy or the store turned it down, or `error`, and what it made: a document or ledger revision, a work record, an artifact revision, an execution, files written, a plan's version with the step and status or the plan's status it recorded, a memory proposal), and the item lists them by run, in order, as many as 500 within half an item, with the blocks each document revision in the window changed since the revision before (document items carry them too, as `blocks`). Actions are not items one by one. A terminal review's item also lists, under `earlier`, the run's actions and its document revisions' changed blocks in its windows reviewed before, from the run's first observation to the watermark, as context for an answer that says what the run did earlier (lab 42's B5): its own and those of its delegated runs brought for its review, not another run's; the latest 500 of each, and the oldest left out first when they do not fit, before any of the window's own. The rubric has the reviewer raise no finding on an earlier action itself. Every snapshot of a session with a plan holds a `plan` item, as context: the plan as recorded in the read that fixes the review's window, with its status, goal outcome and each step's title, track and latest recorded status, by whom and when (lab 42's B2: a run that finished its plan and said so drew `fail` findings when neither held its plan changes). A delegated run has no review of its own, so its
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
(one item for each state a tool call saw an execution in, a `wait_jobs` call's ended jobs
included, whose record keeps their executions for the review: lab 41's B7),
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

**Limits (SNOVA follow-up).** A new author-work review takes a contiguous feed prefix of at most 16 new evidence items and approximately 256 KiB by default, with the existing 256-item/8-MiB snapshot ceiling still bounding finding groups and derived context. It also carries up to eight recent evidence items from before the watermark, within 128 KiB; prior ledger claims are carried as non-promotable context and oversized prior evidence is represented by a locator marker, so a claim and observation on opposite sides of a window boundary can be compared. One oversized first new item is admitted so the cursor cannot stall. A clean prefix can pass its own span, but its `coverage_complete` remains false while later feed rows are pending, so ledger promotion cannot use it. A partial delegated batch remains incomplete. Earlier inconclusive or incomplete finding spans, or historically unreviewed spans, prevent a later terminal pass from certifying the whole session. On a capped or failed attempt the next attempt takes at most one quarter as many new items/bytes; after three failures its watermark stays put and further attempts are admitted at least five minutes after the last failure. A later safe point or terminal boundary must trigger that retry; idle timer dispatch is still planned. This prevents an unavailable reviewer from repeatedly buying calls and silently skipping evidence. Each review still has at most 24 calls, 300 seconds, 300,000 characters of context per call, and 400,000 tokens over all calls; the project reviewer has at most 240 calls per hour. The budget counts provider usage when reported and otherwise a conservative estimate. The run service accepts `--review-token-budget` and `--review-calls-per-hour`, forwarded from `[model]`. A manifest page lists up to 50 items. Of one turn's tool calls, the first 16 run and the rest are answered `not_run`. The GUI shows calls, tokens, cap reason, incomplete coverage and unreviewed spans. When hourly calls are exhausted, work remains pending until the cap permits another dispatch. Contradictions farther apart than the carried evidence still need a later indexed aggregate review.

Checkpoints start at live safe points after the configured minimum interval and document,
artifact, kernel-cell or manual-request thresholds. The terminal rule classifier records
its version, completion-claim match, durable run effects, open/claimed-fixed finding
states and document/ledger/artifact ingestion coverage. Ordinary conversation without those triggers
skips terminal review; failed/interrupted author loops do not run a terminal reviewer.

Terminal holds process at most 24 bounded windows by default. Each window pins a contiguous
feed prefix; unseen overflow retains its cursor. Terminal ingestion drains through a
pinned document-event head, and a ledger-event head, in pages of 200, at most 10,000 events per boundary, and marks
remaining backlog incomplete. A page's observations and its cursor commit in one change,
as the artifact pages' do, and the session is checked once per call, not per item. A
page that fails (an observation the store refuses) stops that ingestion as a whole: no
later page is read, and the next boundary starts again at the same cursor. Completed
windows advance the author-work watermark;
failed/dropped windows, and those a limit ended, retain their exact retry end. Three failed attempts defer further dispatches for at least five minutes without advancing the watermark. Re-check attempts
(reviews of kind `confirmation`, [below](#a-findings-lifecycle)) are counted on each
finding and cannot clear, advance or replace an author-work retry window.
Exhausted or answered re-check targets do not starve later author-work reviews.

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
are references only. An agent's ledger revision brings in the artifact revisions its evidence pins, once in each review window and whichever session of the project wrote them (CR-14 as amended): text and JSON up to 64 KiB, copied whole and verified, as `artifact` items whose locator names the entry revision (`pinned_by`); they make no review due and are not the run's effects, and one that cannot be copied whole is left to the entry's evidence, so that it does not make coverage incomplete. A confirmation snapshot's changed entry revisions bring theirs too. So does, as context, what else the evidence that bears on an entry or checks it pins (lab 40's B3), at most 8 an entry revision: the document revision a proof's block is in, as a `document` item that names the blocks (`blocks`), which the reviewer reads by block; a delegation's outcome, as a `delegation` item (the brief, how it ended, the result with its verdict and status, what it read, the alias and models, and its report, its long texts at most 16,384 characters); and an entry at the revision pinned, as an `entry` item, never a `ledger` one, so that holding it is no review of it. Each names the entry revision that pins it (`pinned_by`); one that cannot be read or does not fit an item is left to the entry's evidence. The recorded sources of an entry's quotes come the same way (CR-4, `consistency/quotes.py`). When an agent's run calls `pdf_pages` or `pdf_search`, or the e-print server's `e-print-mcp_eprint_fulltext` through a connector the project configures, without an error, the review controller records the call and its result as a `source` observation that makes no review due: the tool, its arguments, and the result's structure. The call is the one the run executed (its name and arguments as the run's tool records keep them), and the transcript must name its ID once, with that name and those arguments, so that duplicate IDs attribute nothing. For a PDF, the SHA-256 and path the sandbox's helper reported and each line it returned, with page index, label and number, never the text formatted for the model; for an ePrint, the paper and date stamp from the result's header and the passages the server returned as matching, apart from the header (title, authors, abstract) and the query it echoes. A PDF quote is found in lines `first_line` to `last_line` of its page under the PDF's hash, spanning them; an ePrint quote inside one passage for that paper and date stamp (the echo is read in either quote style `repr` gives it, and is never a passage), and a quote the call sent, as its decoded strings, only in a passage that holds more than what was sent, so that a search for a phrase and then its quote works. White space reads as one space. A source's key begins with a digest of the PDF's hash or the paper's identifier, its mark, so each quote reads, by the feed's key index, at most the 50 newest records of its mark in the session reviewed, never the copies pinned beside entries. Raw sources are not review evidence: the transcript holds what the agent saw, and only a source a quote cites comes into a review, so a long read cut short that no quote cites leaves the coverage whole. The sources are found before the ledger's write and recorded once a window under a key that names the entry revision; a record the feed refuses is left out, and a page the feed refuses leaves the window's coverage incomplete without failing the run's safe points. A quote not found brings nothing, and its citation cannot be confirmed. The PDF tools print the PDF's full SHA-256 and its path escaped in their header.

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
messages (`messages`), that address the finding, or pinned evidence; the finding stays as
it is, and only a re-check or the user makes it `fixed`. A queued re-check is dispatched
with the next review, as a `confirmation` review (the review type keeps its name), whose
snapshot holds the finding, its anchors, the later revisions or messages, and the
author's explanation as the author's. The reviewer answers with `review_decide`:
`resolved`, which makes the finding `fixed`, or `unresolved`, which records a successor on
the new revision, `open`, with the same category and severity, and the finding
`superseded`. Each answer needs at least one evidence anchor, which code checks exists in
the snapshot and lies in what the re-check holds (`_in_new_places`). A re-check the review
does not answer, because it was capped, failed or gave no answer with a valid anchor, is
recorded when the review ends as the finding's latest re-check, `inconclusive` with why,
and the finding stays `open` ("open, last re-check inconclusive"); it is tried at the next
dispatch, at most 3 times (`RECHECK_ATTEMPTS`). Re-checks count toward the review's
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
of which no re-check ran becomes `stale`; a finding anchored only to messages never does. A
stale finding is not offered to the author, and is re-checked when a later dispatch finds
its reviewed work replaced.

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
re-check queued, holding the revisions it named), `confirmed fixed` to `fixed` (a
review's confirmation kept as its resolved re-check), `withdrawn` to `dismissed` (by the
user, or the author's rebuttal accepted by the review), `challenged`, `unaddressed` and
`inconclusive` to `open` (a challenged finding keeps the rebuttal as a dismissal the
review rejected and, like an unaddressed one, is at its bound; an inconclusive one carries
its last re-check marked `inconclusive`), and `superseded` to `stale`. A history row keeps
the earlier status name as `former_status`. The step changes nothing when it runs again.

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
its run ran out of turns. When the response rounds end, each open finding delivered and
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
