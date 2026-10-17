# Research continuity

Date: 2026-10-16; local increments updated 2026-10-17. **Status: Draft with implementation increments.** The owner's continuity requirements (D87) and separate orchestrator sandbox (D47) are confirmed. Baseline: main `57e29536`. The journal, bounded tool retrieval/retention/export, delegated skeleton folds, live-parent waiting, active-tree environment claims, creation cleanup and observed model usage are implemented locally as described below. Durable work/attempt ownership, tree recovery, whole-run memory bounds and normal-launcher confinement remain proposed or incomplete. Local tests do not establish live acceptance.

## Purpose and confirmed requirements

The owner wants extended, semi-autonomous cryptography and mathematics research, organized into grounded research questions and several lines of investigation. Foundations take priority over the UI redesign. The owner confirmed that children remain tied to parents, parents should not end before their children, and interrupted work must be possible to resume at every level (D87). Resume is initiated through the orchestrator: the user must not have to visit and restart each agent separately.

The owner also accepted a restricted OpenShell sandbox for the agent-facing orchestrator, separate from workload sandboxes, with trusted lifecycle and enforcement services outside both (D47). Parent and child model loops may share the orchestration service. This adds a deployment boundary without changing their logical ownership or requiring one VM per agent.

A research question can remain open across many process lifetimes. Reaching a limit or losing a process says nothing about whether the question is answered or an approach is exhausted. Existing ledger entries, work records and plan tracks keep that scientific meaning; recovery must not invent a competing research board.

## Current implementation

| Area | At the baseline |
| --- | --- |
| Parent completion | `delegation/hook.py:closing` refuses a final answer while children run, but takes another model turn. Ending the parent for any reason cancels its running children through the hook's lease cleanup. |
| Restart | `service.py` and `delegation/records.py:abandon` mark abandoned attempts and delegations interrupted. A later main run can see earlier work, but this does not resume the delegated execution tree. |
| Context | `harness.py:_folder` excludes delegated runs. They use the configured character ceiling, 400,000 characters by default, with a report round near it. Main runs with stores can fold their model view. |
| Memory | `loop.py:Transcript.rebuild` changes the model view but retains the original messages and tool records. `tools.py:_run` puts the worker's execution record in a tool record even when the model reads only sampled output. |
| Final persistence | `harness.py:save` validates the complete run record and serializes it for storage. Merely spilling historical messages during a run would not bound this final step. |
| Worker recovery | The executor can survive worker/bridge disconnection. Recovery must inspect the actual execution and kernel state; an orchestrator restart and the loss of a VM are different events. |
| Idle suspension | The personal configuration sets `suspend_after = 3600`. `worker/suspend.py` considers worker activity and kernel CPU, without an orchestrator work lease. A long research interval without worker use can therefore stop the VM and lose kernel memory. |
| Cache layout | `history/view.py:settle` already places the state block before the fold region and raw tail. Every rebuild archives a new state block; its rendered header contains that new archive ID. The Responses adapter supplies a session cache key, which alone does not establish reuse. |
| Skill binding restrictions | `skills/allow.py` holds manually maintained variable names and families. Staging also asks the worker which candidate command names exist; this is a namespace-collision check, not generation of the blocklist from all installed tools. |

These are source findings, not a measurement of memory use or a new runtime acceptance result. Existing behavior remains authoritative until the corresponding implementation changes land.

## Proposed behavior

### Work and attempts

- RC-1. A durable work identity belongs to its project and session and, for delegated work, to its parent work identity. A run is an execution attempt at that work. Resuming creates a recorded continuation attempt; it does not overwrite an interrupted attempt or make its outcome successful. Reuse existing plan, delegation and ledger identities where possible. The implementation must settle the minimal schema before its migration.
- RC-2. An attempt records its work identity, predecessor, parent work, role, brief, model configuration, skill pins, policy identity, archive position, known operations, outstanding children, and outcome deliveries. Recovery uses the recorded role and inputs. A newer policy or unavailable dependency is checked and reported; recovery never silently widens authority or changes an independent check into a research task.
- RC-3. Only one attempt may actively own a work item. Restart and two simultaneous Resume requests claim ownership atomically. The previous attempt is fenced from new effects once its ownership is lost. Existing session ownership and worker operation identities remain in force.

### Parent and child lifetimes

- RC-4. A parent cannot be marked completed while a descendant has unfinished work. It consumes the children's terminal outcomes before completing. A child that returns partial findings because of a limit leaves unfinished work; ending that attempt alone is not completion of the delegated task. The parent may explicitly close a line of work with a recorded reason without claiming it was scientifically disproved.
- RC-5. A parent with no immediate work waits for child completion or user input without polling the model. Waiting consumes no model turns. Progress updates do not mark it completed. Ownership renewal and cancellation remain responsive while waiting.
- RC-6. Exhausting a parent's model-turn allowance pauses its own model work; it does not itself cancel children with remaining authorized resources. The parent retains responsibility and can receive their durable outcomes while paused. Integrating them may require a resumed parent attempt. A shared spending limit prevents new model calls throughout its scope; waiting and saving results do not require paid calls.
- RC-7. Explicit Stop and unexpected interruption have distinct durable reasons. Stop applies to the selected subtree, keeps partial evidence, and does not restart automatically. Recovery of a parent does not revive an explicitly stopped child. An unknown outcome stays unknown until reconciled; it is never silently relabelled cancelled or complete.

