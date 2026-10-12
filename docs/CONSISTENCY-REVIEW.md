# Automatic consistency review

## Implementation status (2026-10-12)

The automatic reviewer is implemented independently from the approved
[behavior specification](specs/consistency-review.md), with a separately authored,
versioned rubric (`sandbox-consistency-1`, D56). It never executes code. The historical
planning review below remains historical context, not a description of the runtime.

The control application keeps `review_` tables in the existing `control.sqlite3`.
Observations, snapshots, evidence grants, tool reads, model usage, checked items,
findings and their transitions are durable. A fenced lease permits one live reviewer
per session across harness processes. Recovery drops only expired owners. A stale owner
cannot read through a reviewer grant, spend budget, raise a finding or finalize a review.

### Running and scheduling

With `--state-dir`, the harness observes its transcript, canonical execution projections,
agent-authored document revisions and publication metadata. The reviewer uses a distinct
`--reviewer-model ALIAS` with the existing model gateway, or `--reviewer-script FILE`
plus that alias for synthetic validation. The adapter limits each answer to 2,048 tokens
and 64 KiB before the reviewer applies stricter response bounds. No alias makes zero
reviewer model calls; required windows end failed, retry twice, then become visibly
unreviewed. This never becomes a pass.

Checkpoints start at live safe points after the configured minimum interval and document,
artifact, kernel-cell or manual-request thresholds. The terminal rule classifier records
its version, completion-claim match, durable run effects, open/claimed-fixed finding
states and document ingestion coverage. Ordinary conversation without those triggers
skips terminal review; failed/interrupted author loops do not run a terminal reviewer.

Terminal holds process at most three bounded windows. Each window pins a contiguous
feed prefix; unseen overflow retains its cursor. Terminal ingestion drains through a
pinned document-event head in pages of 200, at most 10,000 events per boundary, and marks
remaining backlog incomplete. Completed windows advance the author-work watermark;
failed/dropped windows retain their exact retry end. The third failure records an
unreviewed window before advancing. Confirmation attempts track each exact finding
revision separately and cannot clear, advance or replace an author-work retry window.
Exhausted or completed confirmation targets do not starve later author-work reviews.

`ReviewController(persistent=True, messages=asyncio.Queue())` supplies a host boundary
for immediate new user input while the detached review continues. The host must retain
the store/event loop and call `close()` at shutdown. Cancellation requeues an input
already removed from the unbounded queue. The current CLI is a one-shot process: it
cancels unfinished detached work on exit, records dropped/retry state, and cannot start
an idle session or stream a held final answer into a separate live chat turn. No idle
scheduler or automatic wake capability is claimed. A second process encountering a live
owner reports pending rather than taking its grant. The GUI reads current state on
Refresh and shows the review state at each saved run boundary; it has no push transport.

### Evidence and authority

A snapshot admits at most 256 items and 8 MiB of retained JSON/locator bytes; one item is
at most 256 KiB. Document reads pin an exact immutable revision and verify its digest.
Execution output is a bounded copied projection; mutable worker paths and job log files
are references only. Clipped output cannot have physical line-number anchors. Rich
outputs, file contents and a shell artifact inventory are explicitly omitted and make
coverage incomplete. No missing-evidence assessment can become a failing missing-action
finding, and an incomplete/unchecked window cannot become passed.

The [immutable artifact inventory](ARTIFACTS.md) now provides internal pinned bytes,
metadata and events for explicit harness publication. Successful tool records retain
its canonical receipt beside the worker snapshot. This first slice does not connect
those bytes to reviewer grants or establish automatic shell output coverage; file
and rich evidence therefore remain omitted by the current feed. Producer and
observed inputs remain explicitly unknown rather than inferred from a path hint.

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

