# Review notes: E — research workspace GUI and its server

Baseline: main at fa456bc. Reviewed in depth: every module of `packages/workspace/src` (app, api, documents, reviews, jobs, kernels, plans, plan_edits, provisioning, provisioning_client, project_settings, mathrepair, cli; repair_prompt skimmed), the web app's `App.svelte`, `router.ts`, `api.ts`, `kernels.ts`, `plans.ts`, `drafts.ts`, `documentRequests.ts`, `reviews.ts`, `Markdown.svelte`, and the session/document/review views (Session, Project, Document*, AnnotationThread, CommentBodyEditor, FindingCard, ReviewsPanel, JobsPanel, KernelPanel, RunSection), plus `docs/WORKSPACE.md`. Skimmed for structure: `provisioning.ts`, `planEdits.ts`, `EnvironmentPanel.svelte`, `PlanPanel/PlanEditor`, `markdown.ts`, `mathrepair.ts`, the server tests (shape, private access, sleeps) and the license-notice build plugin. Ran: workspace pytest (555 passed, 6.2 s), `npm ci --ignore-scripts`, `npm run check` (0 errors, 0 warnings), vitest (828 passed, 3.7 s), a probe of the jobs route with an unreachable worker, and the math-check bundle built into scratch to inspect its contents. Skipped: the correctness of the math-repair rules, the KaTeX-lexer emulation and browser/server parser parity (three adversarial reviews and shared fixtures cover them), CSS, and the license tooling's internals.

## Summary

The area has two halves. One half is very good and should be kept as is: the local login/origin/Host/CSP boundary, the read-only/writer store split, the strict document-change schema, and above all the one Markdown+KaTeX renderer with its shared parser fixtures. The other half is a set of record viewers and editors. They grew one feature at a time, with no shared server or client structure.

Lines understate the client code: some lines run to 1,412 characters, and there is no formatter. Measured in bytes of code plus tests:

| Feature | Size |
| --- | --- |
| Provisioning observations (a read-only inspector) | ≈ 242 KB |
| Session plans (read-only panel plus body edit) | ≈ 183 KB |
| Kernels | ≈ 40 KB |
| Documents and comments (routes, editor, comment/history views, server tests) | ≈ 81 KB |
| Reviews | ≈ 49 KB |

The passive inspectors carry an "owned bounded proxy" pattern four times on the server and four times on the client. It consists of per-login admission, shielded thread ownership, inode proofs on the project registry, repeated logout re-checks and deep re-validation of responses. All of this is outside the single-user threat model. The document routes do real writes and work fine with plain `to_thread` plus idempotent operation IDs.

For the M2 exit path, the GUI cannot do any of these: create a session, start or chat with a run, see a run in progress, view a PDF, show a ledger or board, or answer an approval. Most of these first need a backend boundary that does not exist yet:
- there is no control process;
- run records are written once, at the end;
- there is no ledger module;
- approvals exist only as a CLI over the event store.

The changes that matter most:
1. **E-01, E-02:** one feature-registration shape for the server, with a shared context, one error mapper and a cleanup stack. Replace the four proxy classes and the three inode-checking registry readers with plain handlers: about 1,000 net source lines and 2,000+ test lines go.
2. **E-03:** one client transport, one `Poller` and one session/logout module, in place of four fetch copies and four polling variants.
3. **E-06, E-07:** decide where the run lifecycle lives before building chat. I recommend a minimal orchestrator control API that the workspace proxies, as it proxies the worker. The harness would persist run events incrementally.
4. **E-05:** one guarded-write/draft primitive. Chat, approvals and ledger edits will all need it; today it is hand-wired in seven views and reimplemented for plans.
5. **E-16, E-15:** a formatter, done first and alone, then the core views' interaction logic moved into testable controllers.

## Findings

### E-01: Four copies of an "owned bounded proxy", three inode-checking registry readers — beyond the threat model, and the template every new panel copies
- Where:
  - Proxy classes: `kernels.py:40-201` (KernelProxy), `plans.py:189-377` (PlanReader), `plan_edits.py:74-378` (PlanEditor), `provisioning.py:498-682` (ProvisionProxy).
  - Registry readers: `plans.py:50-109` (`session_snapshot`), `provisioning.py:61-222` (`ProjectSnapshot`, `project_snapshot`), and orchestrator `projects.py:121-161` and `projects.py:309-370`.