### Recovery

- RC-8. Recovery is available for main, child and grandchild work through one orchestrator-level Resume action. The orchestrator discovers and reconciles the selected work tree recursively; the user need not restart individual agents. It reconstructs ownership and the dependency tree before dispatching new work. Children that completed during an interruption are read and acknowledged, not started again. A parent is not declared ready to finish until outstanding results and deliveries have been reconciled. The orchestrator presents one aggregate recovery result with any blocked branches and their reasons; optional branch selection is an advanced control, not a prerequisite. This entry point does not decide whether recovery should also run automatically after a crash.
- RC-9. Recovery checks every pending operation by its recorded identity before deciding what to do next. If the worker still runs it, reattach or wait. If it completed, retrieve that result. If its outcome is unknown, keep it unresolved and report the uncertainty. Do not repeat an operation with possible effects merely because its response was lost. Any retry uses that operation's existing idempotency contract; where there is none, automatic replay is unavailable.
- RC-10. Persist operation identity and intent before dispatch, and persist results before acknowledging their delivery. Cover the crash windows between those steps explicitly. Reconciliation of model calls does not promise to recover an unavailable response or avoid upstream charges; ambiguous charges remain visible and the current budget is checked before another call.
- RC-11. Saved files and research records survive independently of process memory. Reattach to a surviving kernel only after verifying its identity and generation. A lost kernel is reported as lost; restore only from an available, recorded checkpoint or recompute explicitly identified work. No promise of live process migration is made. Child-kernel cleanup must account for resumable work rather than using the end of an attempt as the only lifetime boundary.
- RC-12. Restore pending questions, answers, tool results and child outcomes with their identities and delivery positions. An answer already received is not requested again, and a result already incorporated is not counted twice. The current message queue's Resume behavior alone is not sufficient evidence that this requirement holds.
- RC-13. Resume respects current policy, available model capabilities, and current spending limits. The original and continuation attempts retain their own configuration and usage records. A deliberate model or skill change is recorded; it is not presented as an exact continuation under the old configuration. Resuming never resets accrued cost or grants a fresh spending allowance by itself.

### Bounded memory and preserved evidence

- RC-14. Persist original messages, tool records, provider-specific state and output references incrementally. The active process retains a bounded working view, bounded caches and the operations it is currently handling. Folding a model view and evicting historical payloads from RAM are separate operations. Eviction occurs only after the evidence has been durably saved.
- RC-15. Full evidence remains retrievable by stable references and bounded reads. Do not replace the only copy with a summary, or remove exact values, code, source passages or provenance to reduce memory. Preserve the existing distinction between full output and a sample, including the worker's own truncation or collection gaps.
- RC-16. Run startup, view rebuilding, finalization, review dispatch, status queries and exports must not reconstruct the entire run in memory. Save summaries and references incrementally; page or stream historical material. Existing consumers of `RunRecord.messages` and `RunRecord.tools`, including the review controller and event log, need explicit compatibility handling. An old record that exceeds a read's bound must produce a visible bounded-read result rather than an unexpected process allocation.
- RC-17. Define measured per-run working-memory limits and aggregate concurrency limits before claiming acceptance. Disk/archive growth has separate accounting and retention policy. Storage exhaustion stops new effects with a visible persistence failure; it must not silently drop evidence and continue. Small per-tool output limits alone do not establish bounded run memory.

### Delegated context

- RC-18. Delegated tasks and checks can rebuild a bounded model view from their own archived work. Use the existing archive and fold mechanisms with role-specific views rather than a second summarization implementation. Independent checks retain their context restrictions; folds do not import a parent's verdict or reasoning that the check was not permitted to see.
- RC-19. A folded or resumed view preserves the brief, current constraints, exact-value references, outstanding operations, child relationships and unresolved questions. The model can retrieve original evidence. A context limit produces a resumable pause or a bounded report, not a claim of completion. Continuation remains within the existing scope and resources.

### Follow-ups included in the review

The owner requested these three additional review items on 2026-10-16. They are proposed engineering work, not approved changes to context order or evidence retention.