Reviewer tools are only manifest/read, checked-item record, finding creation and
confirmation/rebuttal decision. Unknown or denied calls are recorded and consume the
model-turn budget. The reviewer has no worker client, author toolbox, file/network tools,
policy mutation or delegation. The separate call/hour, turn, time, response/context and
soft token budgets reserve before awaiting a model. Actual provider usage is recorded;
actual usage crossing the token budget makes the outcome capped, including a final
response. These are role budgets, not monetary accounting: the project's actual spend
ledger is not implemented and the gateway's hard spend cap remains the monetary boundary.

### Findings, delivery and workspace

Findings retain severity, category, Markdown/LaTeX prose, exact anchors, remedy, revision,
reflags and append-only attributed transitions. Reflags can strengthen the current
severity/prose/remedy while retaining the previous
assessment in history, and a weaker reflag cannot downgrade that current assessment.
Main agents may claim fixes or rebut; only a confirmation reviewer or explicit user
decision can confirm a fix. User writes
use strict expected revisions and replay-safe operation IDs. A block's supersession
links old and replacement revisions and never claims that it fixed the finding.
Confirmation decisions compare the pinned finding revision and current head before
writing. Reviewer confirm/reject also requires audited read ranges covering the finding metadata,
original/response anchored blocks (or whole items) and exact changed revisions. Empty,
out-of-range and unrelated-prefix reads cannot satisfy that guard. Large evidence may
therefore end capped or inconclusive. This records exposure, not model understanding.
Rejected fixes/rebuttals and author response rounds stop at configured bounds;
a claimed fix can remain visibly unconfirmed.
Generic reflags arriving after a fix claim or rebuttal append a deferred audit observation
without changing its revision or response payload. Only the pinned reviewer decision
can resolve that pending response.

Interrupt-threshold findings are delivered at main-agent safe points as bounded
untrusted data. Other severity counts are delivered without their prose or anchors and
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

### Evidence and remaining acceptance

Focused fixtures cover immutable revision reads, unauthorized reviewer tools, value/log
anchors, missing evidence, confirmation and user replacement pins, owner expiry, sticky
and actual-usage caps, concurrent budget reservations, exact retry windows, contiguous
prefixes, malformed cursors, document-only terminal classification, user-input races,
late notices and save-before-acknowledgement. HTTP fixtures cover scope, CSRF, attributed
user decisions, stale heads and replay. Independent [browser acceptance](../.agents/docs/agent-reports/consistency-review-2026-10-12.md)
passed immutable previews, unavailable aliases, delayed selection, scoped sessions, retained
claim/conflict drafts and attributed confirmation. The initial confirmation coverage issue
and corrected scope are recorded, with the exact pending author-work retry preserved.

The initial milestone checks on Python 3.13 passed 9,056 repository tests, with 43 external
integration/image cases skipped; all 425 web tests passed. Ruff, format, strict pyright,
Python/npm license checks, protobuf generation verification, Svelte/TypeScript checks,
production build and whitespace checks passed. The main browser bundle is 490.98 kB;
the review panel loads separately (12.91 kB at that milestone; 13.11 kB after the scope correction).
The finding-assessment/response follow-up passed all 1,221 affected component tests
and 41 focused review/API fixtures, with strict pyright, Ruff, format and whitespace
checks green again.

Final integrated acceptance after the scope correction passed 9,162 Python tests (43
opt-in skips) and 429 web tests, with strict/static/license/protocol checks and both
production builds green. Browser state and transient services were removed.

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

Shell-written artifact inventory/revisions (CRF-6), full rich-output/file evidence,
project spend accounting, ledger-specific evidence, automatic idle turns and a persistent
chat host/push view remain deferred. Mathematical-renderability findings (CR-36–38 and
CRF-14) await the separate bounded KaTeX checker integration; browser rendering is not
presented as an automatic review verdict. Historical finding selection and supersession
inspection are bounded; omitted snapshot context is explicit incomplete coverage.

---

# Historical consistency and shared-component planning review
Date: 2026-10-10. Planning review only. Source inspection supports the findings below; proposed components and backend mappings have not been implemented or exercised.