- Category: over-engineering, duplication
- Priority: P1
- Evidence:
  - **The repeated parts.** Every class repeats:
    - a shielded executor loop: `kernels.py:54-73`, `plans.py:204-221`, `provisioning.py:510-533`, `plan_edits.py:92-108`;
    - admission through `_tasks`, `_scopes` and `_logins`, with MAX_CALLS=8, MAX_LOGIN_CALLS=2 and CALL_TIMEOUT=6: `kernels.py:139-177`, `plans.py:309-352`, `provisioning.py:600-657`, `plan_edits.py:293-339`;
    - a shielded `_drain`/`close`: `kernels.py:179-201`, `plans.py:354-377`, `provisioning.py:659-682`, `plan_edits.py:341-378`;
    - the "login ended" check, three to five times per request;
    - its own error→status table.
  - **The registry readers.** They reopen `<project>.json` by descriptor with `O_NOFOLLOW` and compare (dev, ino, size, mtime, ctime) plus a SHA-256 before and after the work. `plans.session_snapshot` is a near-copy of `ProjectStore.session_snapshot`. `ProjectSnapshot.matches` also re-proves the pathname (`provisioning.py:91-132`).
  - **Plan edits go further.** They take the project's `flock` through the snapshot's private `_root` descriptor (`plan_edits.py:210-234`). They also inode-check `control.sqlite3` (`plan_edits.py:110-118`) and re-read the registry three times (`plan_edits.py:205, 221, 226, 246`).
  - **What these defend against:**
    - the single trusted user's own browser overloading the server;
    - a request finishing after that same user logs out;
    - the trusted harness replacing its own registry between two reads (ABA);
    - the trusted worker returning another scope.

    None of these is a threat in `THREAT-MODEL.md`: there is one trusted user (D15), Z2 state is trusted, and T6 is about rendered content.
  - **The simpler routes work.** `documents.py:149-211` (real USER writes) and `jobs.py` (a worker proxy with cancel) use plain `asyncio.to_thread` and operation IDs, and are adequate.
  - **A user-visible side effect.** `scope in self._scopes` refuses a second concurrent read of the same session with a 503. Two tabs on one session therefore intermittently show "unavailable".
  - **The tests mirror the machinery.** The plans, plan-edits, kernels, provisioning, lockfile-diff and revision-package tests total 4,011 lines. They assert on private members (`reader._tasks`, `reader._scopes`) and use fixed sleeps, for example `test_plans.py:648-657` sets CALL_TIMEOUT=0.03 and then sleeps 0.06 s.
- Proposal: one route shape:
  1. validate the path IDs;
  2. `await asyncio.to_thread(view)` under `asyncio.timeout`;
  3. answer through the shared response/error helpers (E-02).

  Read the registry with `ProjectStore.get()`, or one public `session(project, session)` accessor; drop the descriptor snapshots. Keep byte caps only on untrusted-sized inputs; worker responses already pass through WorkerClient's caps. For plan edits, rely on `SessionPlans.revise`'s CAS on the plan ref plus the operation ID, as documents do. Drop the lock file and the inode proofs. If admission is wanted at all, use one app-wide semaphore middleware. Rewrite the tests as behavioural route tests: 200 shapes, 401/404/409/503, no-store, origin guard, timeout → 503.
- Effort: M
- Risk and verification: these guarantees passed browser acceptance, and three docs advertise them (`SESSION-PLAN-WORKSPACE.md`, `WORKSPACE-KERNELS.md`, `PROVISIONING-WORKSPACE.md`), so retiring them is an owner decision (Q1). First add route-level behaviour tests that survive the refactor, then delete the ownership tests with the code. The fast suite (6 s) stays fast.
- Depends on / enables: do together with E-02. It enables chat, ledger and approval routes without a fifth copy. Cross-area A: orchestrator `projects.py:460-510` and `plan_tools.py` use the same snapshot/lock idiom.

### E-02: `create_app` wiring, error mapping and cleanup are ad hoc per module; worker unavailability returns HTTP 500
- Where: `app.py:211-391` (`create_app`, 180 lines), `app.py:358-385`, `app.py:324-338`, `app.py:263-268`, `app.py:120-125`; `documents.py:32-38`, `documents.py:270-277`; `kernels.py:239-256`; `plans.py:413-434`; `plan_edits.py:400-456`; `provisioning.py:716-743`; `provisioning_client.py:91-108`; `jobs.py:52-61`; `project_settings.py:5`
- Category: architecture, duplication, correctness
- Priority: P1
- Evidence:
  - **Wiring.** Four route modules take `(app, store, worker|document_store, principal-lambda, authorized-lambda)`. The same two lambdas are written out four times (`app.py:362-384`), although `app[api.STORE]` already holds the store.
  - **Cleanup** is a try/finally nested five deep (`app.py:324-338`).
  - **Cache headers.** `secure()` special-cases route names for `Cache-Control` (`app.py:265-268`), although those handlers already set it.
  - **Seven error tables and five response helpers, which disagree.** `app._STATUS` lacks CONFLICT, UNAVAILABLE and TIMED_OUT, so `jobs.py` errors fall to 500. Verified: `GET …/jobs` with an unreachable worker answered `500 {"kind": "unavailable", "message": "cannot reach the worker: Cannot connect to host 127.0.0.1:9 …"}`. The message also carries an internal address. Kernels and plans map unknown errors to 503; documents map `busy` to 503.
  - **Private use.** `project_settings.py` turns off pyright's private-usage check file-wide, only to call `documents._answer`.
  - **View logic in route modules:** `documents.py:280-351` (`_revision`) and `reviews.py:157-187` (`contexts`).