- RC-20. **Cache reuse at rebuilds.** Review CM-25/CM-32 against the requests the adapters actually send. The state block is already near the top; its contents and new archive header can change an early prefix at every rebuild. For OpenAI, reuse requires a matching rendered prefix, and a session key does not guarantee a hit ([official caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching), checked 2026-10-16). Compare the current order with retaining stable history before a freshly appended state block, and with reusing an identical immutable state snapshot where provenance permits. Do not reuse an old ID for changed content or give summary text a higher-authority role. A fold that replaces earlier history can invalidate the prefix even after the state block moves; no layout promises a warm first call in a new session or after cache expiry.
- RC-21. **Measure the cache tradeoff.** Separate a session's first-ever call, a later run's first call, ordinary appended turns, and the first call after a fold. Compare serialized prefixes locally, then measure provider-reported cache reads/writes, fresh input, latency and cost through the configured gateway, separately per model family. Include tool-schema changes, stable versus changed state content, rebuilt archive headers and cache expiry. Check whether the actual Claude breakpoints match CM-32 and what the deployed gateway preserves. Keep the existing continuity probes: cheaper requests do not justify stale constraints, altered role boundaries or reduced research accuracy. Cache placement and literal output stubbing must be evaluated together.
- RC-22. **Trimming stored tool output.** Address three copies separately: the model's working context, payloads resident in RAM, and durable evidence. Deduplicate large output in durable storage and keep a bounded preview plus immutable references in run records, rather than copying full output into the archive, events and final run JSON. Read, review and export full evidence on demand in bounded chunks. Automatic trimming does not delete the only full evidence copy. Any future destructive retention policy requires a separate decision and must report its gaps. Measure serialized record growth and finalization peaks as well as steady-state RAM.
- RC-23. **Generate the skills variable blocklist from the worker tool environment.** Extend the image-build inventory to produce a deterministic, versioned manifest of reserved tool namespaces and known environment-variable names/families, with the package/version and extraction source behind each entry. Base it on the research image and installed environment, not the orchestrator host. Cover runtime provisioning as a revision of that inventory, so a base-image manifest is not silently treated as current after packages change. Prefer build metadata and static inspection; never execute skill-pack helpers or read credential values to discover names. Extraction is incomplete by nature: computed names and undocumented tools need explicit coverage notes and maintained fallback rules. A command-name list alone is insufficient.
- RC-24. **Keep the binding authority rules.** Generated entries supplement the existing deny rules and command-collision checks; they do not replace the pack namespace and explicit-approval rules of SK-31/SK-33. Absence from the generated list is not permission. A missing, stale or malformed inventory must not weaken the existing restrictions, and an untrusted skill cannot remove reserved names. Include tests for tool additions and upgrades, deterministic regeneration, environment revision changes, reserved-name collisions and ordinary permitted pack bindings. The manifest's provenance and its delivery to the harness need a reviewed contract before implementation.

### Runtime lifecycle findings

The [OpenShell review](../../.agents/docs/agent-reports/openshell-foundations-review-2026-10-16.md) distinguishes existing worker reconnect support from the research-tree recovery proposed here.

- RC-25. **Work-aware suspension.** An active research tree can retain a bounded, renewable claim on an environment even when its model is reading literature or waiting for a child and the kernel uses no CPU. Define expiry, pool limits, explicit Stop and checkpoint/release behavior. A process crash cannot retain resources forever, and ordinary idle detection cannot silently discard a live tree's required kernel state. Resume reports any state already lost. This is a proposed implementation of continuity, not permission to disable every idle or resource limit.
- RC-26. **Uncertain sandbox creation and cleanup.** Persist each original sandbox identity and cleanup obligation across failed creation, timeout and restart. A delete request or CLI exit is not proof of removal. Reconcile pending creation/deletion before replacement, and keep unresolved cleanup visible to the orchestrator. Do not overwrite the only record of a possibly live sandbox while retrying creation.

### Orchestrator boundary

- RC-27. **Separate confinement (D47).** The normal launcher runs the agent-facing orchestrator in its own restricted OpenShell sandbox, separate from generated-code workloads. The trusted worker controller and OpenShell gateway, supervisor and credential services remain outside both. Parent and child model loops may share the orchestration service. Research execution continues through the worker API; neither orchestrator failure nor sandbox startup failure enables a host-execution fallback. This is the accepted target architecture, not current launcher behavior.
- RC-28. **Scoped authority (D47).** Permit only the model gateway, policy-governed MCP proxy, scoped worker API and required scoped state service. Keep the orchestrator's software read-only and writable state explicitly scoped. Give it no engine/cluster/OpenShell administrative credentials, management sockets or broad host mounts. The worker independently validates project ownership, permitted policy revisions, operations and limits; an orchestrator request cannot widen its authority. Reuse the existing image and policy compiler, checking their current dependencies and production callers rather than treating the old lab as current acceptance.
- RC-29. **Durable state and launcher integration (D47/D87).** Settle the storage and access contract before moving the run service: the current launcher lets the UI and orchestrator share a control store. Task ownership, checkpoints, evidence references and delivery positions must survive orchestrator process and sandbox replacement, remain accessible to the UI through a scoped boundary, and be recoverable by the replacement orchestrator. Use existing service/storage seams where possible; do not expose the host's whole state directory to solve the shared-store dependency. Define sandbox readiness, service access, shutdown and replacement behavior. Orchestrator replacement does not by itself delete workload environments; one Resume reconciles the existing task tree and worker executions.

## First implementation increment: durable tool journal