> **Update (2026-10-10, D11):** the user decided to reimplement the needed CScience behavior rather than adapt or patch the Claude Science runtime. Where this document speaks of retaining, adapting or patching CScience mechanisms, read it as *behavior evidence for the reimplementation*. No Claude Science code, prompts, schemas or assets are copied (D56). OpenScience Desktop upstream is MIT-licensed and remains a code-reuse candidate, subject to per-file checks.

## Conclusion

The requirements fit together. The main implementation risk is building separate infrastructure for each feature: a document history, a ledger history, a reviewer evidence store, a permission queue, and an execution journal that cannot reliably refer to one another. Share identity, immutable revisions, artifact transfer, event delivery, and scoped decisions. Preserve domain-specific meaning and lifecycle on top of those mechanisms.

Start by proving one ordinary research operation across the container boundary with the new harness (D11). The [implementation plan](IMPLEMENTATION-PLAN.md) turns this review into ordered work with evidence gates. It does not assume a new harness, database, or collection of microservices.

## Inconsistencies and ambiguities resolved

| Area | Planning drift or implementation trap | Resolution |
| --- | --- | --- |
| Functional baseline | Some feature lists read as a new workbench implementation before establishing whether CScience can be adapted | Retain its research loop and supported state store during the first spike. Measure execution seams and patch burden before deciding replacement scope. |
| Automatic review | Several acceptance paths made fresh reproduction sound mandatory alongside Science-style automatic review | Preserve current consistency review scheduling, scope and finding/confirmation behavior. Computational reproduction is a separately configured proposed extension, with its own gate. |
| Published frontier results | A universal verification gate would contradict the requirement to accept published results directly | Record published results with source, conditions and reported measures. Apply project-result evidence/promotion policy separately; no automatic rechecking of cited research. |
| Ledger and open items | “Open issues tracked separately” could imply another store | Use typed entries in the same project ledger. Result/frontier and open-issue views filter those entries and their relationships. Review findings have their own lifecycle and link to entries. |
| New versus restored sessions | A common snapshot mechanism could accidentally initialize a new workbook from the evolved project environment | New dedicated session environments start from the pinned base image. Resume/restore addresses an existing logical environment and its checkpoint. These are separate operations. |
| Shared files | A shared writable mount does not provide conflict detection | Use separate session working directories and revision-checked publication. For deliberately mutable shared files, require a managed write/lease or expose conflicting versions. State the limits of any direct writable mount. |
| Dependencies | Sharing project data could accidentally share a mutable Conda prefix across dedicated environments | Each environment owns its dependency state. Serialize package changes in a shared project environment; dedicated session environments share data references, not installed-tool state. |
| Idle preservation | A package list plus persistent volume may omit changed container files and configuration | Capture/retain every permitted mutable location, dependency/configuration state and durable volume. Verify restoration before releasing the only complete environment copy. Live kernel memory is a separate capability. |
| Local deployment | “Same Kubernetes manifests” could become an unintended Kubernetes-only requirement | Certify one local container path and hosted Kubernetes initially. Local Kubernetes is acceptable; direct Docker/Podman is optional. Select using observed enforcement and persistence. |
| Service diagram | Logical boxes could be interpreted as mandatory independent deployments | Keep document, ledger, review and permission modules together in the control application initially. Separate the GUI, orchestration, and trusted worker/policy infrastructure according to placement and authority. |
| Monitor escalation | Human approval cannot safely wait inside a bounded inspection RPC | Persist the request outside middleware. Prevent the affected effect, then resume or retry only when that operation can do so safely under the scoped decision. Do not blindly replay a network request. |
| Alerting | Endpoint audit mode and middleware failure policy could be mistaken for the requested alert-and-continue action | A middleware denial still blocks even in endpoint audit mode. Alert-and-continue must explicitly allow the operation and emit a finding. `on_error` handles middleware failure, independently of the matched rule action. |
| Completion evidence | Browser callbacks and agent narration could become authoritative proof of execution | Worker/control observations own execution status and artifact publication. Record agent assertions as assertions. Reconnect the browser by replaying durable events. |

