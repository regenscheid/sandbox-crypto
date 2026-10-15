# Automatic consistency review

This page describes the automatic consistency reviewer as built in `packages/orchestrator`
(`sandbox_science.orchestrator.consistency`). It implements the approved
[behavior specification](specs/consistency-review.md), with a separately authored,
versioned rubric (`sandbox-consistency-4`, D56). Version 4 has the reviewer find the record of each action the author narrates (in a final answer, a thread reply or a finding response: changed, wrote, recorded, published, ran or fixed): a refused or failed tool call is no record; a narrated action with none is a `fail` of category `missing_action` anchored to the narration, and one whose record says otherwise is `contradicting_value` (CR-4, CR-18). To match against, each window's snapshot holds one `actions` item (CR-2 as amended for lab 40): the toolbox names each call's effect class in its record, the review controller records each call whose class is `effect` as an `action` observation that makes no review due (the tool, its target as its arguments name it, its outcome, `ok`, `refused` when a gate, a policy or the store turned it down, or `error`, and what it made: a document or ledger revision, a work record, an artifact revision, an execution, files written), and the item lists them by run, in order, as many as 500 within half an item, with the blocks each document revision in the window changed since the revision before (document items carry them too, as `blocks`). Actions are not items one by one. It never executes code. The planning
consistency review of 2026-10-10 is kept, as history, in
[.agents/planning-consistency-review-2026-10-10.md](../.agents/planning-consistency-review-2026-10-10.md).