Control schema 58 adds immutable `tool_intents` and `archive_tool_records`. A stored
run writes its exact toolbox operation ID, original call ID/name/argument text, run
identity and execution context before entering the toolbox. This applies to parent
and delegated loops through `RunArchive`. A failed intent write prevents dispatch;
an existing intent, even with identical arguments, is refused pending reconciliation.
An intent without a saved result means **uncertain**, including a crash before actual
dispatch. It does not establish that the operation ran or authorize replay. The stored
ID is the toolbox ID: worker, background-job and connector adapters can have different
sub-operation identities and still need their own reconciliation implementations.

Every tool result, including a report round's refusal without dispatch, saves the full
structured tool record in the same transaction as its archive message and read
register. The model's sampled text stays distinct. The record is committed before
observer hooks, UI events and the next model call; it remains available when no final
`RunRecord` was saved. A failure in that transaction rolls back both result and message,
leaving any dispatch intent unresolved. Intent and result writes use the existing
bounded busy retry and owned-thread cancellation behavior. No result acknowledgement
or retry is inferred from a missing final record.

`ToolJournal.pending` pages identities without reading argument/output payloads. Full
intent and result reads check stored, schema-validated byte counts before loading
values (default 1 MiB), without asking SQLite to read the payload to measure it. The
existing authenticated workspace API exposes one record at
`GET /api/v0/projects/{project}/messages/{message}/tool-record`; an oversized record
returns 422 with byte counts, and an older message with no journal record returns 404.
The migration preserves older evidence without synthesizing intents or results.

This increment does **not** yet evict historical RAM, deduplicate the legacy final run
record, persist model-call intent, resume the work tree, or move the orchestrator into
OpenShell. It supplies durable tool evidence for
those later steps. Tests use benign in-process arithmetic, storage failures and
cancellation; they do not establish a live OpenShell boundary or full crash recovery.

## Bounded tool-record retrieval

