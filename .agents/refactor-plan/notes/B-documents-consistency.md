# Review notes: B, living documents and the consistency review

Baseline: main at fa456bc (2026-10-14).

Reviewed in depth: `documents/store.py` (schema, connection, transactions, change protocol, `_event` hooks), `consistency/store.py` (schema, `ingest_documents`, `confirmation_targets`, `dispatch`), `documents/annotations.py` (`change`, leased requests), `consistency/findings.py` (`create`, `_transition`), `consistency/mechanical.py`, `consistency/artifacts.py` (`ingest_artifacts`), `consistency/notices.py` and `mathnotices.py`, `documents/mathjobs.py` (`claim`, `step`, `commit`), `mathqueue.py`, `publication.py`, `worker_source.py` (`apply`), `orchestrator/mathcheck.py` and `web/workspace/server/check-math.mjs`, the parity tooling (`scripts/document_parser_parity.py`, `web/workspace/server/table-parity.test.mjs`, the four `*.fixtures.json`), `docs/LIVING-DOCUMENTS.md`, the as-built half of `docs/CONSISTENCY-REVIEW.md`, D12, D82, D85 and `docs/specs/ledger.md`.
Skimmed: `parser.py`, `engine.py`, `identity.py`, `operations.py`, `blocks.py`, `normalize.py`, `source.py`, `comment_bodies.py`, `comment_math.py`, `response_math.py`, `response_bodies.py`, `project_macros.py`, `exports.py`, `delivery.py`, `delivery_journal.py`, `tool.py`, `consistency/controller.py`, `runner.py`, `jobstate.py`, the specs' status lines, the area's tests.
Skipped: `table_rule.py` internals (vendored port), `prompt_bundle.py`, `rubric.py`, `frontmatter.py`, the browser's TypeScript beyond the parity test.

Measured: documents is 13,471 lines in 29 modules, consistency 5,781 in 14. By the same measure as the brief, 25 of the repository's 54 functions over 100 lines are here. The 1,226 tests for the area pass in 26 s. Scratch work (a scratch control store, a renderer-call count, a Node spawn timing) is in `scratchpad/refactor-review/B/`.

## Summary

The document core is careful, measured, well tested and worth keeping. That covers the parser, block and formula identity, the patch engine, and `DocumentStore`'s change protocol (prepare outside the lock, fence and commit inside `BEGIN IMMEDIATE`). Around it, `control.sqlite3` has grown to about 74 tables, 81 indexes, 57 triggers and 2 views, and its `user_version` is still 1. About ten components ("tenants") share that file and its connection through the private `DocumentStore._read()/_write()` seam (146 call sites in 23 files). Each tenant brings its own DDL, version marker, idempotency table, event or notice table and defensive SQL. documents and consistency import each other through the math checks. The math-check pipeline has 4,723 lines for D82 and the project macros, over three kinds of text. It renders every formula of every intermediate revision, one fresh Node process per formula. About 2,400 lines are unwired prerequisites for document files in environments. The ledger spec needs exactly the patterns that already exist in two to nine copies.

The changes that matter most:

1. **B-01, B-02.** A small public `ControlStore`: `read()`/`write()` yield a typed `Tx`, and one migration ladder runs on `user_version`. It replaces the private seam, the 7 version markers and about 30 runtime schema probes. Everything else here depends on it.
2. **B-03.** Math checks: reuse earlier results by formula hash, run one Node process per batch, and (owner's call) stop rendering revisions other than the latest.
3. **B-04.** Extract the five repeated primitives before the ledger is built: operation receipts, events with subscribers, leased notices, versioned Markdown text with a math-source protocol, and feed cursors. The ledger should be their first new client, not their fourth copy.
4. **B-05.** Break the documents↔consistency cycle: move the math checks into their own package, and replace the hard-coded save hooks with subscriptions.
5. **B-11.** Freeze the unwired environment-sync code until LD-49 to LD-52 are scheduled.

## Findings

### B-01: The control store has no public transaction or repository layer
- Where: `documents/store.py:417-543` (`_connection`, `_prepare`, `_write`, `_read`); 146 calls to `documents._read()/_write()` in 23 files: `consistency/store.py` 32, `documents/mathjobs.py` 13, `session_plans.py` 12, `documents/annotations.py` 11, `consistency/findings.py` 10, `artifacts.py` 9, and others. 14 of the repository's 26 files with `# pyright: reportPrivateUsage=false` are in this area. `documents/worker_source.py:33` imports `_Assessed, _key, _Row, _Submission` and calls `_find`, `_prepare_source`, `_act`, `_remember` and `_reject`. Outside the orchestrator, `workspace/reviews.py:162`, `workspace/plans.py:234` and `workspace/plan_edits.py:127` also open `_read()`.
- Category: architecture
- Priority: P1
- Evidence: `DocumentStore` plays two roles. It is the documents repository. It is also, in effect, the control store: it owns the process's single connection and `RLock`, creates six other modules' schemas in `_prepare` (`store.py:471-498`), and calls three other modules' hooks from `_event` (`store.py:1908-1917`). `_read()` yields `Connection | None`, and that produces 94 `db is None` / `assert db is not None` guards. Every tenant writes raw SQL on the shared connection, and no type marks "this code runs inside a transaction". D12 asked for SQLite "behind a repository interface"; there is none. The file-level pragmas say the modules "deliberately share" internals, which is the missing API described in a comment.
- Proposal: add `orchestrator/control/` (about 150 lines). This is the small unit-of-work layer the brief asks about. It has no ORM and no query builder; SQL stays in the repositories.
  ```python
  class ControlStore:          # one per process per file; owns connections, lock, migrations
      def __init__(self, path: Path, *, readonly: bool = False, timeout: float = 10.0): ...
      def migrate(self) -> None                                   # B-02
      def subscribe(self, kind: str, hook: Callable[[Tx, ControlEvent], None]) -> None  # B-05
      @contextmanager
      def write(self) -> Iterator[Tx]     # BEGIN IMMEDIATE; busy -> ControlError(BUSY)
      @contextmanager
      def read(self) -> Iterator[Tx]      # consistent snapshot; raises, never yields None
  class Tx:
      sql: sqlite3.Connection             # repositories keep their SQL
      def emit(self, event: ControlEvent) -> int                  # B-04
      def after_commit(self, fn: Callable[[], None]) -> None      # B-14
  ```
  `DocumentStore(control)` becomes one repository beside `Annotations(control)`, `Reviews(control)`, `MathJobs(control)` and the rest. In-transaction helpers take `tx: Tx`; most already take `db`. Keep the change protocol exactly. Make public the two steps the worker adapter needs: `prepare_source_edit(...) -> PreparedChange` and `apply_prepared(tx, prepared)`. Rename `DocumentError` to `ControlError` and keep an alias, since 25 files outside `documents/` raise it for plans, delegation, artifacts and reviews. Add a CI grep that fails on `._read()` or `._write()` outside `control/`.
- Effort: M (mechanical; it also touches the 14 test files that call `_read`/`_write`).
- Risk and verification: the change should not alter behaviour; the area suites cover it, including `test_document_store.py::test_concurrent_writers_in_processes`. First add a test that a nested `write()` raises a clear `ControlError`. Today it surfaces as a raw `sqlite3.OperationalError` ("cannot start a transaction within a transaction"), because `_busy()` (`store.py:285`) does not match that error.
- Depends on / enables: enables B-02, B-04, B-05, B-11, B-14, B-17 and the ledger.

### B-02: Schema versioning is done per tenant, partly at runtime
- Where:
  - Version markers: `documents/store.py:82` (`SCHEMA_VERSION = 1`) and `:449-502`; `publication.py:116-131` (`publication_meta.version`); `consistency/finding_schema.py:41-78` (`review_finding_schema.version = 2`, plus a table rebuild); `session_plans.py:384`, `delegation_records.py:509`, `child_attempts.py:475` (more `_meta` version singletons).
  - In-place migrations: `mathqueue.py:65-113` rebuilds `math_jobs` when `PRAGMA table_info` detects the old shape, checked at every open. `consistency/store.py:215-235` runs `ALTER review_calls` on every `ReviewStore(...)` construction, and `jobstate.py:36-71` alters `job_watcher_calls`.
  - Runtime probes: `available()` (`consistency/store.py:237`), `exists()` (`project_macros.py:127`, `comment_bodies.py:89`), `projection()` (`finding_schema.py:80`, a `PRAGMA table_info` on every finding read), `scope_table()` (`mathjobs.py:38`), `_ready()` (`publication.py:160`), `_available()` (`annotations.py:333`).
- Category: persistence
- Priority: P1
- Evidence:
  - A scratch store built by `DocumentStore`, `ReviewStore`, `JobState` and `SessionPlans` has 69 tables (plus 5 more from delegation and child attempts), 81 indexes, 57 triggers and 2 views, at `user_version` 1.
  - There are 7 independent version markers, about 10 initializers that run DDL at runtime, and about 30 `sqlite_master`/`pragma_table_info` probes that let readers tolerate older schemas.
  - Every writer open re-runs backfills that are O(rows): `store.py:491-498` scans all annotation entries, and `mathqueue.py:104-113` scans all documents. The harness opens up to five writer stores per run (`cli.py:200`, `467-506`, `JobState`).
  - Because old binaries may keep writing version 1, there is reconciliation code for mixed-version writers: `publication.py:183-212`, `mathjobs.py:190-202`, and `comment_math.reconcile`.
  - `LIVING-DOCUMENTS.md:9-25` lists about 25 tables and "user_version, 1".
- Proposal: one `control/migrations.py` holding an ordered `MIGRATIONS: list[Callable[[Tx], None]]`. `ControlStore.migrate()` runs the pending steps in one `BEGIN IMMEDIATE` and sets `user_version`.
  - Writers migrate at startup: the harness, and the workspace, which already holds a writer (`workspace/app.py:348`).
  - Readers refuse an older or newer version with a clear "upgrade" message, so the read paths drop every probe and legacy projection.
  - Raising `user_version` also stops old binaries from writing. That removes the mixed-writer reconciliation.
  - Tenants contribute migrations instead of running DDL in their constructors.
  - If the owner accepts discarding pre-M2 development databases, start at a fresh baseline (version 2 = today's combined schema) and delete the rebuild and backfill code. Otherwise, version 2 = "run today's `prepare` functions once".
- Effort: M (S if development databases may be reset).
- Risk and verification: existing development stores, if the baseline is reset. Tests that build legacy schemas (`test_math_finding_migration.py` and the legacy-reader cases) must be retired or rewritten. Check against a store made at fa456bc and then migrated forward.
- Depends on / enables: depends on B-01. Enables B-10, B-12 and the ledger's tables.

### B-03: Math checks render every formula of every intermediate revision, one Node process per formula
- Where:
  - `documents/mathjobs.py:236-241`: `claim` takes revision `completed+1`.
  - `mathjobs.py:739-872`: `step` renders every span of the pinned revision.
  - `consistency/mechanical.py:138`: `observe` returns unless the check is the current one, and the view at `comment_bodies.py:43-50` requires `d.head=j.revision`.
  - `mathcheck.py:219-260`: `check_many` calls `check` once per formula. Each call re-reads and hashes the bundle (up to 8 MiB) and the launcher, copies both into a new temporary directory, and spawns Node.
  - `mathjobs.py:775-822`: `identity()` runs twice per step, two more bundle hashes.
- Category: performance
- Priority: P1 (on the M2 path)
- Evidence:
  - Scratch run with the tests' `ScriptedChecker`: a 50-paragraph document (95 formulas), created and then patched five times (one paragraph each), produced 585 renderer calls. Only the latest revision's 95 can raise or close findings, and only about 6 formulas changed.
  - In production each call is a Node process. Here, node plus importing KaTeX took 27 ms (median of 10), before the copying and hashing.
  - Example: 16 patches in one turn to a 600-formula document come to about 10,000 processes. The live controller yields after 8 steps (256 formulas), and closing waits at most 10 s (`MATH-CHECKS.md:169-171`), so findings fall behind the turn.
  - On the Pi 5 (M3), the 2 s deadline, which includes queueing, may make the tail of a 32-formula batch on two slots `unavailable`. That is an inferred risk, not measured.
  - `MATH-CHECKS.md` does not mention full re-rendering.
- Proposal:
  1. **Reuse results.** Before rendering, look up the newest `accepted` or `rejected` observation with the same formula key (hash of TeX, display mode and effective macros, without the span ID) under the same renderer identity. Render only the misses, and never reuse an `unavailable` result.
  2. **One process per batch.** The launcher takes a JSON array of up to 32 formulas and builds a fresh `macros` object for each. Hash and copy the bundle once per batch or per identity change.
  3. **Owner's decision.** Claim only the current revision, record skipped ones as `superseded`, and take block ownership from the block maps, which `attribution` already walks. Reflags would then count checks rather than saves; see `test_two_failing_patches_reflag_but_unrelated_revisions_do_not`.
- Effort: S for (1) and (2); M for (3).
- Risk and verification: the existing math tests cover it: `test_document_math.py` (32 tests), `test_comment_math.py`, `test_response_math.py`, and `test_mathcheck.py`, which uses real Node in the web CI job. First add a test that counts renderer calls across N revisions. A batch always comes from one source in one project, and `trust: false` is unchanged.
- Depends on / enables: independent. Makes D82 checks on ledger statements affordable.

### B-04: Five persistence patterns exist in two to nine copies, and the ledger would add another of each
- Where (pattern, current copies, what the ledger needs):

  | Pattern | Copies now | Ledger need |
  | --- | --- | --- |
  | Operation receipt (operation ID + fingerprint → first result) | 9 tables: `requests` (`store.py:143`), `annotation_operations`, `review_operations`, `project_macro_operations`, `document_export_inputs`, `worker_source_receipts`, `artifact_operations`, child attempts, delegation records. Each has its own replay code and canonical JSON: `dump` is defined 11 times in the two packages, some with `allow_nan=False` and some without | LG-30, LG-31: a mutation of several entries under one operation ID |
  | Event log committed with the change | `events` (its `document_id` is NOT NULL), `review_events`, `artifact_events`, `publication_events`, and the delegation and child event tables. `DocumentStore._event` hard-codes three subscribers inside the transaction (`store.py:1887-1917`) | LG-33 `ledger.*` events; LG-34 views rebuilt from events; LG-28 flags when a bound document block changes |
  | Leased notices (pending, renew, release, acknowledge; 120 s lease, at most 128 per run, 20,000 characters) | `annotations.py:824-906`, `consistency/notices.py:15-91`, `mathnotices.py:16-110` (a subclass that overrides all four methods), plus `JobState`. `loop.py:112-316` keeps each source's `seen`/`delivered` sets by hand | Flags and decisions reaching the main agent |
  | Versioned Markdown text with block map, IDs, owners and ID counters | `revisions`, `annotation_body_revisions` (`comment_bodies.py:27-32`), `review_response_bodies` (`response_bodies.py:40-50`) | LG-1: statements with math, normalized (LD-42) and checked (D82) |
  | Math source queue, job link table and "current check" view | `math_sources`; `comment_math_sources` + `math_comment_jobs`; `response_math_sources` + `math_response_jobs`; the views `math_current_checks` and `math_job_scope` are UNIONs over the sources. Dispatch works by probing tables: `mathjobs._fence:107-140`; `claim` falls through document → comment → response (`:243-251`); `commit` (`:652-672`); `mechanical._anchors:316-341`; `source_key` | A fourth source, ledger statements |
  | Cursor-paged feed consumer | `ingest_documents` (`consistency/store.py:390-460`), `ingest_artifacts` (`consistency/artifacts.py:149-345`) | CR-9 and CR-10: the reviewer consumes ledger events |
  | Expected-revision compare-and-set with append-only transitions | annotation threads, `Findings._transition` (`findings.py:260-331`), project macros, session plans | LG-30 |

- Category: duplication
- Priority: P1 (before the ledger)
- Evidence: the table above. Time is stored three ways (ISO text for documents; REAL seconds for annotations, reviews and jobs; `time.time()` called directly in `annotations.py:698,769,840,868`), so a ledger would have to pick one more.
- Proposal: build on B-01's `Tx`, adding small primitives in `control/`:
  - `Receipts`: one table `operation_receipts(scope, actor, operation_id, fingerprint, result)` with one canonical JSON.
  - `EventLog`: one table `control_events(seq, kind, producer, project, session, subject_type, subject_id, payload)`, with `ControlStore.subscribe`.
  - `LeasedNotices`: parameterized by table, selection SQL and projection. It owns the `seen`/`delivered`/acknowledge bookkeeping so `loop.py` stops doing it; coordinate with area A's notice-source interface.
  - `TextRevisions`: `text_revisions(subject_type, subject_id, revision, body, sha256, block_map, owners, next_block, next_span, author)`, with a `MathSource` protocol (`load`, `macros`, `owners`, `scope`, `finish`). `math_jobs` keyed by `(subject_type, subject_id, revision)` replaces the link tables and the UNION views.
  - `FeedCursor`: a pinned target, bounded pages, and a compare-and-set on the cursor in the same transaction as the observations.

  Move existing tenants over one at a time; receipts and notices are the smallest. The ledger is then: an entry repository, `ledger.*` events, a `TextRevisions` statement with its `MathSource`, a `FeedCursor` for the reviewer, and an anchor kind for findings anchored on entry fields (LG-25), registered with `Findings.validate_anchor`, which today knows only document blocks, logs and artifacts (`findings.py:178-228`).
- Effort: L in total (receipts, events and notices M; text revisions and math sources M to L).
- Risk and verification: existing data has to be migrated (see B-02 and question 1). Tests that run raw SQL on specific tables will churn (`test_response_math.py` has 39 `execute` calls, `test_document_math.py` 32). Rewrite them against the public API before moving the tables.
- Depends on / enables: depends on B-01 and B-02. Enables the ledger and B-05.

### B-05: documents and consistency import each other; the math-check schema is spread over five modules in both packages
- Where:
  - `documents/mathjobs.py:23` imports `consistency.store` at module level, and `consistency/store.py:25` imports `documents`.
  - 77 imports inside functions across the two packages (33 of them in `mathjobs.py`) avoid the cycle.
  - Foreign keys run both ways: `math_finding_state` → `review_findings` (`mathqueue.py:47-53`), and `review_findings.math_check_id` → `math_jobs` (`finding_schema.py:27-34`).
  - The view `math_current_checks` is defined in `documents/comment_bodies.py:43`, and `math_job_scope` in `consistency/response_bodies.py`.
  - The generic `threaded()` helper lives in `mathjobs.py:66` and is imported by `consistency/controller.py` and `runner.py`.
- Category: coupling
- Priority: P2
- Evidence: no direction of dependency holds between the two packages; math checks write findings, and findings read math state.
- Proposal: create a package `orchestrator/mathchecks/` holding `mathjobs`, `mathqueue`, `comment_math`, `response_math`, `mathcontroller`, `mechanical`, `mathnotices` and the two views, depending on both documents and consistency. consistency then depends on documents in one direction only, and documents depends on neither. The save hooks (math scheduling, publication, macro pins) become `control.subscribe("document.revision_created", ...)` calls made where the harness is assembled (`cli.py`). Move `owned`/`threaded` out (B-15).
- Effort: M
- Risk and verification: import order; pyright and the area suites catch it. Add an import-graph test that `orchestrator.documents` imports nothing from `orchestrator.consistency`.
- Depends on / enables: depends on B-01's `subscribe`. Enables B-04's `MathSource`.

### B-06: `ReviewStore.dispatch` (356 lines, 58 branches) builds, budgets and commits a snapshot in one function
- Where: `consistency/store.py:604-959`; `confirmation_targets` at `:553-602`.
- Category: complexity
- Priority: P2
- Evidence:
  - It passes positional 5-tuples `(kind, location, payload, sha, complete)`.
  - Unnamed numbers: 4096/4097, 262145, 33, 65, 100/101, a 256-byte overhead, and 3 retries (`confirmation_failed`, `:961-968`).
  - The fallback is a recursive call (`:811-820`).
  - One read transaction covers the window and feed, then each finding opens its own `_read()` (`:710-719`), and `documents.revision()`, `artifact_pin` and `evidence()` each open another.
  - Imports inside the function at `:717`, `:734` and `:859`.
  - `confirmation_targets` repeats a 6-line correlated subquery twice, and splices in an optional SQL fragment depending on whether the project-macro tables exist (`:564-571`).
- Proposal:
  - Two dataclasses: `EvidenceItem(kind, locator, payload, sha256, complete)`, and `SnapshotBudget` with `fits(group)` and `add(group)`.
  - About eight functions: `_window(tx) -> Window(watermark, retry_end, end)`; `_feed_items(tx, scope, window)`; `_open_findings(tx, scope, targets)`; `_finding_group(scope, row) -> FindingGroup` (response, body pin, revision and artifact pins, grants; today's loop body at `:690-806`); `_coverage(...)`; `_confirmation_key(...)`; `_commit_snapshot(tx, plan) -> str | None` (the fence and inserts at `:850-958`).
  - Replace the recursion: before building, `if kind == "confirmation" and not fits: kind, coverage = "terminal", False`.
  - Write the latest-body subquery once as a CTE, and drop the optional fragment once B-02 guarantees the tables exist.
  - Name the constants.
- Effort: M
- Risk and verification: `test_consistency.py` (1,216 lines), `test_artifact_review.py`, `test_reviewer_prompt_provenance.py`. First add golden tests of the snapshot's items, grants, coverage flag and confirmation key for three cases: author work; a confirmation that fits; a confirmation that does not fit and falls back to terminal.
- Depends on / enables: easier after B-01 and B-02.

### B-07: `Annotations.change` (268 lines, 70 branches) handles five actions in one body
- Where: `documents/annotations.py:555-822`.
- Category: complexity
- Priority: P2
- Evidence: the replay check appears twice, verbatim (`:587-604` and `:667-685`). Authority checks are spread over four places (`:560-563`, `:652-658`, `:710-721`, `:742-750`). It calls `time.time()` directly while `ReviewStore` takes an injectable clock.
- Proposal: `change()` becomes four steps:
  1. `value = _parse(raw, author)`, which checks the author, body bounds and control characters.
  2. `prepared = PREPARE[value.action](...)`: create builds the anchor; a reply may build a scope report; edit and edit-body load the parent body and check it; resolve and reopen need nothing.
  3. `with control.write() as tx`: replay, or `APPLY[value.action](tx, prepared)`.
  4. A shared `_record(tx, result)` writes the event and the receipt.

  B-04's `Receipts` removes both replay copies. Inject the clock.
- Effort: S to M.
- Risk and verification: `test_document_annotations.py` (889 lines), `test_comment_math.py`, and the workspace's document API tests.

### B-08: `Findings.create` (196 lines) and `MechanicalFindings.observe` (180 lines) mix policy and SQL
- Where:
  - `consistency/findings.py:333-528` has three outcomes: deferred reflag (`:412-432`), reflag with a possible severity upgrade (`:433-466`), and new finding (`:467-519`). The severity `ranks` dict is defined twice (`:312`, `:433`).
  - `mechanical.py:135-314`: accept (`:186-206`), reflag (`:207-260`), new finding (`:261-314`). Its UPDATE of `review_findings` (`:210-225`) bypasses `Findings`.
- Category: complexity
- Priority: P2
- Evidence: the function lengths and branches above, and two modules writing `review_findings` independently.
- Proposal:
  - In `findings.py`: `_status_for(value, run, anchors)`, `_defer(...)`, `_reflag(...)`, `_raise(...)`, and a module-level `SEVERITY_RANK`.
  - In `mechanical.py`: `_accept`, `_reflag` and `_raise`. Move the UPDATE behind `Findings.reassess(tx, ...)`, so that one module writes `review_findings`.
- Effort: M
- Risk and verification: `test_consistency.py`, `test_document_math.py`, `test_math_finding_migration.py`.

### B-09: Two feed consumers with different atomicity, and project JSON read per item
- Where: `consistency/store.py:390-460` (`ingest_documents`); `consistency/artifacts.py:149-345` (`ingest_artifacts`); `ReviewStore.scope` (`consistency/store.py:244-254`).
- Category: duplication
- Priority: P2
- Evidence:
  - `ingest_documents` writes each observation in its own transaction (through `observe()`, `:426`), then advances the cursor in another (`:451-457`). It is correct only because observations are idempotent by key and the cursor uses `max()`.
  - `ingest_artifacts` inserts the observations and compare-and-sets the cursor in one transaction (`:284-330`), and also has a second source for uncertain operations.
  - `scope()` reads and validates the project's JSON file on every call: once per observation inside `ingest_documents`, and once per notice or finding API call.
- Proposal: use B-04's `FeedCursor`. Both consumers shrink to about 40 lines, and both become atomic. Split `ingest_artifacts` into `_read_page`, `_project_events`, `_project_uncertain` and `_commit_page`. Check scope once per public call, not once per item.
- Effort: S to M.
- Risk and verification: `test_artifact_review.py` and `test_consistency.py` (terminal drain, the 10,000-event cap).

### B-10: Re-validating the control store's own rows goes beyond the threat model
- Where:
  - 135 `substr(CAST(x AS BLOB),1,n)` clips in the two packages: `mathjobs` 19, `comment_bodies` 19, `response_bodies` 19, `exports` 14, `response_math` 11, `project_macros` 11, `comment_math` 11, and others.
  - After the clip comes a length or regex check of IDs and hashes the code itself wrote, e.g. `comment_math.py:48-60`, `mathjobs.py:127-139`, `mathjobs.py:255-261`.
  - 41 "exceeds its bound" errors.
  - Tests drop the immutability triggers and write 20 MiB rows to show that reads refuse them (`test_response_math.py:265-282`; `LIVING-DOCUMENTS.md:72-74`).
- Category: over-engineering
- Priority: P2
- Evidence: `THREAT-MODEL.md:52` places the control application and its state in Z2, "trusted state". Anyone who can write `control.sqlite3` can also change the code that reads it. The clipping makes most of the SQL hard to read, and every new query needs another bound and another test.
- Proposal: validate at write boundaries: pydantic on input, and `CHECK` constraints on formats and lengths, which SQLite enforces on every write. Read rows plainly. Keep the append-only triggers (a cheap guard against bugs) and the bounded API responses (pagination). Remove tests that work by dropping triggers. Owner's decision (question 5).
- Effort: M (table by table, alongside B-02 and B-06).
- Risk and verification: low if the `CHECK` constraints land first. The migration must validate existing rows against them.
- Depends on / enables: B-02.

### B-11: About 2,400 lines of unwired environment-sync prerequisites depend on `DocumentStore` internals
- Where:
  - `documents/worker_source.py` (552 lines; imported only by `test_worker_document_source.py`).
  - `delivery.py` (459; tests only).
  - `delivery_journal.py` (485; `kernel_notice_bindings.py:27` imports two of its helpers).
  - `exports.py` (596; only `ExportInputs`' schema is wired, through `store.py:484`).
  - `publication.py`'s `Publications` reader (used only by `delivery.py`).
  - Their tests are 2,231 lines.
- Category: dead code
- Priority: P2
- Evidence: the publication hook (`publication.py:134-141`) writes 3 to 4 rows on every revision and promotion, for a consumer that does not run in production. All of this landed on 2026-10-12 for LD-3, LD-35 to LD-38 and LD-49 to LD-52, which `LIVING-DOCUMENTS.md:261-262` still lists as not built.
- Proposal: freeze it.
  - Move it to `documents/environment_sync/`, or to a branch, with a short README naming the spec items and the missing runtime pieces.
  - Do no further work on it until LD-49 to LD-52 are scheduled, then rebuild on B-01's public API.
  - First move `DeliveryError` and `endpoint_fingerprint` somewhere neutral, since `kernel_notice_bindings` uses them.
  - Decide whether the publication hook stays on (question 2). Its bootstrap already copes with enabling it later.
- Effort: S
- Risk and verification: none for the M2 path.

### B-12: Project-macro provenance uses nine tables to pin one setting into every consumer
- Where:
  - `documents/project_macros.py`: 703 lines; its schema at `:36-79` defines the setting's revisions, heads and operations, plus `math_macro_pins`, `document_macro_pins`, `response_macro_anchors`, `response_macro_revisions`, `math_macro_contexts` and `review_macro_pins`.
  - It is wired into `store.py:1915-1917`, `response_bodies`, `mathjobs`, and `consistency/store.py:564-575`, `:734-738` and `:936-940`.
  - `workspace/reviews.py:155-185` re-resolves the context for every finding and every history entry.
- Category: over-engineering
- Priority: P2
- Evidence: the setting (LD-28) is a bounded dictionary of KaTeX macros. Its exact historical context is pinned separately for documents, responses, math checks and reviews. Confirmation keys and snapshot completeness depend on it (`consistency/store.py:782-786`, `:840-842`, `:867-868`).
- Proposal: keep the immutable `project_macro_revisions`, and put one `macro_revision` column on the rows that need it (revisions, `math_jobs`, review evidence). Derive the effective context when reading, and drop the separate pin, anchor and context tables. Question 4.
- Effort: M
- Risk and verification: `test_project_macros.py`, `test_comment_math.py`, `test_response_math.py`.
- Depends on / enables: B-02.

### B-13: Parser parity: keep the shared fixtures, and run the whole-document comparison in CI
- Where: `web/workspace/src/lib/{documents,dollarmath,mathdelimiters,tables}.fixtures.json` (36 + 41 + 54 + 52 = 183 cases, run by pytest in `test_document_parser.py:45-88` and by vitest); `web/workspace/server/table-parity.test.mjs` (1,024 seeded documents through `scripts/document_parser_parity.py`, in the web CI job); `parser.py:57-60` (`ss-markdown-1` is kept permanently for immutable block maps).
- Category: tests
- Priority: P3
- Evidence: CI runs the fixtures and the seeded table corpus. The comparisons over all constructs (15,000 and 40,000 random documents) are "run by hand" (`LIVING-DOCUMENTS.md:135`).
- Proposal: extend the generator in `table-parity.test.mjs` to lists, quotes, containers, both math syntaxes, and links, images and code spans, at a size CI can afford (about 1,000 to 2,000 documents in the existing 30 s subprocess budget). A version bump of markdown-it, markdown-it-py or a plugin then fails CI instead of waiting for a manual run. A policy question for the owner: every parser change adds a permanent profile; before real use, should resetting history be allowed?
- Effort: S
- Risk and verification: CI time in the web job.

### B-14: Connection and transaction details
- Where:
  - `documents/store.py:400-447`: one connection and one `RLock` per `DocumentStore`, with `check_same_thread=False`. In the harness, documents, reviews, artifacts, plans and math share that one instance (`cli.py:200-301`), so all their reads and writes in the process queue behind a single lock.
  - `store.py:1866`: `_cache_put` runs inside the transaction, before COMMIT. If the commit then fails, the cache holds a parse with block and formula IDs for a revision number that was never committed. The key includes the text hash, but the same text can carry different IDs (revert, `store.py:1203`).
  - `jobstate.py:74-82` uses a third connection style in the same file: a new connection per call, Python's implicit transactions, and tables that are not STRICT and have no column types.
  - `cli.py:467-506` opens three more stores after a run, just to acknowledge notices.
- Category: concurrency
- Priority: P3
- Evidence: the locations above.
- Proposal: inside `ControlStore`, keep one writer connection plus per-thread read connections (WAL readers do not block), fill caches through `Tx.after_commit`, use a single clock, and pass one `ControlStore` per process to every repository, including `JobState` and the acknowledgement path.
- Effort: S to M.
- Risk and verification: `test_document_store.py` (concurrency); add a test that a failed commit leaves no cache entry.
- Depends on / enables: B-01.

### B-15: Executors that keep ownership of work through cancellation are reimplemented per component
- Where: `mathjobs.py:48-67` (`owned`, `threaded`), `mathcheck.py:160-169` (`_owned`), `consistency/artifacts.py:347-397` (`ArtifactConsumer`), `delivery.py:180-198` (`_sql`), `artifact_publisher.py:62`; four more copies in the worker (`capture.py:45`, `environments.py:83`, `installer_records.py:374`, `executor.py:49`).
- Category: duplication
- Priority: P3
- Evidence: the same shield-and-drain logic written about nine times.
- Proposal: one module in a library both deployables may import: `owned(awaitable)`, and `OwnedExecutor(max_pending, name)` with the admission bound, shield, and drain on `aclose()`.
- Effort: S
- Risk and verification: the existing tests that work must stay owned through repeated cancellation (`test_document_math.py:737`, `:1018`, and the artifact consumer tests).

### B-16: Docs drift
- Where and evidence:
  - `LIVING-DOCUMENTS.md:9-25` lists about 25 tables and schema version 1. In fact there are about 74 tables and 7 version markers (B-02).
  - `LIVING-DOCUMENTS.md:135` says "three files" of shared cases; there are four (`tables.fixtures.json`).
  - The docstring in `documents/__init__.py:4-15` describes the modules of "W4's first slice"; 20 modules are not listed.
  - D12's "repository interface" does not exist (B-01).
  - The workspace exception in CONVENTIONS.md covers the orchestrator's "scoped project and control stores". `workspace/reviews.py:155-185` goes further: it composes SQL-level helpers on a raw connection (B-17).
  - About 20 acceptance-diary claims (test counts, browser runs) are embedded in the area's nine docs and will go stale.
  - `MATH-CHECKS.md` does not say that each revision is checked in full (B-03).
- Category: docs drift
- Priority: P3
- Proposal: correct the lines above; move the acceptance diaries to `.agents/docs/agent-reports/` and link to them.
- Effort: S
- Risk and verification: none.

### B-17: The workspace reaches into orchestrator internals at the SQL level
- Where: `workspace/reviews.py:155-185` (`store.documents._read()`, `finding_anchors(db, ...)`, `MathJobs(store).job`), `workspace/plans.py:234`, `workspace/plan_edits.py:122-127`.
- Category: coupling
- Priority: P3 (P2 once W5's API starts)
- Evidence: a separate deployable composes the orchestrator's schema-level functions through a private connection.
- Proposal: the workspace calls repository methods that return view models (for example `Reviews.finding_view(project, session, finding)`). No raw connection crosses the package boundary. Cross-reference area E.
- Effort: S to M.
- Risk and verification: the workspace API tests.
- Depends on / enables: B-01.

## Keep

- **`DocumentStore`'s change protocol** (`store.py:777-1035`). It parses, carries IDs over and assesses the change outside the write lock. Under `BEGIN IMMEDIATE` it checks that `_key` is unchanged, then reuses that work or redoes it. A busy store returns `busy`, retryable with the same operation ID. The ledger should use the same protocol.
- **Store invariants.** Append-only revisions and events guarded by triggers; fingerprinted operation IDs; WAL with `synchronous=FULL`; unknown files and versions refused.
- **The parser, identity and engine core.** Its decisions are documented, its performance is measured (`LIVING-DOCUMENTS.md:247-255`), it has seeded property tests (`test_document_properties.py`), and the fixtures and seeded table corpus are shared with the browser.
- **`table_rule.py`.** Keep this vendored port close to upstream. Exempt it from function-length metrics and do not split `table`.
- **Reviewer boundaries.** The reviewer has no worker client, file or network tools, or author toolbox. Budgets are reserved before model calls, and "unavailable or incomplete never becomes a pass". Findings keep their own lifecycle, apart from document revisions (CR-8).
- **`mathcheck.py`'s process hygiene.** Bounded pipes, process-group kill, pinned versions and options. Keep all of it while batching (B-03).
- **The tests.** 1,226 for the area in 26 s, on real SQLite, with a multi-process writer test and few sleeps.

## Questions for the owner

1. **Development databases.** Can pre-M2 `control.sqlite3` files be discarded, or exported and re-imported, when the schema gets a new baseline? This decides whether B-02 keeps the legacy migrations, the legacy-reader branches, `ss-markdown-1`, and the finding and math table rebuilds.
2. **Environment files and exports.** Are document files in environments (LD-3, LD-49 to LD-52) and exports (LD-35 to LD-38) part of the M2 exit? If not, freeze B-11's code and decide whether the publication hook stays on.
3. **Math-check scope.** Must every intermediate revision be checked, so that reflags count saves? Or only the current revision when the worker reaches it? And is one Node process per batch from one source acceptable, instead of one per formula?
4. **Macro provenance.** Does M2 need exact historical macro contexts pinned for each check, review and response (B-12)? Or is recording the macro revision used enough?
5. **Tampered store.** Should the code defend against a tampered or corrupted control store (B-10), when the threat model treats Z2 state as trusted?
6. **Sequencing.** Should the ledger wait until B-01, B-02 and B-04's receipts, events and notices exist (roughly 1 to 2 weeks of refactoring), so that the ledger is their first new client?