- Proposal:
  - A `WorkspaceContext` (projects, documents read-only, writer, worker, job_state, auth with `principal(request)` and `authorized(request)`), set once as an AppKey.
  - Each feature module exposes `register(app, ctx)` and an optional `async close()`. The app closes them in reverse order, or through `contextlib.AsyncExitStack`.
  - One `errors.py` with `to_response(exc)` for ProjectError, DocumentError, WorkerAPIError and ValidationError, and one ErrorKind→status table: unavailable and timed_out → 503, conflict → 409.
  - One `json_ok()` that sets no-store.
  - Public names for the document helpers.
  - View assembly moved out of the route modules (E-12).
- Effort: S–M
- Risk and verification: mechanical; the 555 route tests cover it. Add a test that an unreachable worker → 503.
- Depends on / enables: E-01. It enables the second origin (E-08) and new feature modules.

### E-03: The client has four bounded-fetch copies and four polling variants, and its logout fence lives in `kernels.ts`
- Where:
  - Fetch copies: `api.ts:396-447` (observeKernels), `plans.ts:152-188`, `provisioning.ts:324-357`, `planEdits.ts:85-121`.
  - Pollers: `kernels.ts:83-152` (KernelPoller), `plans.ts:191-232` (PlanPoller), `provisioning.ts:362-401` (ProvisionReader), and an ad hoc loop in `JobsPanel.svelte:13-31`.
  - Logout fence: `kernels.ts:154-166`, re-exported as `plans.ts:235` `onPlanLogout = onKernelLogout`.
  - Timestamp validators: `kernels.ts:45-55`, `plans.ts:54-63`, `provisioning.ts:107-115`.
- Category: duplication, architecture
- Priority: P1
- Evidence:
  - **The fetch copies.** Each re-implements:
    - AbortController plus a deadline;
    - redirect refusal and a Content-Length pre-check;
    - a chunked reader with byte and chunk caps;
    - fatal UTF-8 decoding;
    - a module-global "owned" flag;
    - its own typed error class.
  - **The pollers.** The three classes are line-for-line equivalents: visibility pause, epoch, backoff to 30 s, 401 → signed_out, 404 → missing.
  - **The jobs loop is the odd one out.** `JobsPanel` polls every 3 s with no visibility pause and no backoff, and it keeps polling after a 401 (`JobsPanel.svelte:23-27`).
  - **The ordinary helpers have none of this.** `get` and `post` (`api.ts:378-392`) have no deadline and no bounds.
  - **The runs list never refreshes.** `SessionView.svelte:16` loads the session once, so a run that ends does not appear, while the plan, jobs and kernel panels on the same page do update.
  - **Small helpers repeat** in kernels, plans, planEdits and provisioning: `object`, `id`, `text`, `counter`, the timestamp check.
- Proposal: three shared modules.
  - `lib/http.ts`: `request<T>(method, path, {signal, deadlineMs, maxBytes?, body?})` → T or `ApiError`.
  - `lib/session.ts`: one signed-out state and the 401 handling, which every request consults.
  - `lib/poller.ts`: a generic `Poller<T>` (visibility, epoch, backoff, phases).

  Port jobs, kernels, plans, provisioning and the runs list to the poller; the live transcript and ledger will use it later.
- Effort: M
- Risk and verification: the existing kernel, plan and provisioning tests drive the pollers with fake reads. Write the `Poller` tests once, then adapt them. Keep the logout semantics: no read starts after logout begins.
- Depends on / enables: E-04 decides how much validation remains. Enables E-07, E-10 and E-11.

### E-04: Response handling is split between hand-written deep validators and unchecked casts, with no shared API contract
- Where:
  - Deep validators: `plans.ts:47-143` (`planObservation`), `kernels.ts:57-80`, `provisioning.ts:100-280` (`provisionData`), `planEdits.ts:70-76` and `128-149`.
  - Unchecked casts: `api.ts:378-392` (`as T` for documents, comments, jobs, runs) and `reviews.ts:1-51`.
  - Server-side re-derivation: `plans.py:112-182`, which recomputes orchestrator step hashes (`plans.py:144-157`).
- Category: duplication, over-engineering
- Priority: P2
- Evidence:
  - **Inspector responses are re-checked field by field in the browser:** calendar-correct timestamps, hash formats, cross-field coherence, and plan completion rules restated from the server.
  - **Documents, comments and reviews are cast.** Yet they are the data that actually reaches the renderer.
  - **The validators are TS rewrites of Pydantic models** (CompactPlan, ProvisionObservation, …) and drift silently.
  - **Review and finding types mirror raw SQLite rows,** for example `cap_reached: boolean | number` and `created: number`. The server side is `Json = dict[str, Any]` (`reviews.py:19-20`).
  - **The safety boundary is elsewhere.** For untrusted text it is escaping (Svelte text plus `markdown.ts`), under CSP `connect-src 'self'`, not schema checks on the app's own server.