Full structured results can be retrieved in pages without materializing the complete
record. `Archive.tool_record_chunk` resolves the project/message reference to the row
inside a read transaction, then uses a read-only SQLite incremental blob handle on
the stored JSON text. It closes that handle and transaction before returning bytes.
Each read is at most 65,536 bytes. It reads the stored UTF-8 representation directly;
it does not select, cast, parse or re-encode the entire payload. No schema migration or
copy of existing evidence is required. This follows SQLite's [incremental I/O
contract](https://www.sqlite.org/c3ref/blob_open.html), which permits read-only access
to TEXT and BLOB columns.

`GET /api/v0/projects/{project}/messages/{message}/tool-record/chunks` accepts `offset`
(default 0) and `limit` (default 65,536, range 1–65,536). Offsets are byte offsets, not
character positions. The typed JSON response contains the message ID, offset, total
bytes, next offset (null at EOF), and base64-encoded bytes. Consumers decode each page
and concatenate bytes before parsing UTF-8 JSON; a page may end inside a Unicode
character. The immutable message reference allows an interrupted reader to retry the
same page. An offset equal to the total returns an empty final page; a larger offset,
unknown or duplicate parameters, malformed integers or a request body returns 400.
Missing or differently scoped records return 404, including legacy messages without
a journal record. Existing authentication and no-store response rules apply.

Acceptance for this retrieval increment: exact bytes and SHA-256 after closing and
reopening the store, repeated pages, multibyte boundary cuts, and bounded reads from
small and 10-times-larger records. In fresh reader processes, the additional Python
allocation peak must stay below 1 MiB and the additional peak RSS below 8 MiB while
hashing all pages of each fixture. This gate measures retrieval only: whole-run RAM,
concurrent-reader admission, record deduplication and bounded finalization remain
separate requirements. No live OpenShell behavior is claimed.

Local validation on 2026-10-16: 820 affected tests pass, with Python and web static
checks. The 2 MiB and 20 MiB fixtures required 33 and 321 pages; their additional
Python allocation peaks were 144,810 and 175,914 bytes, and peak RSS grew 2,072,576
and 2,138,112 bytes. Both reconstructed hashes matched. A positive control using the
full-read path on the 20 MiB fixture exceeded both budgets (41,946,627 Python bytes,
40,280,064 RSS bytes), confirming that the gate detects whole-record loading.
Independent review and integration remain pending.

## Discovering a run's tool evidence

The local discovery increment adds `Archive.tool_records` and the authenticated read route
`GET /api/v0/projects/{project}/sessions/{session}/runs/{run}/tool-records`. It lists
metadata for archived tool results, including results saved before an interruption
with no final run record. It reads neither result payloads nor message contents.
Each entry has the archive message ID, session position, toolbox operation ID (null
for a result without a dispatch intent), and record byte size. A null byte size
explicitly marks legacy messages with no full record in the journal.

`after` is an exclusive session-position cursor (default 0); `limit` is 1–100 (default
100). Entries are ordered by position. The reply includes `next_after` (last returned
position, or the incoming cursor for an empty page) and `has_more`, computed within
the page's read transaction. Readers can continue after appends without retaining
previous pages. This is a live enumeration, not a completion or recovery claim.
An empty page for a valid run name in an existing session means only that no matching
tool messages were found; it does not prove the run exists or has finished. Missing
projects/sessions return 404. Malformed scopes, paging values, duplicate or unknown
query parameters and request bodies return 400. The existing byte-page route retrieves
each full record by the returned message ID.

Tests cover reopening without a final run record, appends between pages, legacy
gaps, project/session/run scope, and the fixed 100-entry page bound. The fresh-process
memory fixture also enumerates metadata for the 2 MiB and 20 MiB records under the
same 1 MiB Python / 8 MiB RSS additional-allocation gates used for byte retrieval.
The combined local gate passes 845 affected tests, including the workspace package;
Python/web static checks pass. Independent review and integration remain pending.

## Retaining bounded structured records

The local retention increment bounds each historical structured tool record, separately
from model messages and per-run counts. After a successful archive append, a record
larger than 64 KiB (or deeper than the bounded preview walker supports) retains at most
16 KiB of preview JSON including an immutable project/message reference. Small records keep
their existing representation. Whole small fields are retained within the preview
budget; omitted fields and shortened tool/argument text are explicitly marked. The
model's sampled result and original call messages are unchanged. The full record stays
in the journal and is retrieved with the existing bounded APIs.

Live observers and the on-tool callback receive the original current record before
it is replaced, including when an observer fails or is cancelled. A run without an
archive keeps its original inline evidence. The saved run and event fallback recognize
the preview, and the workspace labels it as a preview rather than a full record.
Acceptance checks the real tool loop, failure/cancellation boundaries, exact evidence
after reopen, and final save without reloading original payloads. This does not yet
bound retained model messages, the number of per-call summaries, review caches or
total run memory; those remain subsequent work.

The preview marker is `_record_storage`, with format `tool-record-v1`, project and
message IDs, an omitted-field count, shortened header names and compact outcome
metadata. Each retained nested field is complete, at most 4 KiB, within a shared
16 KiB budget; at most 64 top-level fields are considered. The reference is created
only after commit and never points at a replacement or re-encoded original. Previews
are detached snapshots so an observer cannot change their content after measurement.

The deterministic memory gate runs four and forty 1 MiB results in fresh processes,
through the real tool loop, archive, validation and final save/export. Its fixed limits
are 12 MiB additional Python peak allocations, 32 MiB additional peak RSS and a 256 KiB
final record for this fixture. The inline positive control must exceed 32 MiB of both
memory measures and retain more than 40 MiB of final JSON. The [local report](../../.agents/docs/agent-reports/tool-retention-2026-10-16.md)
records the measurements and compatibility tests. This isolates structured output
retention; it is not the complete RC-F9 long-history/folding acceptance.

## Acceptance

Use deterministic, benign fixtures first: arithmetic, reading local text, and writing uniquely identified files in disposable directories. No live research or production records are needed to establish lifecycle correctness. Tests use public APIs and injected clocks, as the repository conventions require.

| Fixture | Required observation |
| --- | --- |
| RC-F1 Parent waiting | A parent whose own work is finished waits on two children without further model calls. A child finishing wakes it; completion requires both outcomes. User messages and Stop still reach it. |
| RC-F2 Parent limit | A parent's last allowed call occurs while a child has remaining resources. The parent remains unfinished and owns the child; the child's result survives. Resuming permits the parent to incorporate it. |
| RC-F3 Three-level restart | Interrupt root, child and grandchild at different boundaries. Resume reconstructs the same work tree with linked continuation attempts and no duplicate active owner. |
| RC-F4 Completed child | A child completes and its result is durable just before the parent stops. On resume it is delivered once and the child is not relaunched. |
| RC-F5 Lost operation response | A fixture writes one identified file and its response is lost. Reconciliation finds the completed operation, with one write; an operation without a conclusive outcome stays visibly unresolved. |
| RC-F6 Duplicate Resume | Two clients request resume concurrently, and an old attempt tries a late effect. At most one current attempt proceeds; the old owner is refused. |
| RC-F7 Explicit Stop | Stop one child, then interrupt and resume its parent. The stopped child stays stopped and its partial results remain readable. |
| RC-F8 Kernel outcomes | Test a surviving kernel and a lost kernel separately. Resume identifies the correct generation or reports state loss and the available checkpoint; no hidden-state continuity is asserted. |
| RC-F9 Long history | With the same working-view ceiling and per-call payload, increase history by at least 10x. Measure RSS and Python allocations through run startup, multiple folds, review dispatch, final save, history reads and export. Historical payload growth must stay on disk rather than grow resident memory proportionally. Record numeric budgets before running the gate. |
| RC-F10 Evidence after eviction | Retrieve known early, middle and late output values, code and source text after eviction and restart. Their bytes and hashes match the originals; existing truncation is still labelled. |
| RC-F11 Delegated folds | Each delegated role crosses several folds and resumes afterward. Exact values, changed assumptions and unfinished questions survive; an independent check never receives prohibited parent context. |
| RC-F12 Resource failure | Exhaust storage and the shared model budget separately while children run. Saving and waiting are correctly classified, no new unauthorized effect occurs, and replenishing resources does not silently restart explicitly stopped work. |
| RC-F13 Rebuild cache comparison | Replay identical inputs under each candidate layout. Record which serialized prefix changed and why; live measurements distinguish cache reads, writes and missing usage data. Later-run starts and folds are measured separately, and continuity probes still pass. No predicted prefix reuse is labelled an observed cache hit. |
| RC-F14 Output deduplication | A large tool result remains readable byte-for-byte after previews are trimmed and the process restarts. Reviews, history and streamed export resolve the same scoped immutable reference. Durable record size and memory stay bounded without duplicate full payloads. |
| RC-F15 Generated binding inventory | Build the same tool inventory twice, then change a fixture package/version and provision another tool. Generation is reproducible, changed coverage is visible, existing denied variables remain denied, legitimate pack bindings work, and missing/stale inventory grants no additional binding authority. |
| RC-F16 One orchestrator Resume | One action resumes an interrupted three-level tree containing a completed child, an interrupted child, an explicitly stopped branch and a temporarily unavailable environment. Eligible work resumes without agent-by-agent input, completed work is not repeated, stopped work stays stopped and the orchestrator reports the blocked branch. |
| RC-F17 Research without kernel CPU | Advance a fake clock past the idle threshold while an authorized tree holds an environment for pending research. Its required kernel is retained; expiry, explicit release and pool limits follow the recorded policy. Restart distinguishes a surviving kernel from state already lost. |
| RC-F18 Creation cleanup | A create times out while the backend continues, then deletion fails or remains pending. Restart and retry retain the original sandbox identity and cleanup obligation; no environment is reported cleanly deleted until absence is confirmed. |
| RC-F19 Separate orchestrator boundary | The production launcher puts orchestration and research execution in distinct sandbox identities. Approved service calls work, prohibited destinations and ungranted files are denied, project-scoped worker requests remain enforced, and sandbox startup failure does not execute research on the host. Use harmless fixture endpoints and files. |
| RC-F20 Orchestrator replacement | Replace the orchestration sandbox while a child computation continues in a surviving workload sandbox. Durable task state and UI history remain available. One orchestrator Resume reconnects to the existing attempt, incorporates its result once and preserves explicitly stopped branches. No administrative credential or broad host mount is needed. |

A real OpenShell restart test is required for claims about RC-F5 and RC-F8 at that backend. A scripted gateway is sufficient for lifecycle accounting, but cannot establish research-quality preservation through folds. Extend the existing continuity probes before claiming that latter property. Report prepared, implemented, deterministic-test verified and live verified separately.

## Implementation sequence

### Delegated folding contract

A delegated task or check uses the existing folding engine, restricted to that
attempt's archive run identity. The restriction applies to view rebuilds and to
the spans loaded for overview enrichment. Every derived state block, fold and
spill retains the parent run identity, so it cannot enter a main run's view.
The child's original brief is pinned in its state block at every rebuild. Tasks
also retain their permitted standing constraints; checks receive no parent
digest, ledger retirement judge, parent folds or sibling conversation. Model
ceilings are resolved for the child's selected alias, including shared
input/output windows. Explicit input limits and spending limits still apply.

The initial delegated implementation uses the existing deterministic skeleton
folds. It makes no enrichment calls outside the child's token allowance.
Enriched delegated folds require shared accounting before they can be enabled.
These changes extend an attempt's working context; they do not by themselves
implement continuation attempts, bound historical RAM, or establish research
quality preservation. Those remain separate acceptance requirements.

### Stored-evidence download contract

The workspace exposes a read-only download of one run's stored evidence, including
unfinished runs with an archive or tool intent. `sandbox-run-evidence-v3` is UTF-8
NDJSON: a manifest, scoped database rows, each text/blob field in at most 64 KiB
base64 chunks, field checksums, and a final stream checksum. The manifest records
the immutable row cutoffs captured together before export. Later appends are not
included. Every field retains its SQLite storage type, so JSON text, Unicode,
NUL bytes and native provider payloads can be recovered exactly. A download without
its verified final line is incomplete. The offline verifier requires each version's
complete table field set and rejects unknown or duplicate fields before retaining
their names, so malformed rows cannot grow its working memory without bound.

This bundle contains the final run JSON when present, archive messages and saved
text samples, tool intents, full structured tool records, starting inputs and
observed model usage. It does not claim to
include external worker files, document revisions or referenced earlier runs:
their references and existing truncation/omission metadata remain intact. It is
an evidence interchange, not a restore instruction or permission to replay work.
Queries and chunks have bounded memory; no read transaction is held across network
backpressure. Concurrent downloads are limited by the workspace. Export failure or
client cancellation releases the slot, and never writes to the source database.

### Parent waiting increment

While a parent still has room for another model call, an attempted final answer
with running children waits on those children in the existing session lease.
It makes no model calls while waiting. Child completion supplies durable outcomes
for the parent's next call; new user input wakes the parent early, and Stop cancels
the wait through the existing subtree cancellation path. Waiting start/end events
make this visible in chat. At a turn/context/budget limit, a finalization hook keeps
the parent's existing session and environment leases until its children finish and
save their outcomes. Children receive their configured role turn allowance, independent
of the parent's remaining calls; token, spending and concurrency limits remain in force. No additional parent model calls occur. New messages remain
queued for a later run, and Stop still cancels the subtree. The limited attempt keeps
its limit classification; unconsumed child outcomes are not acknowledged by waiting.
This covers live ownership through a limit, without implementing durable paused-work
identity, continuation attempts, recovery after process loss or redefining a limited
child as completed work.
If a child process ends but its durable outcome could not be saved, closing stops
with `child_outcome_unavailable`; an empty live-task set is not completion evidence.

### Creation cleanup contract

A managed environment keeps its original `creating` record until its first observed
sandbox identity has been removed and the gateway confirms absence. Readiness polling
saves that identity before accepting or rejecting the phase. Retry reconciles this
record before allocating a replacement name, and explicit deletion uses the same
identity check. Delete failure, a still-present object or a changed identity leaves
the obligation durable and visible. Cancellation cannot detach an active cleanup.

If creation's response was lost and the gateway has not yet exposed an identity,
absence alone is inconclusive: the original request could still create it later.
The worker retains `creating` and refuses replacement or a claim of successful
deletion. A later retry can reconcile the original object once it appears. A request
that never appears needs operator reconciliation of its upstream outcome; the worker
does not infer a safe retry from elapsed time. This deliberately favors a visible
blocked environment over losing an uncertain compute allocation.

### Explicit Stop and interrupted cleanup

An accepted Stop is stored before signalling cancellation. The existing immutable
delegation ancestry applies that intent to every descendant; another session or
sibling subtree is unaffected. Starting a child checks the parent's Stop in the
same transaction that admits the child, closing the Stop/admission race. Recovery
of a dead attempt retains this distinction even when no final record was saved.
Saved outcomes stay immutable.

Cleanup after a lost lease, parent failure or service interruption still cancels
live child tasks and saves their partial records, but labels them `interrupted`.
Only an explicit root Stop or `cancel_delegation` makes their outcome `cancelled`.
Live cleanup checks each child's durable Stop ancestry even if interruption occurs
before the service polls or before a committed child Stop reaches its signal.
Children that have not begun are cancelled before awaiting that read. Cancellation
or a read failure cannot bypass child cleanup; an unreadable state is surfaced as
an error after cleanup with the available signal.
A child that fails to stop within the cleanup deadline remains `interrupted`, with
its requested stop reason preserved; a request alone does not prove effects ended.
This is the reason classification prerequisite for tree Resume, not a Resume path.

### Immutable starting inputs

Before a stored run's first model call, a mandatory start hook saves a versioned
snapshot of its resolved request, execution context, selected model alias, harness
settings and prompt/tool/memory/skill pins. Root, child and grandchild runs all use
the same hook. The snapshot excludes process-only cancellation/deadline objects,
credentials and the historical transcript; the archive keeps the conversation.
It is at most 1 MiB, scoped by project/session/run, and cannot be changed or deleted.
A failed write or conflicting run identity prevents model and tool dispatch.

Schema 59 adds this observational record without inventing starting inputs for older
runs. Streamed evidence export includes it, even if the attempt never saved a final
record; the verifier also accepts the earlier v1 export format. These inputs are evidence for recovery, not authority to replay work or an
exact kernel checkpoint. Recovery still needs work/attempt ownership, policy and
dependency revalidation, pending-operation reconciliation and delivery handling.

### Child persistence failure

A child whose final record or delivery acknowledgement cannot be saved reports a
failed attempt with `persistence_error`, retaining its partial report and withholding
the structured successful result. A completed model answer alone is insufficient
to report `returned`. Already committed evidence remains intact; this classification
does not authorize replay of effects or pretend an acknowledgement succeeded.

### Bounded legacy run reads

Materializing a saved `RunRecord` defaults to an 8 MiB byte limit. SQLite incremental
I/O checks the stored byte length before loading it, so refusal does not allocate the
legacy JSON value in Python or SQLite. An oversized record returns a visible
`unsupported` result directing the reader to streamed stored-evidence export. Explicit
offline readers may opt out of the default; the workspace retains its own bound.
Rebuilds propagate the refusal instead of silently discarding legacy user constraints
or stored model/fold limits. This does not bound final-save serialization or metadata queries that inspect old JSON.

### Streaming folded-history reads

View rebuilds select fold records and spills independently from raw messages. Folded
model/tool payloads are read one row at a time only when a failure marker needs a
later-success check, then discarded; matching keeps argument digests and message
positions. The raw tail and the user's preserved words retain their existing
rendering and role boundaries. Scope selection happens in SQL, before payloads are
loaded, so a parent's rebuild does not read a child's payloads. Exact evidence stays
in the archive. This removes folded-output accumulation at rebuilds; the live
transcript, fold metadata and preserved user words still need their own memory bounds.

### Active-tree environment claim

Each stored root attempt holds one renewable claim for its session and environment;
its descendants share that claim while the root owns their lifetime. The worker
stores the claim with the sandbox ID, a unique holder, and expiry. A claim lasts
120 seconds, renews every 30 seconds, and the API refuses durations above 300 seconds
or more than 32 active claims in one environment. Claims keep idle suspension from
discarding kernel state during model-only work. They do not start a stopped VM,
override explicit deletion or promise recovery of lost kernels.

The worker checks project ownership and the current policy before a grant or renewal.
A renewal pins the original sandbox ID and cannot protect a replacement silently.
The root stops when renewal fails; a crash leaves at most the unexpired claim.
Release is scoped to the original holder/session and does not release another root's
claim. Claims survive worker restart; suspension checks them again under the same
environment lock immediately before stopping. Unmanaged backends explicitly report
that they have no managed idle suspension. This is an active-attempt safeguard;
durable paused-work ownership and orchestrator-level Resume remain separate work.

Each step is split into reviewable changes with a production caller; no unused recovery framework is merged.

1. **Durable evidence, state access and bounded memory.** Characterize current consumers and settle the UI/orchestrator storage boundary for RC-29, then add incremental storage, output deduplication and bounded historical reads. Replace the full-record finalization path and verify RC-F9/F10/F14. This is independently useful and does not depend on the automatic-resume decision.
2. **Work ownership and waiting.** Settle the schema, add work/attempt relationships and environment claims, and replace model polling with durable waiting. Cover RC-F1/F2/F6/F7/F17 before changing cancellation cleanup.
3. **Explicit recovery throughout the tree.** Reconcile operations, sandbox lifecycle and kernels, recover child outcomes and pending input, and expose the orchestrator's single Resume action through the existing control surface. Cover RC-F3 through RC-F8, RC-F12/F16/F18. Do not begin with automatic crash recovery.
4. **Confined orchestrator in the normal launcher.** Integrate the existing image/policy with the durable state boundary, scoped service access and trusted lifecycle ownership. Verify RC-F19/F20 through the production launcher; the old isolated harness lab is a reference, not proof that current chat, UI and recovery paths work there.
5. **Delegated folding and cache layout.** Extend the existing context machinery with role-specific views, and test RC-F11 with the existing continuity probes. Measure RC-F13 before selecting a revised layout; measurements can begin earlier. Scope and budget rules remain unchanged.
6. **Combined acceptance.** Run bounded-memory, interrupted-tree, orchestrator-replacement and role-context scenarios together, followed by the OpenShell cases. Resume UI redesign after the foundational behavior is demonstrated.

**Independent follow-up:** generated skill-binding restrictions (RC-23/RC-24/F15) can proceed without changing the task lifecycle. Keep its manifest contract, image/provisioning changes and binding enforcement in separately reviewable steps.

Changes belong in existing storage, lifecycle and hook seams, not new subsystems inside `loop.py`. Update affected as-built documents only as each implementation lands. No running personal instance is migrated as part of this draft.

## Observed model-usage increment

Implemented locally (RC-10/RC-13/RC-14): schema 60 adds immutable
`archive_model_usage`, one bounded usage record per answered main/delegated call,
with project/session/run, one-based response ordinal, role, observation time and
the archived assistant message ID. The response (including native data) and usage
commit together before observers, delivery acknowledgements or tool dispatch.
A failed write fails the run. Schema 59 data is retained without fabricated usage.

Budget queries prefer this journal for each response ordinal, excluding its copies
in a final run record or assistant event. Unjournaled ordinals still use the saved
record, or its event when no final record exists. Existing event `turn` counts
answered responses and aligns with the usage list's one-based index. This keeps
legacy and partially persisted records usable without double charging.
Journaled calls use observation time for the daily window, including a call made
after a run has been active for more than a day. Legacy records keep their existing
run-start cutoff because they have no trustworthy per-call time. Unknown reported
cost stays unknown and retains the existing budget refusal rule.

The evidence download is v3, adding the usage table; its verifier still accepts
v1/v2 with their original field sets. This increment does not journal pre-dispatch
model intent or recover a response/charge lost before observation; those remain
explicit recovery work. No call is replayed and no upstream charge is invented.

The [implementation report](../../.agents/docs/agent-reports/model-usage-2026-10-17.md)
records interruption, migration, mixed-source accounting and daily-window checks.

## Open decisions

1. **After an unexpected restart:** automatically resume eligible work within its existing authorization and budget, or leave it paused for an explicit Resume. Asked of the owner; pending. Recommendation for the initial implementation: explicit Resume. Automatic recovery can follow once the same reconciliation path is proven. Explicit Stop stays stopped in either choice.
2. **Implementation measurements:** choose numeric memory and concurrency budgets from a bounded baseline and the supported deployment profiles. This is engineering work, not a request for the owner to pick a RAM constant.

The parent's responsibility, orchestrator-led recovery at every level and separate orchestrator sandbox are confirmed requirements, not open questions. Proposed state names and schema details above are not approved or implemented merely by this draft.

## Sources

[Product](../PRODUCT.md), [architecture](../ARCHITECTURE.md), [projects and sessions](../PROJECTS-AND-SESSIONS.md), [plans and delegation](plans-and-delegation.md) PD-15/PD-22/PD-36 to PD-40, [run service](run-service.md) RS-6f/RS-12, [context management](context-management.md), [worker recovery](../WORKER.md#sandbox-daemon-and-recovery), and the owner's requirements of 2026-10-16 (D47, D87).