The OpenShell approval/alert distinctions follow its current [middleware configuration documentation](https://docs.nvidia.com/openshell/latest/extensibility/supervisor-middleware/configure): the documented default RPC timeout is 500 ms, the maximum is 30 seconds, and failure policy is distinct from middleware verdicts. This establishes an integration constraint, not a tested approval mechanism.

## Shared foundations and their consumers

These are logical modules. Their ownership should be explicit even when several share one process and database.

| Common component | Reused by | Shared contract and boundary |
| --- | --- | --- |
| Project identity and resource scope | Sessions, containers, skills, documents, ledger, reviews, connectors | Stable project/session IDs and scoped resource references. Session, environment, kernel, execution and attempt remain distinct identities. |
| Artifact and revision repository | Papers, extracted pages, cell outputs, documents, ledger evidence, reviews, exports | Immutable bytes/revisions, content hashes, lineage, expected-revision mutations and publication. Large payloads travel as artifact references. |
| Typed source anchors | PDF navigation, document comments, ledger citations, review locations | One representation of revision plus block/math node, PDF page/region, or code/cell position. An uncertain reattachment is visible. |
| Durable event delivery | Worker streaming, GUI reconnect, review triggers, monitor findings, ledger projections | Correlation, event IDs, per-stream sequence/cursor, replay and deduplication. Persist domain changes and their events together; a broker is not automatically required. |
| Scoped decision workflow | Install permissions, tool grants and monitor escalation | Durable pending/approved/denied/expired state, exact operation/scope, policy revision and continuation. User preference questions and scientific verdicts are different record types. |
| Execution and environment services | Shell, kernels, package managers, parsers, renderers, batch jobs and optional reproductions | Common routing, limits, cancellation, reconciliation, files and provenance. Backend adapters map these operations to actual enforcement capabilities. |
| Capability and procedure registry | Worker selection, Conda provisioning, external skills, MCP and role configuration | Required versus observed tools; effective versions and actual load/probe evidence. A description advertised by a connector is not a successful worker probe. |
| Mathematical presentation | Chat, workbooks, documents, comments, ledger and reviews | One TeX-preserving rendering policy, math-node identity and visible errors. Full document compilation uses the ordinary worker boundary. |

Do not flatten every object into one generic status field. A scientific result, an open research question, a review finding, an execution failure and a monitor alert differ in meaning. Share references and persistence mechanisms while retaining their own schemas and transitions. Frontier selection is a research-domain projection with comparison rules; it is not the event transport or review queue.

## Concrete reuse candidates

### CScience mechanisms (behavior evidence for the reimplementation)

The current source already contains execution logs, artifact versions, scoped memory, exact-version verification records and unified annotations. They provide candidate backing structures for shared references. Extend or adapt these structures after tracing callers; do not create a competing ledger/document store merely because the new architecture diagram has more boxes. Relevant inspected migrations are `0005_execution_log.sql`, `0026_memory.sql`, `0029_verification.sql`, `0085_annotations_unified.sql` and `0115_verification_checks_resolution.sql` under `dist/review-0.1.60/runtime/assets/drizzle/sqlite/` in the existing checkout.

The bundled Python kernel worker is a long-lived JSON-lines subprocess, with request IDs, persistent variables, streamed output, interruption and usage data. It is not already a Jupyter kernel. First retain its semantics through a kernel adapter; changing the wire protocol is independent of moving execution to a worker. Evidence: `assets/kernels/kernel_worker.py`, header lines 3–21, streaming around 162–204 and main loop around 1672 onward. The broader [source study](SOURCE-STUDY.md) records review/document behavior and pinned source versions.

The local wrapper's capability profiles and installation-root discovery are useful to adapt. Run actual probes inside the assigned worker and its policy. The existing P38 Conda handling preserves the sandbox-injected proxy on Linux/macOS when no explicit CScience proxy overrides it. Reuse one environment/network injection path across kernels, shell and package operations so provisioning cannot silently bypass or break inspection. Current evidence: `pkg/platform/lib/profiles.mjs`, `pkg/platform/lib/capabilities.mjs`, and `src/patches.mjs` P38 around 1693–1778. This code inspection is not a worker integration test.

### Standard components worth testing

| Candidate | Opportunity | Required evidence before adoption |
| --- | --- | --- |
| Jupyter messaging/kernel adapters | Share execution, interruption and rich mathematical output across Sage and other compatible kernels; retain the existing Python adapter initially | Test native Sage syntax, exact types, output events and interruption through the same worker API. Keep kernel sockets private to the worker; do not expose a Jupyter server merely to use its kernel protocol. [Messaging](https://jupyter-client.readthedocs.io/en/stable/messaging.html), [Sage kernel](https://doc.sagemath.org/html/en/reference/repl/sage/repl/ipython_kernel/kernel.html) |
| PDF.js viewer | Reuse a browser PDF renderer and layer project navigation/anchors over it | Demonstrate stable page/region navigation and selection on exact source revisions. It does not replace scientific PDF extraction/OCR or mathematical transcription checks. [PDF.js](https://mozilla.github.io/pdf.js/) |
| pdf_oxide (D59) | Worker-side text, character positions, tables, outline/bookmarks, annotations and optional page rendering under MIT OR Apache-2.0 | Check physical-page and page-label mapping, character coordinates for region anchors, equation/matrix transcription against rendered crops, and behavior on scanned or malformed PDFs. Pin the version; the project is young and fast-moving. [Repository](https://github.com/yfedoseev/pdf_oxide) |
| KaTeX renderer | Share browser math rendering across every research view while preserving original TeX | Test matrix/aligned syntax, macro scope, errors, and rendering security settings. Full TeX compilation remains separate worker execution. [Security guidance](https://katex.org/docs/security.html) |
| OpenShell runtime and middleware | Reuse sandbox lifecycle and policy hooks across certified pools | Use its selected driver per gateway rather than introduce a second scheduler. Verify writable-state retention, local/cluster controls, supported inspection and the three monitor actions. [Runtimes](https://docs.nvidia.com/openshell/latest/how-it-works/sandboxes/runtimes), [evaluation](OPENSHELL-EVALUATION.md) |
| OpenScience fork contracts | Learn from typed execution/events, permission receipts, replay and teardown failure handling | Extract only components whose dependencies and current behavior are established. Native bridge and unfinished agent/persistence paths are not ready substitutes for a backend. [Assessment](REUSE-ASSESSMENT.md) |

External crypto skills remain in their own repository. A shared platform binding should expose project resources, execution, documents/ledger and decisions to them; do not copy their catalog or reproduce a separate board implementation for that pack.

## What the review does not settle

Backend choice, maintainable CScience patchability, exact persistence implementation, and a replacement harness remain evidence-dependent. Jupyter, PDF.js and KaTeX are targeted candidates, not selected infrastructure. This review recommends common contracts and implementation boundaries; it adds no requirement for a new database, workflow engine, event broker, real-time collaboration framework, or public multi-user service.

## Mechanical document findings

Document-revision D82 checks use the same finding/history and authenticated USER decision surfaces, with explicit `math` origin, a real mechanical check ID and no synthetic reviewer run. The additive migration preserves existing reviewer IDs, history, notices and CAS heads; readonly legacy stores project `review` origin without migration. Reviewer categories/grants are unchanged and exclude this mechanical authority. Generic confirmation, reflag, supersession and threshold delivery paths cannot decide math findings. A separate bounded controller checks immutable document revisions and collects closing-only notices together with terminal findings, sharing at most two author response rounds. Accepted current formulas and complete parsed removal confirm mechanically; stale, skipped and unavailable evidence never does. USER takeover retains unresolved historical evidence while excluding new author reflags/notices. Details, limits and remaining non-document LD-53 scope are in [document math checks](MATH-CHECKS.md).