- Proposal:
  - Generate the TS types from the server's Pydantic response models: export JSON Schema with a script checked in CI, as `generate_protos.py --check` is.
  - Add response DTOs for reviews, findings and math checks.
  - Keep only cheap scope checks that protect UI state ("does this answer belong to the selected scope?").
  - Drop deep re-validation of same-origin responses.
- Effort: M
- Risk and verification: this removes checks the docs advertise (Q1). A schema-to-TS generator would be a new devDependency; a small own script avoids one.
- Depends on / enables: E-03, E-13. Cross-area B, whose review store returns raw dicts.

### E-05: The draft / operation-ID / CAS write pattern is copied into seven views over a document-shaped draft; plans have a separate 240-line implementation
- Where:
  - Shared helpers: `drafts.ts:3-9`, `documentRequests.ts:6-24`.
  - Views:
    - `DocumentEditor.svelte:13-72`;
    - `DocumentComments.svelte:16-63`, which stores `retained: JSON.stringify({kind, math, quote, session…})` at line 40;
    - `AnnotationThread.svelte:17-61`, with `reason: mode, retained: session` at line 34;
    - `CommentBodyEditor.svelte:14-37`, with `reason: 'edit_body', retained: JSON.stringify(...)` at line 28;
    - `FindingCard.svelte:17-42`, with `reason: action, retained: references` at line 24;
    - `ProjectMacros.svelte:10-28`;
    - `PatchReview.svelte:24-25`.
  - Separate plan implementation: `planEdits.ts:123-239` (PlanEditSession).
- Category: duplication, missing abstraction
- Priority: P2
- Evidence:
  - **Hand-wiring in each view.** Each wires up, by hand:
    - a `loadDraft`/`saveDraft` effect;
    - `documentRequest` (a SHA-256 of the body mapped to a stored UUID);
    - the POST and `documentAnswered`;
    - `clearDraft`;
    - the conflict display;
    - the "storage unavailable" warning.
  - **The shared draft type is stretched to fit.** The one `Draft` shape `{base, text, reason, block, retained}` holds comments, findings and macros by stuffing unrelated values into it, so `drafts.ts:23-25` cannot validate them. Finding responses call `documentRequest` with a non-document key.
  - **Plans do the same differently:** their own storage key, format and validation, `plan-edit-<uuid>` IDs, and unknown/acknowledged states.
  - **Failure handling is uneven.** `DocumentEditor.svelte:50` `resolve()` has no error handling.
  - **None of these interactions has a test** (E-15).
- Proposal: a `lib/mutation.ts` `GuardedWrite<Draft, Body, Result>` providing:
  - a typed, versioned draft store, with a kind and a schema per kind;
  - retention of the operation ID by (kind, scope, body hash);
  - the states idle, saving, conflict, uncertain and done;
  - a `retry()` that reuses the operation ID.

  Wrap it in a thin Svelte rune class. Port DocumentEditor first, then comments, findings and macros, then plan edits.
- Effort: M
- Risk and verification: draft persistence across reloads passed browser acceptance. Migrate the old keys (`document-draft:`, `plan-text-draft:`) or accept losing drafts once. Test it as plain TS.
- Depends on / enables: E-03. Enables a chat composer, approvals and ledger edits.

### E-06: The GUI cannot create a session or start or chat with a run, and no backend boundary supports it
- Where: `ProjectView.svelte:36-37` ("A session starts with `sandbox-science-harness run`"); orchestrator `cli.py:740` (`run`: one task to an answer); `WORKSPACE.md:219` ("no automatic idle wake or server-side run scheduler"); `ARCHITECTURE.md:9` (the GUI as an API proxy); the W4 layering exception in CONVENTIONS ("does not grant the workspace worker or agent authority").
- Category: architecture
- Priority: P1
- Evidence:
  - There is no long-lived control process; each run is a CLI process.
  - The only user→agent channel is a document work request (an addressed comment). It is delivered at a MAIN safe point, or to the next manually started run.
  - If the workspace process started runs itself, the GUI server would take on the harness's model and worker authority, widening the layering exception.
  - The backlog has "live updates while a run is in progress" and "live message transport, idle wake-up", but no explicit GUI chat item.
- Proposal: pull a minimal control API forward from W5: `sandbox-science-harness serve` on a Unix socket or loopback port, with a token that only the workspace holds. It would offer:
  - create a session;
  - start a MAIN run for a session with a user message;
  - post a user message to a session, stored as an addressed request: generalize the work-request table, so that documents and chat share delivery and acknowledgement;
  - cancel a run;
  - run events by cursor (E-07).

  The workspace adds thin proxy routes, shaped like `jobs.py`, and a conversation pane on the session page.

  Minimal alternative with no new process: the workspace writes chat messages as session-addressed requests, as it already writes work requests. A separate launcher runs `harness run` when a session has pending requests and no active run.