The control application keeps the reviewer's tables ([below](#tables)) in the existing
`control.sqlite3` ([living documents](LIVING-DOCUMENTS.md#the-control-store)).
Observations, snapshots, evidence grants, tool reads, model usage, checked items,
findings and their transitions are durable. A fenced lease permits one live reviewer
per session across harness processes. Recovery drops only expired owners. A stale owner
cannot read through a reviewer grant, spend budget, raise a finding or finalize a review.

## Running and scheduling

With `--state-dir`, the harness observes its transcript, canonical execution projections,
agent-authored document revisions, the session's agents' ledger revisions and the flags
their changes raise or clear ([ledger](LEDGER.md))
and durable explicit-publication artifact events. The reviewer uses
`--reviewer-model ALIAS` with the existing model gateway, or `--reviewer-script FILE`
plus that alias for synthetic validation. The alias may be MAIN's: the reviewer still has
its own calls, limits, budget and records. The adapter limits each answer to 2,048 tokens
and 64 KiB before the reviewer applies stricter response bounds. No alias makes zero
reviewer model calls; required windows end failed, retry twice, then become visibly
unreviewed. This never becomes a pass.

**Limits (lab 38).** A review of a normal session's window by a large model reads twenty to fifty items in eight to twenty calls, each sending the whole conversation again, so its limits are set for that: at most 24 calls, 300 seconds, 300,000 characters of context per call, and a budget of 400,000 tokens over all its calls; and a project's reviewer makes at most 240 calls an hour. The budget counts what each call used, as the provider reports it, and an estimate only for a call not yet answered or one the provider does not report. The run service takes the budget and the hourly cap as `--review-token-budget` and `--review-calls-per-hour`, the settings of a project that has none of its own, and the launcher passes `[model]`'s `review_token_budget` and `review_calls_per_hour`. A manifest page lists up to 50 items; a larger `limit` is read as 50. Of one turn's tool calls, the first 16 run and the rest are answered `not_run`, asking the reviewer to make them again; only text or tool arguments over their bounds fail a review. A review a limit ends is `capped`, and its summary names the limit and what it used: the token budget, with the tokens counted and the calls made; the project's hourly cap, with its calls in the last hour; the call limit; the time; or the context. The GUI's review view shows each review's calls, tokens and counted budget against its limits. A review a limit ends keeps its window, as a failed one does: the next review covers it again, and the third time it is left unreviewed; a terminal hold does not retry it at once, since the same limit would end it again. When the project's reviewer has no calls left this hour, no review starts: the session's work waits as one pending review, covering all of it since the last, which the next safe point or run boundary starts once calls are left. The first deferral records `review.deferred`, and the GUI's review list says the session's review is waiting, and why, until a review starts.

Checkpoints start at live safe points after the configured minimum interval and document,
artifact, kernel-cell or manual-request thresholds. The terminal rule classifier records
its version, completion-claim match, durable run effects, open/claimed-fixed finding
states and document/ledger/artifact ingestion coverage. Ordinary conversation without those triggers
skips terminal review; failed/interrupted author loops do not run a terminal reviewer.

Terminal holds process at most three bounded windows. Each window pins a contiguous
feed prefix; unseen overflow retains its cursor. Terminal ingestion drains through a
pinned document-event head, and a ledger-event head, in pages of 200, at most 10,000 events per boundary, and marks
remaining backlog incomplete. A page's observations and its cursor commit in one change,
as the artifact pages' do, and the session is checked once per call, not per item. A
page that fails (an observation the store refuses) stops that ingestion as a whole: no
later page is read, and the next boundary starts again at the same cursor. Completed
windows advance the author-work watermark;
failed/dropped windows, and those a limit ended, retain their exact retry end. The third failure records an
unreviewed window before advancing. Confirmation attempts track each exact finding
revision separately and cannot clear, advance or replace an author-work retry window.
Exhausted or completed confirmation targets do not starve later author-work reviews.

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
at most 256 KiB. Document reads pin an exact immutable revision and verify its digest.
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

Confirmation snapshots explicitly grant the original finding evidence and response
anchors under their original evidence IDs, scoped to the same project/session. They
also pin claimed-fix and user replacement revisions from durable transition payloads,
even if the process crashed before optional feed observation. Those bytes count toward
the snapshot bound. Confirmation coverage describes the exact finding/fix groups and
their original and response evidence; unrelated pending author work cannot make that
scope incomplete. Confirmations consume no author feed and preserve its retry window.
A passed confirmation still requires a recorded checked claim. An omitted group remains
incomplete and is not counted as attempted.
If no confirmation target fits, an incomplete author-work window can still advance its
feed; the omitted claimed fix stays explicitly unconfirmed.

Reviewer tools are only `review_manifest` and `review_read`, `review_checked` (a checked-item record),
`review_finding` (finding creation), `review_decide` (confirmation/rebuttal decision) and `review_citation` (a quote's check, CR-4: `supported`, `unsupported` or `inconclusive`, with an explanation, for a quote's `source` item the snapshot holds, once the reviewer has read all of it and of the entry revision that quotes it; written to `citation_checks` with the digest of that revision's statement, fields, assumptions and evidence, and the tool and arguments the source came from (past 8 KiB of JSON, a clipped copy and the digest of the whole; a check that cannot be stored is the tool's error, not the review's failure). The rubric tells the reviewer that a source is what a tool returned, and may be a PDF an agent made unless the PDF is vouched for). Unknown or denied calls are recorded and consume the
model-turn budget. The reviewer has no worker client, author toolbox, file/network tools,
policy mutation or delegation. The separate call/hour, turn, time, response/context and
soft token budgets reserve before awaiting a model. Actual provider usage is recorded;
actual usage crossing the token budget makes the outcome capped, including a final
response. These are role budgets in calls, turns, time and tokens. The cost the gateway
reports for each call is kept with it and counts in the project's cost and its optional
daily cost limit ([run service](specs/run-service.md), RS-6a); the gateway's own budget
stays the hard cap.

## Findings, delivery and workspace

A reviewer's statement, remedy, confirmation explanation, checked statements and summary, and the main agent's response explanation and response body, hold no character the user may not see, as it is or as a character reference (`check_agent_text`, threat model A3): the user decides on them, and agents read them again. The check runs when they are written, after their line ends become line feeds; a reviewer whose summary is refused is told why and writes it again, once. The user's own explanations are kept as written. The finding card shows the statement, the remedy and the latest explanation a reviewer or the main agent gave, with its explanation source, exactly (`ApprovalText`) above the user's decision; the explanation is read when the user opens the response. Each step of the history's reason and each response body exactly under its rendering, with a banner beside any rendering that hides a character or a link (`HiddenBanner`); stored text from before the check reads the same way. A review's summary shows hidden characters as code points.

Findings retain severity, category, Markdown/LaTeX prose, exact anchors, remedy, revision,
reflags and append-only attributed transitions. Reflags can strengthen the current
severity/prose/remedy while retaining the previous
assessment in history, and a weaker reflag cannot downgrade that current assessment.
Main agents may claim fixes or rebut; only a confirmation reviewer or explicit user
decision can confirm a fix. User writes
use strict expected revisions and replay-safe operation IDs. A block's supersession
links old and replacement revisions and never claims that it fixed the finding.
A claimed fix may cite changed ledger entry revisions (`entry_revisions`, a later revision of an entry the finding is anchored to that changes the anchored field, and the entry's statement, fields, assumptions or evidence unless that field is its title, status or links); the confirmation snapshot pins each as a `ledger` item, and confirm/reject requires a whole read of it. A response without them keeps the digest it had before the field. A finding's anchor may name a field of a ledger entry revision the snapshot holds (`title`, `statement`, `status`, `fields`, `links` or `evidence`); supersession counts an anchor on a ledger entry as replaced only when a later revision changed what a decision rests on, and the anchored field when it names one ([ledger](LEDGER.md)). `review_finding_entries` keeps the entries each finding is anchored to, written by triggers, and an index finds the review evidence that holds an entry.
Confirmation decisions compare the pinned finding revision and current head before
writing. Reviewer confirm/reject also requires audited read ranges covering the finding metadata,
original/response anchored blocks (or whole items) and exact changed revisions. Empty slices of a nonempty source,
out-of-range and unrelated-prefix reads cannot satisfy that guard. Exact empty artifact
reads have a separate audited exposure marker. Typed changed-artifact references must
match a relevant logical artifact and an existing canonical session grant; unavailable
artifact content cannot be confirmed by reading its metadata. Large evidence may
therefore end capped or inconclusive. This records exposure, not model understanding.
Rejected fixes/rebuttals and author response rounds stop at configured bounds;
a claimed fix can remain visibly unconfirmed.
Generic reflags arriving after a fix claim or rebuttal append a deferred audit observation
without changing its revision or response payload. Only the pinned reviewer decision
can resolve that pending response.

Interrupt-threshold findings are delivered at main-agent safe points as bounded
untrusted data. An agent's own rebuttal, which leaves a finding open at a new revision,
is not delivered back to the session's runs: in lab 40 the agent read its rebuttal as a
new notice and rebutted again, seven times, until its run ran out of turns. A reviewer's
decision on it, or the user's response, is delivered. Other severity counts are delivered without their prose or anchors and
do not cause an extra author response turn. Notices are acknowledged only after a
successful model call received them and `ProjectStore.save_run` durably saved the run.
A failed model/save or a finding arriving on the last allowed model turn leaves it
pending. No findings automatically edit documents or weaken tool policy.

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
on the contradiction with that exact document revision. It remains claimed fixed until
the separate scripted confirmation runs:

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
original and changed grants through ScriptedModel, and records confirmation. It proves
plumbing and scoped evidence exposure, not model judgment. Remove the disposable
server and directory afterwards.

## Mechanical math findings

D82 math checks use the same finding/history and authenticated USER decision surfaces, with explicit `math` origin, a real mechanical check ID and no synthetic reviewer run. Reviewer categories/grants are unchanged and exclude this mechanical authority. Generic confirmation, reflag, supersession and threshold delivery paths cannot decide math findings. A separate bounded controller checks immutable document revisions and collects closing-only notices together with terminal findings, sharing at most two author response rounds. Accepted current formulas and complete parsed removal confirm mechanically; stale, skipped and unavailable evidence never does. USER takeover retains unresolved historical evidence while excluding new author reflags/notices. The checks cover document revisions, comment and reply bodies ([comment math](COMMENT-MATH.md)), MAIN fix/rebut explanations and ledger statements ([ledger](LEDGER.md)); details and limits are in [document math checks](MATH-CHECKS.md).

The [MAIN explanation math checks](FINDING-PROSE-MATH.md) cover only
agent MAIN fix/rebut explanation source. Its ordinary corrections preserve the
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
| `review_confirmation_targets` | The finding revisions a confirmation review checks, append-only |
| `review_calls` | Each reviewer model call's usage and prompt provenance |
| `review_records` | A review's checked items, findings and deferred reflags, append-only |
| `review_findings` | Findings with their current severity, category, statement, anchors, remedy, status, revision and origin (`review` or `math`) |
| `review_finding_entries` | The ledger entries each finding is anchored to, kept by triggers as findings are written and their anchors change ([ledger](LEDGER.md)) |
| `review_transitions` | Every finding revision with its status, actor and reason, append-only |
| `review_notices` | Findings due for delivery to the author, with their claim and delivery |
| `review_response_pins`, `review_response_discovery` | The explanation body revision each confirmation review pinned, and older responses held from checking because their author is unknown ([finding prose math](FINDING-PROSE-MATH.md)); the explanation bodies and their revisions are `text_revisions` of kind `response` ([living documents](LIVING-DOCUMENTS.md)) |

## Not built

- Shell-written artifact inventory and revisions (CRF-6), rich outputs and file contents as evidence, and ledger-specific evidence.
- Automatic idle turns, a reviewer that outlives a run, and a push view.
- Math checks of ledger fields other than statements and of reviewer, USER and platform prose (D82).