- Effort: L
- Risk and verification: a click now spends model budget, so idle wake and budgets need the owner's decision (Q2, Q3). Keep worker keys out of the workspace. Run cancellation and restart need explicit rules.
- Depends on / enables: E-07, E-02 (proxy module), E-05 (composer). Cross-area A (loop safe points, request delivery).

### E-07: There is no in-progress transcript; a run record is written once at the end and fetched whole
- Where: `WORKSPACE.md:335` ("Each record is written once") and `WORKSPACE.md:405`; `api.py:34` (`MAX_RUN_BYTES` 64 MiB) and `api.py:121-124`; `RunSection.svelte:20-23`; `SessionView.svelte:16`
- Category: architecture, performance
- Priority: P1 (for chat)
- Evidence: during a run, the GUI shows plan progress, jobs and kernels, but not the conversation. A finished run arrives as one JSON document of up to 64 MiB, parsed whole. The Markdown goes are bounded, but this payload is not.
- Proposal:
  - The harness appends run events (message, tool call, bounded tool-result summary, notices, stop) to an append-only `run_events(run, seq)` table in `control.sqlite3` as they happen. The final JSON record stays.
  - The workspace serves `GET …/runs/{run}/events?after=N&limit=M`, consumed by the generic `Poller` (E-03); SSE can come later.
  - RunView renders incrementally, and the runs list polls.
- Effort: L (mostly orchestrator)
- Risk and verification: notice and request acknowledgement must still follow the durable final save. Events are observations, not the acknowledged record.
- Depends on / enables: cross-area A (`loop.py`, `cli.py`); E-03; enables E-06.

### E-08: A PDF viewer and rich previews need a second origin, which the server is not structured for
- Where: `app.py:80-101` (one CSP), `app.py:211-391`; `WORKSPACE.md:109` and `WORKSPACE.md:401`; THREAT-MODEL T6; D54
- Category: architecture, security
- Priority: P2. It is on the M2 path: the "paper" step and fixture step 1 ("import a small mathematical PDF, inspect an equation").
- Evidence: the server has one application, one listener, one cookie and one CSP (`frame-ancestors 'none'`, `default-src 'none'`). D54 needs previews on a separate origin, in sandboxed frames, with PDF.js scripting off. The workspace has no artifact read route; the backlog lists "public read/download routes, PDF anchors and rich previews" as open.
- Proposal: after E-02, add a second application on a second port, which is a distinct origin:
  - its own CSP: `sandbox`, and scripts only for a bundled PDF.js viewer;
  - no session cookie: the main app mints short-lived capability URLs (an HMAC of artifact ID and expiry);
  - artifacts read through the orchestrator or worker artifact API, never from the filesystem.

  The main app embeds `<iframe sandbox="allow-scripts">` and adds `frame-src` for that origin. Page and region anchors come back through `postMessage`, with origin checks.
- Effort: L
- Risk and verification: PDF.js is Apache-2.0 (tier 1). Cookies are shared across ports on one host, so the preview origin must never rely on cookies; the session cookie's name already carries the port.
- Depends on / enables: E-02; the artifact inventory (another area).

### E-09: Approvals in the GUI have no data path and no re-authentication mechanism
- Where: D76 (answers come from `sandbox-science-policy-middleware approve/deny`); `WORKSPACE.md:78` and `WORKSPACE.md:400`; D54
- Category: architecture, security
- Priority: P2
- Evidence: permission requests live in the event store, which belongs to policy-middleware and the MCP proxy, another deployable. Reaching them would mean importing a second deployable, a wider layering exception, or calling an API that does not exist. Login is a one-time token URL, so D54's re-authentication has nothing to re-check.
- Proposal: give policy-middleware, or the control API (E-06), a small authenticated approvals API: list pending requests, and answer one by fingerprint with the expected state. The workspace proxies it. Build prompts only from recorded request facts. For a single local user, re-entering the printed login token within a short window (`/reauth`) would satisfy D54 without passwords (Q4).
- Effort: L
- Depends on / enables: E-02, E-05; cross-area D.

### E-10: The ledger and board are blocked on a missing backend; prepare the generic pieces instead
- Where: the orchestrator has no ledger module (only references in `documents/exports.py`, `delegation_records.py`, `session_plans.py`); `plans.ts:108` already accepts `kind: 'ledger'` evidence; `WORKSPACE.md:399`
- Category: architecture
- Priority: P2 (blocked)
- Evidence: plan evidence can name ledger entries, but there is nothing to show or link to.
- Proposal: do not start GUI work before the ledger store exists. Prepare with E-03 (paginated resource and poller), E-05 (guarded write), and a generic cursor-pagination helper; ReviewsPanel hand-rolls cursor paging six times.
- Effort: — (feature work)
- Depends on / enables: ledger spec and store (another area).

### E-11: The review and finding views are debug-grade for the M2 "user feedback → automatic review" step
- Where: `ReviewsPanel.svelte:9-14` (24 `$state` variables, three generation counters), `ReviewsPanel.svelte:83, 93, 101-105, 110` (`JSON.stringify` in `<pre>`); `FindingCard.svelte:63, 66`; `RunSection.svelte:35-37`
- Category: other (usability), complexity
- Priority: P2
- Evidence: one component holds the review list, review detail, evidence slices, audit records, math checks and findings, with six hand-rolled cursor paginations. Audit records, queue state, locators, unreviewed windows, classifier identity and math evidence are all shown as pretty-printed JSON. A user must read JSON to learn what a review concluded.
- Proposal:
  - Split the panel into ReviewList, ReviewDetail, MathChecks and Findings, each on a paginated-resource helper.
  - Render the common fields as UI: status, severity, remedy, history, and an anchor that links to the document block (`documentHref` with the block in focus).
  - Keep the raw JSON behind a details toggle.
- Effort: M
- Depends on / enables: E-03, E-04, E-13.

### E-12: The workspace reaches into orchestrator internals: private store access, raw SQL, the registry lock protocol
- Where: `plans.py:233-245` and `plan_edits.py:127-139` (raw SQL on `session_plan_records` and `session_plan_projections` through `_read()`); `reviews.py:164` (`store.documents._read()`); `plan_edits.py:212-234` (`flock` on `<project>.lock` through `SessionSnapshot._root`, duplicating orchestrator `projects.py:470-510` and `projects.py:664-670`); `project_settings.py:5`; inline imports at `documents.py:331`, `reviews.py:158` and `reviews.py:213`
- Category: coupling
- Priority: P2
- Evidence: five private-member uses carry pyright suppressions, and one file turns the check off entirely. Table names and the registry lock protocol are re-implemented in the GUI server, so a schema or locking change in the orchestrator breaks the workspace without any visible dependency.
- Proposal: add public orchestrator queries, such as `SessionPlans.exists(scope)` and `finding_anchors(project, finding)` on a store, and route every workspace read and write through orchestrator public methods. This is also the seam a W5 control API would expose. The plan-edit lock goes with E-01.
- Effort: S–M
- Depends on / enables: E-01; cross-area A.

### E-13: The API is inconsistent: time formats, status codes, raw-dict payloads, undocumented routes
- Where:
  - Epoch-second times: reviews' `created: number`; `CommentEntry.created_at: number` (`api.ts:328`), shown with `new Date(entry.created_at * 1000)` (`AnnotationThread.svelte:76`). Everything else uses ISO strings with `formatTime` (`format.ts:4`).
  - `reviews.ts:7` `cap_reached: boolean | number`.
  - Status codes: E-02.
  - Unused route: `GET …/jobs/{job}` (`jobs.py:34`) has no client.
  - The API table at `WORKSPACE.md:244-256` lists 13 of the 41 registered API routes (counted from `app.router`).
- Category: docs drift, other (API consistency)
- Priority: P2
- Proposal: one API convention:
  - ISO-8601 UTC strings for all times;
  - real booleans;
  - Pydantic response DTOs;
  - one error envelope with the E-02 status mapping.

  Generate the route table in the docs from `app.router`, with a test that the doc matches the registered routes. Use or remove `GET jobs/{job}`.
- Effort: S–M
- Depends on / enables: E-02, E-04; cross-area B.

### E-14: `mathrepair.ts` mixes six concerns, and its module side effect ships the browser API client inside the trusted Node math checker
- Where: `web/workspace/src/lib/mathrepair.ts`, 1,441 lines, holding:
  - rules: ~31-270;
  - the definition/trust lexer: 273-436;
  - the `Kept` cache: 438-505;
  - the Unicode approximation: 517-950;
  - recovery: 951-1098;
  - the ModelRepairs client: 1099-1369;
  - the HTML for marks: 1375-1441.

  The side effect comes from line 22 (`import { api } from './api'`) and lines 1367-1369 (`export const pageRepairs = new ModelRepairs(...)`). `mathcheck.ts:5` imports the module; see also `vite.math.config.ts`.
- Category: architecture, dead code
- Priority: P3
- Evidence: I built `vite build --config vite.math.config.ts` into scratch. The resulting `mathcheck.mjs` (538 KB) contains `fetch('/api/v0…')`, `observeKernels`, the logout POST and `new ModelRepairs(...)`. The module-level instance pulls the browser API client and the kernel/logout fences into the trusted server-side checker. It is inert in Node (`window` is undefined), but it is dead code inside a trusted component.
- Proposal: split the file into pure modules: `math/rules.ts`, `math/definitions.ts` (security-relevant), `util/kept.ts`, `math/unicode.ts` and `math/recover.ts`. Keep a browser-only `math/modelRepairs.ts`, and create `pageRepairs` in `main.ts` or `Markdown.svelte`. `mathcheck.ts` then imports only pure modules.
- Effort: S–M
- Risk and verification: low. `server/math-license-notices.test.mjs:30` lists module IDs and needs updating. Check with the 24 post-build math cases and the existing 799-line test.
- Depends on / enables: none.

### E-15: The core interactive views are untested, while test effort is concentrated on proxy ownership
- Where:
  - No test imports DocumentEditor, DocumentComments, PatchReview, HistoryView, ReviewsPanel, JobsPanel, SessionView, ProjectView, RunSection, ToolCall or ProjectMacros.
  - The view tests that do exist render through `svelte/server`, in a `node` test environment (`vite.config.ts:22-25`), so nothing tests interaction.
  - On the server, plans, kernels and provisioning have 4,011 test lines; documents and macros have 654; reviews has 345.
  - Timing-dependent tests: `test_plans.py:657`, `test_kernels.py:329` (`sleep(0.1)`), and 10 sleeps in `test_provisioning.py`.
- Category: tests
- Priority: P2
- Evidence: the save, conflict, resolve and retry flows for documents, comments and findings are verified only by recorded manual browser acceptance.
- Proposal: with E-05, move the interaction logic into TS controllers and unit-test those; no DOM is needed. Optionally add happy-dom (MIT) for one smoke test per view. On the server, swap the ownership tests for behaviour tests (E-01).
- Effort: M
- Depends on / enables: E-05, E-01.

### E-16: The web app has no formatter, and very long lines make it hard to review
- Where: longest lines by file: `provisioning.ts` 1,412 characters (105 on average), `ReviewsPanel.svelte` 873, `FindingCard.svelte` 600, `planEdits.ts` 593, `EnvironmentPanel.svelte` 565; `package.json:11-17` has no format script, and ruff covers only Python.
- Category: other (reviewability)
- Priority: P2
- Evidence: 7,935 lines of source come to about 410 KB. Several files carry multiple statements per line, so line counts understate their size two to three times.
- Proposal: add prettier and prettier-plugin-svelte (both MIT) as devDependencies, with a format check in `npm run check` and in CI. Land one formatting-only pull request before any refactor.
- Effort: S
- Risk and verification: a large diff, so keep it alone, with no logic changes. Run the license check on the new packages.
- Depends on / enables: makes the refactors above reviewable.

### E-17: Plan edits use the session cookie's digest as the USER identity
- Where: `app.py:376` and `app.py:383` (principal = `_digest(cookie).hex()`); `plan_edits.py:222-224` and `plans.py:232` (`PlanAuthority(..., user_id=login)`); orchestrator `session_plans.py:116-119` (`namespace` uses `user_id`); `plans.ts:74`
- Category: correctness, other (attribution consistency)
- Priority: P3
- Evidence: the SHA-256 of the browser's session cookie becomes:
  - the durable author of plan versions;
  - the namespace for operation IDs.

  It changes at each login and restart, and is returned to later sessions in plan responses. Documents, comments and findings instead use the fixed `USER` author (`documents/models.py:115`). The UI already warns that a retry after logging in again "does not prove it is the same operation".
- Proposal: use a stable USER identity for attribution and the operation namespace. Keep the digest only for in-memory admission or rate limits.
- Effort: S
- Risk and verification: existing plan records carry digests; display them as USER.
- Depends on / enables: none.

### E-18: The workspace holds a worker token that can also execute code (hardening)
- Where: `cli.py:193-207`; `WORKER.md:107` (client tokens are scoped by project, not by operation); `jobs.py`, `kernels.py`, `provisioning_client.py`
- Category: security
- Priority: P3
- Evidence: the workspace needs only observation, listing and cancellation. Its token, whether the pool's or a project client's, also authorizes execution. A workspace bug or a future proxy route could therefore run code.
- Proposal: add an operation scope to worker client tokens (an `observer` scope: read plus cancel). Until then, document that the workspace should get a per-project client token, never the pool's.
- Effort: S–M (a worker change, area C)
- Depends on / enables: none.

### E-19: The display-repair provenance duplicates the prompt format, and the page ignores it
- Where: `repair_prompt.py` (228 lines) and `test_repair_prompt.py` (364 lines); `mathrepair.py:344-346` (`assemble_repair(...)` then `wanted.verify(messages, ())`, checked against `_messages()` at `mathrepair.py:232-238`, which builds the same text); `mathrepair.py:129-130`. `input_provenance` appears nowhere in `web/workspace/src`.
- Category: over-engineering, dead code
- Priority: P3
- Evidence: two implementations of the same user message (`RequestFormat.question` and `_messages`) are checked against each other on every repair. They produce an additive field that the browser ignores and nothing persists (`WORKSPACE.md:201-207`).
- Proposal: build the messages in one place, and defer the descriptor until LD-46 has durable call records, or delete it (Q7).
- Effort: S
- Depends on / enables: none.

### E-20: `WORKSPACE.md` has accreted: a misplaced introduction, stale statements, narrative logs
- Where: `docs/WORKSPACE.md`:
  - lines 3-9: an environment-panel paragraph placed before the document's introduction;
  - line 74: "The routes that take a POST are logout, math repairs, and job cancellation; approvals and edits will." There are 9 POST routes;
  - lines 240-262: an API table with 13 of the 41 routes;
  - lines 397-407: "Not built yet", followed by sections appended after it (lines 410-456);
  - lines 182-197 and line 389: review and test narratives, the latter one paragraph of roughly 2,500 words;
  - also the docstring at `app.py:9-13`.
- Category: docs drift
- Priority: P2
- Proposal: restructure into Overview, Running, Security boundary, API (the generated table from E-13), Views, Rendering rules, Limits and Not built. Move acceptance and review histories to `.agents/docs/agent-reports`, and give each feature one paragraph linking to its own document.
- Effort: S–M
- Depends on / enables: E-13.

### E-21: The main-thread KaTeX mitigations could shrink once KaTeX runs in a Web Worker
- Where: `markdown.ts:115-250` (the go scheduler and module-level state), `mathrepair.ts:273-436` (KaTeX-lexer emulation that refuses definitions); the backlog item "KaTeX in a Web Worker"
- Category: complexity
- Priority: P3
- Evidence: time-slicing and definition refusal exist because a single formula on the page thread can block for many seconds, as three adversarial reviews showed. A worker with a hard timeout would bound that directly.
- Proposal: keep the current code; it is tested and works. Once the worker exists, re-evaluate whether the go scheduler and the definition lexer can shrink. Not a pre-M2 item.
- Effort: M–L
- Depends on / enables: none.

## Keep

- **The local security boundary** (`app.py:128-208`, `app.py:236-261`, `app.py:394-439`):
  - a token login printed once;
  - a port-named HttpOnly, SameSite=Strict cookie stored as a digest;
  - an Origin check on every non-GET request and no CORS;
  - a Host check against DNS rebinding;
  - the CSP and headers on every response, errors included;
  - static file serving that refuses bad segments and escaping symlinks.

  All of it has negative tests (`test_app.py`). It matches the single-user Z4 threat model closely.
- **The read-only/writer split.** GET routes read documents through a `mode=ro` connection (`app.py:234`, `documents.py:242`), so a GET cannot write even through a bug. Writes use a separate writer.
- **The document-change endpoint** (`documents.py:43-76`, `documents.py:149-211`): a strict Pydantic discriminated union with `extra=forbid`, a USER identity the server derives, operation IDs and a CAS base. This is the right template for every write route, chat included.
- **The renderer** (`markdown.ts`, `Markdown.svelte`, `dollarmath.ts`, `mathdelimiters.ts`, `blockmap.ts`, `attach.ts`): `html: false`, a link allowlist, images as links, KaTeX `trust: false` with limits, visible bidi controls, `contain: paint`, time-sliced rendering, and copying of the original LaTeX. Its fixtures are shared with the server parser and run on both sides in CI. This is the strongest part of the GUI.
- **The math-repair service** (`mathrepair.py`): single-flight requests, an LRU cache, a per-session limit, key scrubbing in logs, no redirects followed, bounded answers.
- **Fast, clean suites:** 555 Python tests in 6.2 s, 828 web tests in 3.7 s, svelte-check 0/0, and npm installs with scripts off plus license and integrity checks.
- **A small hash router** without a library (`router.ts`), and consistent use of Svelte 5 runes.

## Questions for the owner

1. **Q1.** May the "owned bounded proxy" guarantees be retired as beyond the single-user threat model? They are per-login admission, logout re-checks, registry inode and lock proofs, and deep response re-validation (E-01, E-04).
2. **Q2.** Where should "start a run" and "send a message" live for M2: a minimal control API pulled forward from W5, or a launcher that runs `harness run` for sessions with pending messages? May a GUI message wake an idle session, and so spend model budget (E-06)?
3. **Q3.** Is chat with MAIN from the GUI part of the M2 exit? The backlog has no explicit item for it.
4. **Q4.** For D54 re-authentication with one local user, is re-entering the printed login token within a short window enough, or do you want a password or passkey (E-09)?
5. **Q5.** Should further passive-inspector work (provisioning, kernels, plan provenance) stop until the M2 path exists: chat, live transcript, PDF viewer and ledger?
6. **Q6.** May prettier and prettier-plugin-svelte be added, and possibly a schema-to-TS generator, as devDependencies (E-16, E-04)?
7. **Q7.** Should the display-repair provenance descriptor be kept, deferred to LD-46, or removed (E-19)?
