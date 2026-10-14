# Research workspace

The workspace is the research GUI (W4, D80). It shows the harness's records: the projects, their sessions and environments, and each session's runs, with the agent's messages, its tool calls and their results, and the LaTeX in them rendered. It shows the project's living documents, each revision with the server's block IDs and who changed what, and lets the user edit and comment on them ([living documents](#living-documents)). LaTeX that KaTeX rejects is repaired where it can be, by rules or by the reviewer model, and marked ([math repairs](#math-repairs)). It has two parts:

- `web/workspace/`: the app the browser runs. Svelte 5 with TypeScript, built by Vite into static HTML, scripts, stylesheets, KaTeX's fonts and distribution notices.
- `packages/workspace` (`sandbox_science.workspace`): the server, in the project's Python on aiohttp, with the command `sandbox-science-workspace`. It serves the built files and a JSON API, handles login, and asks the reviewer model for math repairs when it is configured to.

Besides runs and documents, the GUI shows a session's plan and progress, with a text editor for the plan ([session plans](#session-plans)); its kernels ([session kernels](#session-kernels)); its background jobs, with cancellation ([background jobs](#background-jobs)); its consistency reviews and math checks, with the user's decisions on findings ([consistency reviews](#consistency-reviews-and-math-checks)); a project's macros; and the recorded provisioning state of a project's environments ([provisioning](#provisioning-observations)). The ledger and board, approvals, chat and a live transcript are not built ([not built](#not-built)).

## Building and running

```bash
cd web/workspace
npm ci --ignore-scripts      # exactly the packages in package-lock.json; no install scripts
npm run build                # writes dist/
cd ../..
uv run sandbox-science-workspace serve --state-dir harness-state --static web/workspace/dist
```

Distribute the complete `dist/` directory, including `THIRD-PARTY-NOTICES.txt`, `LICENSE-Sandbox-Science.txt` and `license-manifest.json`. The build collects the package and helper origins it actually emits and ships their complete primary notices, with attribution and pinned license text for embedded components (React-derived Apache 2 helpers, the MIT is-promise adaptation, and the OFL fonts and glyph-derived SVG assets). The manifest records the notices and each distributed file's SHA-256, excluding the manifest itself. Unknown origins or assets, missing or changed notices, changed fonts, and version or license mismatches between the lockfile and the installed packages fail the build; copying a public directory is refused. The server serves these files like any other built file. See [notice inputs and update procedure](../web/workspace/licenses/README.md) and [licensing](LICENSING.md). The [isolated math-check distribution](MATH-CHECKS.md#build-and-configure) uses the same notice inputs for its code-only bundle.

The server prints a login URL once:

```
Sandbox Science workspace on http://127.0.0.1:8200/
Log in by opening this URL; it works until the server stops:
    http://127.0.0.1:8200/login?token=…
```

Open it in a browser on the same machine. The settings of `serve`:

| Setting | Meaning |
| --- | --- |
| `--state-dir` | Required. The directory the harness keeps its records in (`sandbox-science-harness … --state-dir`): project and run records, and the control store with documents, plans and reviews |
| `--static` | Required. The web app's build, `web/workspace/dist` |
| `--listen` | `host:port`, default `127.0.0.1:8200`; port 0 picks a free port |
| `--allow-remote` | Accept a listening address that is not loopback (see below) |
| `--log-level` | `DEBUG`, `INFO` (default), `WARNING` or `ERROR` |
| `--worker`, `--worker-token-file` | The worker's URL and a file with the workspace's worker token, for job views and cancellation, kernel observations and provisioning observations |
| `--model-url`, `--reviewer-model`, `--api-key-file` | The model gateway for math repairs ([math repairs](#math-repairs)) |

The server refuses an address that is not loopback unless `--allow-remote` is given. It speaks plain HTTP, so on another address the login URL and the session cookie cross the network unencrypted, and it then accepts any host name ([authentication](#authentication)). Placing the GUI on another machine is W5's work.

**The server's network reach.** It listens where `--listen` says. It can contact the model gateway at `--model-url` for math repairs, and the worker at `--worker` for job views, cancellation, kernel observations and provisioning observations. Without either setting it opens no outbound connection.

For work on the app itself, `npm run dev` serves it with Vite and passes `/api` and `/login` to a server on port 8200. Log in through the dev server's port. Logging out does not work there, since the dev server is another origin, and Vite's pages do not carry the server's headers.

The app is a directory of its own, `web/`, rather than a member under `packages/`: uv takes every directory in `packages/` for a Python project, and pyright and pytest search it, while the app's toolchain is Node's. Each browser app gets its own directory under `web/`.

## Authentication

Login works like Jupyter's, for one user on their own machine (threat model, zone Z4: the GUI is trusted by the user, the content it shows is not).

- **Login token.** At each start the server makes a random token (32 bytes, 43 URL-safe characters) and prints the login URL once, on standard output; it neither logs nor stores the token. A restart makes a new token and ends every session.
- **Login.** `GET /login?token=…` compares the token in constant time. A match sets the session cookie `sandbox_science_workspace-<port>` (32 random bytes; HttpOnly, SameSite=Strict, Path `/`, and Secure over HTTPS) and redirects to `/`, so the token leaves the address bar. Browsers send a host's cookies to every port on it, so the name carries the port, and two workspaces on one machine keep their sessions apart. The server keeps only the cookies' SHA-256 digests, in memory. A session lasts until the server stops or the user logs out (`POST /api/v0/logout`); past 64 sessions, the oldest ends.
- **The API needs the session.** Every route under `/api/` answers 401 without it.
- **Changes need the server's own origin.** A request with any method other than GET or HEAD must carry an `Origin` equal to the server's own (scheme and `Host`); a missing or `null` origin is refused. Nothing answers CORS: no route takes preflights or sets `Access-Control-*` headers. SameSite=Strict also keeps the cookie off requests started by other sites. The routes that take a POST are listed in the [API](#api-v0).
- **The server's own names.** On a loopback address, every request must name the server in `Host`: `localhost`, `127.0.0.1`, `[::1]` or the address it listens on, with its port. A page whose own host name an attacker rebinds to 127.0.0.1 (DNS rebinding) is refused, and could not use the cookie in any case, since the cookie belongs to the name the user logged in with. With `--allow-remote` this check is off, because the names that reach the server are not known.
- **User attribution.** Writes are attributed to the USER on the server; a request body cannot choose its author. Routes that need a per-login identity (plan reads and edits, math repairs) use the SHA-256 digest of the session cookie.
- **What remains.** Cookies are not bound to ports, so any other web server on the same host name receives the session cookie when the browser visits it, as with Jupyter. On the user's own machine only the user's services listen there; research code runs in sandboxes that cannot reach the host's loopback, except on the worker's local backend, which is for tests.

Re-authentication for high-risk approvals (D54) comes with approvals.

## What the browser may do

Every response carries these headers, errors and files included:

| Header | Value | Why |
| --- | --- | --- |
| `Content-Security-Policy` | `default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` | Scripts, stylesheets, fonts and fetches only from the server's own files and API. No inline or eval script, no frames, no plugins, no forms. KaTeX lays formulas out with inline `style` attributes, so those are allowed, and nothing else inline |
| `X-Content-Type-Options` | `nosniff` | A file is only what its type says |
| `Referrer-Policy` | `no-referrer` | A followed link does not tell its site where it came from |
| `X-Frame-Options` | `DENY` | No framing, for browsers without `frame-ancestors` |
| `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy` | `same-origin` | Other sites' windows and pages cannot reach the app or embed its files |

The build inlines no asset as a `data:` URL, so the fonts arrive as files the policy admits. API answers are `Cache-Control: no-store`; the app's assets, whose names carry their content's hash, are cached for a year.

**Files.** The server serves only files inside the build directory, with a type from a fixed table (HTML, JavaScript, CSS, JSON, fonts, SVG, PNG, icons, text). A path with an empty, `.` or `..` segment, a dotfile, a backslash or NUL is not found, however it is encoded; so is a symbolic link that leads outside the directory. A GET outside `/api/` for a path that is no file and whose last segment has no `.` is one of the app's routes, and gets `index.html`.

## API v0

All routes need the session and answer JSON. Every route that is a GET also answers HEAD. POST routes also need the server's own origin. Any other path under `/api/` answers 404. A test (`packages/workspace/tests/test_docs.py`) checks that this table lists exactly the routes the server registers.

| Method | Route | Answer |
| --- | --- | --- |
| GET | `/api/v0/projects` | `projects`: each project's ID, base profile, policy revision, default environment, times and number of sessions. A damaged record is listed with its `error` |
| GET | `/api/v0/projects/{project}` | `project`: the same facts, its `sessions` (each with its environment, whether it is dedicated, when it was opened and closed, and how many runs it keeps), and its `environments`, the default first, each with the sessions in it |
| GET | `/api/v0/projects/{project}/macros` | The project's current versioned macro setting ([project macros](PROJECT-MACROS.md#changing-the-setting)) |
| POST | `/api/v0/projects/{project}/macros` | Changes the setting: exact `expected` revision, bounded `macros` and `operation_id`, attributed to the USER on the server |
| GET | `/api/v0/projects/{project}/sessions/{session}` | The `session` and its `runs`, oldest first: each run's `name`, `run_id` and `started_at` |
| GET | `/api/v0/projects/{project}/sessions/{session}/runs/{run}` | The run's record ([run records](#run-records)), at most 64 MiB |
| GET | `/api/v0/projects/{project}/sessions/{session}/plan` | The session's current plan, version, progress and goal outcome; closed sessions stay readable ([contract](SESSION-PLAN-WORKSPACE.md)) |
| POST | `/api/v0/projects/{project}/sessions/{session}/plan/changes` | A USER revision of the plan's body, guarded by its base version ([contract](SESSION-PLAN-EDITING.md)) |
| GET | `/api/v0/projects/{project}/sessions/{session}/kernels` | The worker's passive observation of the session's kernels and their package revisions ([contract](WORKSPACE-KERNELS.md)) |
| GET | `/api/v0/projects/{project}/sessions/{session}/jobs` | Active jobs and the 20 most recently ended, with watcher check-ins and active waits ([background jobs](#background-jobs)) |
| POST | `/api/v0/projects/{project}/sessions/{session}/jobs/{job}/cancel` | Cancels the job through the worker |
| GET | `/api/v0/projects/{project}/sessions/{session}/reviews` | The session's consistency reviews, newest first, with an `after` cursor ([consistency reviews](#consistency-reviews-and-math-checks)) |
| POST | `/api/v0/projects/{project}/sessions/{session}/reviews` | Queues a review request (body `{}`) for the main agent's next live safe point; an idle session does not start a model turn |
| GET | `/api/v0/projects/{project}/sessions/{session}/reviews/{review}` | One review's manifest |
| GET | `/api/v0/projects/{project}/sessions/{session}/reviews/{review}/records` | The review's audit records, paged by `after` |
| GET | `/api/v0/projects/{project}/sessions/{session}/reviews/{review}/evidence/{evidence}` | A slice of one evidence item (`start`, `count` up to 16,000, optional `block`) |
| GET | `/api/v0/projects/{project}/sessions/{session}/findings` | The session's findings, with the macro context each renders with, paged by `after` |
| GET | `/api/v0/projects/{project}/sessions/{session}/findings/{finding}` | One finding with its decision history, paged by `before` |
| POST | `/api/v0/projects/{project}/sessions/{session}/findings/{finding}` | A USER decision on the finding: rebut, claim a fix, confirm, dismiss or reopen, guarded by its revision |
| GET | `/api/v0/projects/{project}/sessions/{session}/findings/{finding}/responses/{response}` | The body of one response to the finding, optionally at `body_revision` |
| GET | `/api/v0/projects/{project}/sessions/{session}/math-checks` | The project's mechanical math checks visible from this session, paged by `after` ([math checks](MATH-CHECKS.md)) |
| GET | `/api/v0/projects/{project}/sessions/{session}/math-checks/{check}` | One check's evidence, paged by `after`, optionally for one `span` |
| GET | `/api/v0/projects/{project}/documents` | `documents`: the project's living documents and its sessions' workbook documents, without the deleted ones: each one's ID, name, title, scope and session, creator, current revision and whether agent patches wait as suggestions |
| GET | `/api/v0/projects/{project}/documents/{document}` | One revision of a document, by its ID or, for one of the project's own, its name: the current one, or `?revision=N` ([documents](#document-routes)) |
| GET | `/api/v0/projects/{project}/documents/{document}/history` | Who changed what (LD-11): each revision with its author, reason and the blocks it changed, added, moved and removed; each block with the revisions that changed it; and the agents' patches with their states |
| POST | `/api/v0/projects/{project}/documents/{document}/changes` | A USER change to the document ([documents](#document-routes)) |
| GET | `/api/v0/projects/{project}/documents/{document}/patches/{patch}/preview` | A retained patch against the current text, and whether its operations can apply |
| GET | `/api/v0/projects/{project}/documents/{document}/comments` | The document's comment threads at a revision ([comments](#document-routes)) |
| POST | `/api/v0/projects/{project}/documents/{document}/comments` | Creates, replies to, edits, resolves or reopens a thread |
| GET | `/api/v0/projects/{project}/documents/{document}/comments/{thread}` | One thread's entries, paged by `before` |
| GET | `/api/v0/projects/{project}/provisioning/environments` | The project's recorded environments, from its registry only ([provisioning workspace API](PROVISIONING-WORKSPACE.md)) |
| GET | `/api/v0/projects/{project}/environments/{environment}/provisioning/observations` | The environment's package gate, queue summaries and current revision |
| GET | `/api/v0/projects/{project}/environments/{environment}/provisioning/observations/operations/{operation}` | One provisioning operation's detail, optionally at sequence `seq` |
| GET | `/api/v0/projects/{project}/environments/{environment}/provisioning/observations/operations/{operation}/files` | A page of the operation's resolved files |
| GET | `/api/v0/projects/{project}/environments/{environment}/provisioning/observations/revisions` | A page of retained package revisions |
| GET | `/api/v0/projects/{project}/environments/{environment}/provisioning/observations/revisions/{revision}/probes` | A page of a revision's probes, pinned by the revision record's hash |
| GET | `/api/v0/projects/{project}/environments/{environment}/provisioning/observations/revisions/{revision}/packages` | A page of a revision's recorded packages and declared licenses ([contract](PROVISIONING-WORKSPACE.md#recorded-packages-at-a-selected-revision)) |
| GET | `/api/v0/projects/{project}/environments/{environment}/provisioning/observations/lockfile-diff` | The differences between two pinned retained lockfiles ([contract](LOCKFILE-DIFF-WORKSPACE.md)) |
| POST | `/api/v0/math-repair` | The reviewer model's repair of one formula ([math repairs](#math-repairs)) |
| POST | `/api/v0/logout` | Ends the session: 204, no body |

Outside `/api/`, `GET /login` takes the login token, and every other GET is a [file or an app route](#what-the-browser-may-do).

### Document routes

A document revision's answer holds its `document`, its `revision` (author, the user who accepted it, reason, base and parent, warnings, what was normalized, the parsers' versions), its `text`, its front matter's `title`, effective `macros` and `macro_context` (setting revision and hash, anchors, known or held state, warnings and identity), and its `blocks`: each one's `id`, `kind`, `parent`, `depth`, `lines`, `status`, structural kind and attributes, its editable Markdown `source` without its containing block's indentation, and its formulas' `id`, `kind`, `status` and `tex`. `matched` is false when the server could not parse the revision as its block map was made, and the formulas then come without their TeX.

Document writes use `POST …/documents/{document}/changes`, with a strict action (`source`, `patch`, `revert`, `promote`, or `apply_patch`), `base`, `reason` and `operation_id`. The action carries respectively `text`, `operations`, `target`, `name`, or `patch_id`; unknown fields are refused. Results include `outcome`, revision, retained patch ID and structured conflicts. `GET …/patches/{patch}/preview` returns the retained operations, the exact current `base`, current and proposed source, and whether the named operations can safely apply.

Comments use `GET …/comments?revision=N&after=THREAD`, returning `threads` and `next_after` (10 threads or 1 MiB at most). `GET …/comments/{thread}?before=ENTRY_NUMBER` reads up to 50 entries or 256 KiB of projected entries and an `entries_before` cursor, with the anchor projected onto the current document revision. `POST …/comments` takes a strict `create`, `reply`, `edit`, `edit_body`, `resolve` or `reopen` action and an `operation_id`. Create carries `anchor: {revision, block, math? | start?/end?}`, `body` and optional `addressed_to`; reply and edit carry `thread`, `expected`, `body` and an optional target session; `edit_body` carries the original `entry`, the current body's `base` and `sha256`, `thread`, `expected`, the new `body` and `operation_id`, and appends history without changing the original entry or a work request already pinned to it; resolve and reopen carry `thread` and `expected`. Completing a `request_id` is reserved for the addressed session's main agent; a user cannot claim that completion through this route. User attribution and project and session ownership are derived or checked on the server, and unknown fields are refused. Bodies are bounded to 8,192 characters or 16 KiB of UTF-8 within the ordinary 64 KiB request limit; anchors keep immutable context derived on the server. Lists of changed blocks carry explicit totals, truncation and revision links.

### Errors and records

Errors are `{"error": {"kind", "message"}}` (document errors also carry `details`) with the worker API's kinds: 400 `invalid_request` for a malformed ID, run name, document name, revision number or request body, 401 `policy_denied` without a session, 404 `not_found`, 409 for a stale guard (a document, comment, plan or finding revision, or a provisioning pin), 413 for a request body over 64 KiB (document changes allow 8 MiB of escaped JSON, while document text stays bounded to 1 MiB of UTF-8; plan changes allow 128 KiB), 422 `unsupported` for a record over the size limit or an observation without a configured worker, 429 `unavailable` for a session that has asked for as many model repairs as it may for now, 503 `unavailable` when the worker cannot be reached or answers badly, a request times out, or a project or run record is damaged, and 500 `internal` for an unexpected failure. One mapping in `errors.py` gives every route these statuses. A 503 or 500 carries a fixed message that names neither the worker's address nor a record's path; the worker's own refusals (400, 403, 404, 409 and 422) keep their status and message. JSON that a record cannot carry, a NaN from a tool's output say, is sent as a string.

The server reads the harness's records through the orchestrator's `ProjectStore`, and the documents, plans and reviews through its `DocumentStore`, in a thread. Reads use a read-only connection; explicit user changes open a separate writable connection lazily. Reading the harness's state directory directly is this stage's arrangement: the workspace runs beside the harness and imports the orchestrator's stores (the one deployable member importing another, in the [conventions](../.agents/CONVENTIONS.md)). W5 gives the control application an API of its own for the GUI to use.

## Views

**The client's requests.** Every request goes through one function (`src/lib/http.ts`): to this origin under `/api/v0`, without the cache or redirects, with an optional deadline and byte limit, failing with an `ApiError` that carries the server's error kind and message. The types of the typed answers (kernels, plans and plan edits, provisioning) are generated from the server's response models into `src/lib/api-types.ts` by `scripts/generate_api_types.py`; a workspace test fails when the file is stale. Answers from this server are trusted as the server's own: the client checks only that a polled answer belongs to the project, session or environment the view shows. The panels that refresh (plans, kernels, jobs, provisioning and the session's runs) share one poller (`src/lib/poller.ts`): it reads only while the page is visible, never overlaps its own reads, gives up on a read after eight seconds, waits three seconds between reads and backs off to thirty after failures, and stops for good on a 401, and, for a panel that reads its own scope's record, on a 404 or a 409. The jobs panel retries a 404 or 409, since the jobs route passes the worker's refusals through. A read that hiding the page interrupted is read again when the page is shown. A 401 from any request, or the Log out button, ends the page's session (`src/lib/session.ts`): every panel stops and no request starts until the page is reloaded after a new login.

Hash routes, without a router library: `#/`, `#/projects/<project>`, `#/projects/<project>/sessions/<session>`, `#/projects/<project>/documents/<document>` with `/revisions/<number>` for an earlier revision, and `#/projects/<project>/documents/<document>/history`.

- **Projects:** each project's base profile, policy revision, number of sessions and last change.
- **Project:** its facts; its macro setting, with an editor; its sessions, each with its environment (the project's default or its own), number of runs, and whether it is open; its documents, the project's and its sessions' workbooks'; and the Environments panel ([provisioning](#provisioning-observations)).
- **Session:** its environment and state, the plan panel, the kernel panel, background jobs, consistency reviews and math checks, and its runs, oldest first. The newest is open; the others are fetched when opened. The session's facts and its list of runs refresh while the page is visible, so a run that ends and a session that closes show up.
- **Run:** when it ran and for how long, its environment and policy revision, and whether it answered. Then the system prompt (folded), the task, and the agent's messages, each with its tool calls: the arguments (code as a block), and the result the model read, laid out: state and exit code, stdout, stderr, outputs, the error and its traceback, platform notices, and where the call ran, with the isolation tier in effect. Anything else in the result shows as JSON, and the call's full record opens on request. Successful runs show their final answer. Failed runs show the partial transcript and the fixed failure phase, without an Answer heading.
- **Document:** a revision, the current one unless the address names another: its title, name and scope, its number with links to the revisions before and after it and to the history, its author (with the user who accepted it when it was a suggestion), when it was made and why, whether its delimiters were saved in the dollar form, and its warnings (LD-30). Then where the browser's parse and the server's block map disagree, if they do, and the text, rendered with the server's IDs ([living documents](#living-documents)), with the editor and the comments. A revision that deleted the document says so.
- **History:** the revisions, newest first, each with its author, reason and the blocks it changed; the revisions that changed each block, folded; and the agents' patches with their states, and the revisions they made.

## Living documents

The project's living documents and its sessions' workbooks show each revision and who changed what. The current revision offers source and block editing, and workbook documents can move to project scope. Earlier live revisions offer an explicit revert. History previews retained patches against the current text and lets the user apply them with that exact revision as the guard. Documents live in the harness's control store; see [living documents as built](LIVING-DOCUMENTS.md) and [the reconciliation rules](LIVING-DOCUMENTS.md#changes).

Drafts keep their base revision in memory across views and in this browser tab's session storage across reloads. Conflicts show the newer text and author while keeping the draft. Resolving a whole-source conflict starts with the current text and keeps the old draft separately; block resolution touches only the named block. A browser without storage keeps the in-memory draft and warns before closing. Inputs are disabled while a save is pending.

**Comments and addressed work requests.** Each document revision offers a block, formula or quoted-range comment composer, its original quote and context and its attachment status (`current`, `moved`, `stale`, `orphaned`). Threads show attributed append-only history, requests' queued, delivered and responded states, revision links and changes outside the anchor. Replies, opening-comment edits and resolve and reopen actions guard the expected thread revision. Drafts survive navigation and reload independently of source edits; retry IDs for an exact payload survive uncertain responses. Large thread histories and thread lists are paged.

Choose a recorded session's main agent to queue a work request. Workbook documents default to their own session; project documents have no default. An active main run receives it at a model safe point; an idle session keeps it until its next manually started main run. There is no automatic idle wake or run scheduler on the server. The full comment body is the instruction, and the anchor, quoted document and other comments grant no authority. Delivery is acknowledged only after the run transcript is durable; a crash between saving the transcript and acknowledging can replay the stable request ID. Agent replies do not resolve user threads. See [the annotation and delivery rules](LIVING-DOCUMENTS.md#comments-and-work-requests).

**Comment body math.** [Comment and reply math](COMMENT-MATH.md) shares the bounded isolated checker and closing-only notices with document math. Original bodies and every correcting actor stay immutable; project-wide checks identify the original comment entry, body revision, pinned parent document macro revision and exact check context. The comments route accepts a body correction (`edit_body`) only for USER-owned source, guarded by thread, body and hash. Body drafts and retry IDs survive conflicts independently of reply drafts; adopting current guards keeps unsaved text. Legacy identities that are bound to an older parser or unknown stay visible as held checks while ordinary edits stay usable.

**One parse on both sides** (LD-5, LD-9). A document is parsed by the text renderer's parser with two rules more, as the server parses it: YAML front matter at the very start of the text, and theorem-like containers, `::: lemma (label)` to `:::` (`src/lib/documentrules.ts`, ports of mdit-py-plugins' `front_matter` and `container`, MIT, credited in the file). The rules are registered in the server's order, and each block rule records where the blocks it makes lie and where their own text begins on each line (`src/lib/blockmap.ts`), from which the browser derives its block map as the server does: each block's kind, depth and lines, without trailing blank lines, and each formula's block, kind and TeX. The shared cases in `src/lib/documents.fixtures.json` pin both, and run through both parsers in CI, with the dollar and delimiter cases run through the document parser too.

**The server's IDs** (`src/lib/attach.ts`). The revision's block map comes from the server with each block's ID, kind, depth, lines and status, and each formula's ID, kind and TeX. A block of the browser's parse takes the ID of the server's block of the same kind, depth and lines, in order, and its formulas take the server's span IDs when both read the same kinds and TeX. Each block's element carries its ID as `data-block`, shown at its top right while the pointer is over it, and each formula's carries `data-math`. Where the parsers disagree, the view says where and why, and that part keeps no ID and is outlined: it never guesses which block is which. Revisions parsed with table profile 1 keep their original maps and can show the Unicode table trim mismatch that profile 2 corrects ([table profiles](TABLE-PARSER-PARITY.md)). A revision whose block map a parser of another version made comes without its formulas' TeX; its blocks still take their IDs, and its formulas theirs in order.

**What a revision changed.** A block the revision changed, added or moved is marked at its left, in the colors of its status; a first revision marks none. A displayed equation in a quote is rendered without the quote's markers, which its TeX keeps as the plugin reads it.

**Macros** (LD-28; `src/lib/macros.ts`). A document's math renders with the server's effective context: bounded project definitions, overridden by its front matter. KaTeX expands a macro wherever a formula uses it, and a few short macros that use one another can expand into a great many characters: three of them, written inside a formula, once froze a page for 39 s. So, on both sides:

- the effective context has at most 200 macros, each a control sequence defined in at most 1,000 characters;
- a macro whose definition defines another, as `\def` does, or uses one of its arguments more than once, which would let it multiply what it is given, is left out;
- the server also leaves out a macro that expands into itself, or to more than 2,000 characters with the macros it uses expanded, and warns of each on the revision;
- and as KaTeX renders a formula, each expansion of one of the document's macros counts its definition's characters: a formula whose macros expand to more than 2,000 characters in all is shown as written, with why.

Render and repair caches carry project scope, effective context identity and definitions, so one project or context cannot reuse another's result. The project view offers a USER editor for the JSON setting; exact revision guards and persisted drafts and retry IDs keep concurrent edits. The same setting is available through the [USER CLI and scoped API](PROJECT-MACROS.md#changing-the-setting). Settings admit at most 16 KiB of canonical definitions; unsafe settings are refused, while unsafe document front matter keeps its source and a warning. A formula that defines a macro is still shown as written ([math repairs](#math-repairs)).

An unnumbered current document uses the current project setting. An explicitly numbered revision uses the setting pinned when it was saved; older history without a pin stays unknown. Comments use the selected document's effective context; thread history is diagnostic rather than a claim about each body's rendering when it was saved. Anchored findings resolve a unique agreed context from their document anchors; contradictory or unproved contexts are visibly held, with project-only rendering labelled diagnostic. See [context and lifecycle boundaries](PROJECT-MACROS.md#exact-contexts-and-uncertainty).

**Limits.** A document holds up to 1 MiB of text and 20,000 formulas, as the server keeps them; its math renders in goes as a text's does ([showing untrusted text](#showing-untrusted-text)), so a long document takes more goes, not longer ones. The same renderer shows it, with the same rules for links, images, raw HTML and math.

## Session plans

The session view shows the session's plan in a panel that polls the [read-only plan route](SESSION-PLAN-WORKSPACE.md): goal, feasibility, expected outputs, ordered phases, stable step IDs, tracks, current completion, in-progress, blocked and skipped counts, and attributed update times. A changed step intent does not inherit old completion: the previous assertion is labelled separately. A completed workflow keeps its reported achieved, partially achieved or not achieved goal outcome and remaining work. Open visible sessions refresh every three seconds with bounded backoff; hidden or unmounted views stop polling and ignore late responses; a closed session's plan is read once. Recorded progress is not process liveness or scientific verification.

For an open session's plan, the panel offers a structured [text editor](SESSION-PLAN-EDITOR.md) for its wording, which saves through the [plan changes route](SESSION-PLAN-EDITING.md). It keeps stable IDs and order, and does not create, reorder, approve or run anything.

## Session kernels

With a worker connection configured, an open session page shows a [passive kernel panel](WORKSPACE-KERNELS.md). It reads only its recorded scope and shows exact start and current package revisions, generation and the worker's receipt time. Current, predates, unknown, empty and unavailable states stay distinct; a previous observation stays dated when the current state is unknown. Reads pause while the page is hidden and stop on view replacement and logout. The panel has no restart action.

## Background jobs

With `--worker URL --worker-token-file FILE`, session pages show all active jobs and the 20 most recently ended, with state, elapsed time, CPU and memory when available, output head and latest tail, watcher progress notes and failures. The browser asks only the workspace server, through the shared poller: every three seconds while the page is visible, backing off after failures. Keys never enter the browser; the server uses the recorded session's environment and checks job ownership before showing or cancelling a job. The Cancel job button sends a POST under the same authentication and origin guard as other writes. Output and watcher text are escaped plain text.

Active waits show what the agent waits on, for how long, and the watcher alias, or say that check-ins are off when no watcher was configured. An expired harness heartbeat is shown as disconnected. Job completion and attention notes stay available after a wait ends. With the worker's sandbox daemon (the default on the OpenShell backend), running jobs survive a worker restart. See [background jobs as built](BACKGROUND-JOBS.md) for the harness's job tools, notices, watcher budgets and limits.

```bash
uv run sandbox-science-workspace serve --state-dir harness-state --static web/workspace/dist \
    --worker http://127.0.0.1:8100 --worker-token-file worker.key
```

Without a worker connection the jobs panel says that live job views are unavailable. When the worker cannot be reached or answers badly, the job routes answer 503 `unavailable` with a fixed message that does not name the worker's address; the worker's own refusals keep their status (400, 403, 404, 409 or 422). Project, session, run and document views keep working.

## Provisioning observations

The project's Environments panel shows the recorded provisioning state of the project's shared environment and its retained dedicated environments, read through the worker ([panel](PROVISIONING-WORKSPACE-VIEW.md), [API](PROVISIONING-WORKSPACE.md)): the package gate, pending operations, an operation's request and resolved files, retained package revisions and their probes, a selected revision's recorded packages and declared licenses, and a comparison of two retained lockfiles ([comparison view](LOCKFILE-DIFFERENCES-VIEW.md)). Pages hold at most eight entries and are followed explicitly. The panel does not observe VM state, and it has no install, approve, deny, cancel, rebuild or policy controls. Source URLs and other recorded text show as escaped text.

## Consistency reviews and math checks

Each session has a consistency-review panel, backed by the shared control store. It shows recent outcomes; pending, running, failed, capped, inconclusive and unreviewed states; role usage and configuration identity; exact evidence slices, audit records and findings; and attributed decision history. Older pages are reached through explicit cursors. A pass is limited to the recorded checks and is not a proof. Refresh fetches the current state; saved runs show their review status as it was when they were saved.

Users can rebut, claim a fix with exact `doc-ID:revision` references, confirm, dismiss or reopen a finding. Drafts and the operation IDs of uncertain requests survive reload; a response to a stale finding keeps its text until the user chooses the current revision. Writes need the authenticated same-origin boundary, and the server derives the USER identity. Request review queues a request for the next live safe point; idle sessions do not start a model turn. See [consistency review as built](CONSISTENCY-REVIEW.md) for snapshot budgets and the retry and delivery rules.

The panel also lists the project's mechanical math checks ([document](MATH-CHECKS.md), [comment and reply](COMMENT-MATH.md) and [MAIN explanation](FINDING-PROSE-MATH.md) checks) for an authenticated project and session, so USER checks and checks without a session stay visible. Findings stay scoped to the session that authored the checked text. These checks have the `math` origin and no reviewer run or model; their anchors open bounded, escaped evidence with the exact document, block and span. Queued, same-head retry, unavailable and quota-held states are shown. USER decisions on math findings use the same revision guard, retry IDs and kept drafts. Saved run summaries show math checks pending and their count; the panel does not update by push.

## Run records

With `--state-dir`, the harness keeps every run that reaches the tool loop, including ordinary failures. It keeps the same record `--transcript` writes, beside the projects' records:

```text
<state-dir>/projects/<project>.json
<state-dir>/runs/<project>/<session>/<started>-<run-id>.json
```

Runs have a tree of their own because project IDs may contain dots: a project's runs kept beside its record could collide with another project's record or lock file. `<started>` is the start time in UTC, such as `20261011T130102345678Z`, so names sort in order of starting, and a run ID used again adds a record rather than replacing one. With `--state-dir`, a `--run-id` must therefore be an identifier. Each record is written once: to a temporary file, synced, then linked to its name, which fails rather than replace another record, so a reader never sees half of one. A record holds:

| Field | Contents |
| --- | --- |
| `run_id` | The run's ID, which starts the operation IDs of its tool calls |
| `context` | Project, session, environment and policy revision |
| `task` | The task as given |
| `started_at`, `ended_at` | When the run started and ended, in UTC |
| `stopped` | `answered`, `turn_limit`, `user_message`, or `error` |
| `failure` | Optional fixed phase: `model`, `tool`, `harness` or `cleanup`; older records omit it |
| `final_text` | The answer, or null for a failed run |
| `delivered` | For each notice source (`requests`, `jobs`, `reviews`, `math`), the IDs a successful model call saw; acknowledged after the run is saved. Older records' `job_notice_ids`, `work_request_ids`, `review_notice_ids` and `math_notice_ids` read as these |
| `summaries` | What a source reported at the run's end: the consistency review (`reviews`) and the document math checks (`math`). The record also shows them as `review_summary` and `math_summary` |
| `messages` | The conversation in the OpenAI chat format the harness uses, system prompt first |
| `tools` | One record per tool call, in order: its name, its arguments as the model wrote them, and its outcome (the worker's execution record, or an artifact, an error kind, …) |

Model, tool-loop, harness and cleanup exceptions keep the messages and completed tool outcomes observed so far. A failed record has `stopped="error"` and no final answer; the GUI labels it a failed run and shows its partial transcript. The command still exits 1. The failure field holds only a fixed phase, never exception text, an upstream error body or a traceback. Checked with the CLI and a browser ([report](../.agents/docs/agent-reports/failed-run-records-2026-10-12.md)).

Only notices delivered through a successful model call may be acknowledged, and only after the run is durably saved. A failed record cannot finalize staged ignored-math outcomes. Scientific decisions already completed stay recorded. A refusal during setup or isolation before the loop, cancellation, process death and an unwritable state directory leave no failed record. Cancellation propagates through later cleanup failures without saving a run or acknowledging notices.

The harness writes `--transcript` first. A record it cannot keep, such as one whose tool output is nested deeper than its schema follows, is reported, and the command exits 1.

## Showing untrusted text

Everything the views show was written by an agent, a user, a paper or a program, so it is untrusted (threat T6). One renderer shows all text, the `Markdown` component (`src/lib/markdown.ts`), and the rest of the app shows text only as text.

- **Markdown** is markdown-it's default dialect (CommonMark, tables and strikethrough) with raw HTML off: tags in the text show as written.
- **Links** are kept only for absolute `http` and `https` addresses and `mailto`. Any other link, `javascript:` or relative, stays text. Links open in a new tab, without a referrer and without access to the app (`noopener noreferrer nofollow`). Bare addresses are not turned into links.
- **Images** become links and are never loaded, so text cannot make the browser fetch an address.
- **Math** is written with dollars, or with LaTeX's own delimiters: `\(…\)`, `\[…\]`, and display environments such as `\begin{align}…\end{align}` written without delimiters ([where math begins and ends](#where-math-begins-and-ends)). It goes to KaTeX in its untrusted mode (`trust: false`: `\href`, `\url`, `\includegraphics` and `\html…` show in red as written, and do nothing), with positive sizes capped at 20 em and at most 1,000 macro expansions. A formula that defines a macro (`\def`, `\gdef`, `\let`, `\newcommand` and the like) is shown as written: macros come from the bounded project setting with document front matter overriding it ([project macros](PROJECT-MACROS.md), LD-28), and a definition can multiply KaTeX's work far beyond the formula's length within that limit (a formula of 480 characters took KaTeX 35 s). Commands are read token by token with KaTeX's own token pattern, so a `\verb|…|` body or a `%` comment defines nothing. A comment starts only at a token that is `%` alone, as in KaTeX: `%` with a combining mark is another token, which a macro such as `\@secondoftwo` can skip unparsed. Where KaTeX's lexer would stop with an error, the reading goes on, finding more rather than less. KaTeX reads a `%` in the link of `\href`, `\url` or `\includegraphics` as a character, and which `%` that is depends on how it parses the formula, so in a formula with one of them and a `%`, every name after a backslash counts. LD-28 has the GUI apply such a definition to its own span, with a warning; this differs ([below](#differences-from-the-specification)). A formula KaTeX cannot parse is repaired if it can be, and otherwise shown as a plain-text approximation; either way it is marked, and its LaTeX as written is behind the mark ([math repairs](#math-repairs)). Rendering never changes the text. KaTeX's MathML remains available to assistive technology.
- **Copying math.** Selecting and copying a formula puts its original LaTeX, with the parser's original delimiters, on the plain-text clipboard (LD-44), including when the view shows a repair or Unicode approximation. A partial formula selection copies the whole expression without moving the visible selection. Mixed selections keep surrounding text and block boundaries; display-only repair marks and diagnostics are left out of the formula's plain text. HTML copying keeps the rendered view. Editor, code-only and non-math copying keep their ordinary behavior.
- **Nothing a text draws leaves its own box.** KaTeX caps only positive sizes, so `\smash` and a negative `\raisebox` could draw an agent's text over the tool call below it; the stylesheet clips each rendered text to its box (`contain: paint`).
- **Limits.** Text over 200,000 characters is shown as written. One text sends KaTeX at most 1,000 formulas and 20,000 characters of math, and no formula over 5,000 characters, since KaTeX's output is about 150 times its input; math beyond that is shown as written. The repairs of one text's formulas send KaTeX at most 20,000 characters more; beyond that, its formulas are approximated without them. Texts render in goes of 250 ms, each a task of the page's thread, with the Markdown, KaTeX and the page's update all counted, as when a page first shows all its texts. Within a go, texts render one after another. Once its time is spent, no further text starts: one that comes later shows as it was last rendered, or as written if it never was, and waits for its turn in a later go. KaTeX stops too, between formulas, and the math it had no time for shows as written, marked *not rendered yet*, until a later go renders the text again, starting where it stopped. The first text and the first formula of a go always render, so each go gets further, and a go can run over by one text's Markdown and one formula. So however much text and math a page has, its first view holds the page's thread about a quarter of a second, and the browser draws and takes input between goes. A formula whose repairs had no time waits for a later go too, and the model is not asked for it before the rules have been tried. Without definitions, the slowest formula of 5,000 characters found takes KaTeX about 60 to 80 ms (alternating `\Huge` and `\tiny`). KaTeX renders on the page's own thread, so one formula cannot be interrupted. The page keeps KaTeX's result for each formula, and the repairs' candidates and approximations, each within a count and a size, so a text rendered again sends KaTeX only what is new. The macros in force are part of what these results, and the model repairs, are kept by (LD-28).
- **Tool calls** show their arguments and results as plain text. A result's rich outputs (HTML, SVG, images) are not rendered.
- **Code**, in tool calls and in Markdown, shows bidirectional control characters as their code points, such as `⟦U+202E⟧`, so that code cannot read differently from what ran.

D54 puts previews of untrusted artifacts (HTML, SVG, notebook output, PDFs, and agent-written Markdown) on a separate origin, in sandboxed frames. The workspace shows only text, in the app's own origin, under the rules above and the policy that admits no inline script. Rich artifacts wait for the separate origin.

### Where math begins and ends

Comments and anchors address math spans, so the browser and the server must agree where each formula begins and ends (D65). The browser's rules (`src/lib/dollarmath.ts`) are a port of the `dollarmath` plugin of mdit-py-plugins 0.6.1, which the server uses with markdown-it-py ([living documents as built](LIVING-DOCUMENTS.md#parsing)), with these settings on both sides, and one rule of this project's own:

| Setting | Value | Effect |
| --- | --- | --- |
| `allow_space` | false | `$ x$` and `$x $` are not math, so amounts such as "$5 and $10" stay text |
| `allow_digits` | true | A digit may touch a dollar sign. The plugin's check for the other setting has no exact JavaScript twin |
| `double_inline` | true | `$$…$$` inside a paragraph is displayed math, as agents often write it |
| `allow_blank_lines` | false | A blank line ends a `$$` block, so a stray `$$` cannot swallow the rest of a text |
| `allow_labels` | true | `$$…$$ (eq:1)` labels an equation |
| A `$$` block ends within 200 lines of its opening line (this project's own) | | The plugin looks to the end of the text from every `$$` that does not close, which makes some texts take quadratic time; past the limit, `$$` is ordinary text, so such math is displayed within its paragraph |

The dialect is markdown-it's default (markdown-it-py: `js-default`) with HTML off. The cases in `src/lib/dollarmath.fixtures.json` pin the spans, and the server's parser passes every one, the line limit applied to the plugin's block rule ([living documents as built](LIVING-DOCUMENTS.md#parsing)). Table parsing has two profiles: profile 2 matches the browser's row and cell trim, and a seeded whole-document comparison checks complete blocks, math and source offsets and attached IDs; profile 1 keeps its original reading for older revisions ([table profiles](TABLE-PARSER-PARITY.md)). The port keeps the plugin's quirks, which the cases record: a `$$` block in a block quote keeps the quote markers in its content (a document's view renders it without them), a block can end in the next list item, the text is the plugin's (stripped of all Unicode whitespace, except an image's description, which neither side strips), and a backslash that ends a text escapes a dollar sign that starts it.

**LaTeX's own delimiters** (`src/lib/mathdelimiters.ts`). Agents often write `\(…\)`, `\[…\]` and bare display environments (`equation`, `align`, `alignat`, `gather`, `multline`, `flalign` and `eqnarray`, starred or not, and `displaymath`). markdown-it reads them as rules of its own, which give the same tokens as dollar math and follow its rules: a delimiter after an odd number of backslashes is escaped, counted as for dollars (so a backslash that ends a text escapes a delimiter that starts it, the plugin's quirk); inline, a formula ends at the first closing delimiter in its paragraph, and one of only whitespace, Python's as for dollars, is not math; a block begins a line and its closing delimiter ends one, within 200 lines and with no blank line between. Code blocks are read as code wherever they are, indented or fenced, in lists and quotes too, so nothing in them becomes math. Code spans are read as dollar math reads them: a span that starts before a formula keeps it, but a formula that starts before a span runs over its opening backtick (`\(a `b \) c` d` makes "a `b " math). The rules differ from `$$` in four ways: a `\[` or `\begin{…}` line may begin a block right after a line of a paragraph, as a code fence may, since it can only be math; a block stops, as a fence does, at a line outside its list item or quote, where `$$` reads on into the next item or a lazy line; a block whose closing delimiter has more text after it on its line is read within its paragraph, where `$$` would look further for a line that ends with `$$`; and a block's content leaves out the markers of the quotes it is in. An environment is its own delimiter, so KaTeX gets the whole environment, except `displaymath`, whose body it gets. Adding these rules changes no dollar case. The cases in `src/lib/mathdelimiters.fixtures.json` pin the spans; the server's parser passes them too. The rules come from the user's own `cscience-crypto`, which converts these delimiters to dollars before parsing; reading them while parsing keeps code as written in indented code and in fences inside lists and quotes, which a conversion before parsing rewrote, keeps a displayed formula in its list item, and pairs no delimiter with a stray dollar sign.

**Escaped brackets.** Markdown writers escape brackets that are not links (`See \[1\]`, `\[sic\]`, `- \[x\] done`, and Pandoc's `\[@smith2020\]`, `\[^1\]` and `\[path\_to\_dir\]`), which is also LaTeX's displayed math. Within a paragraph, `\[…\]` is displayed math in two cases:
- its content is padded with a space, a tab or a line feed just inside both brackets (`\[ a - b \]`), as TeX writers pad and Markdown writers' escapes do not;
- unpadded, it holds what math uses and bracketed prose seldom does: a command (a backslash and a letter), a brace, one of `= + < >`, or `^` or `_` after its first character (`^1` alone is a footnote's reference).

A backslash before ASCII punctuation is a Markdown escape, and it and the character it escapes count as neither. Anything else stays text, as the escapes make it.

So "See \[1, 2\]", "we \[...\] agree", "pages \[3-5\]", "version \[1.2\]", `\[path\_to\_dir\]`, `\[\*\]`, `\[\#1\]` and Pandoc's citations and footnotes stay as written. "so \[x^2\] holds", `\[\alpha\]`, `\[a = b\]`, and every padded formula, such as `\[ f(x) \]`, `\[ |x| \]`, `\[ n! \]` or `\[ -1 \]`, display. A line that is only `\[n\]` is a displayed formula, since the rule is for brackets within a paragraph.

The trade-off goes both ways:
- unpadded displayed math within a sentence with nothing of the rule, such as `\[n\]`, `\[2\]`, `\[f(x)\]` or `\[a - b\]`, shows as bracketed text, as `\[1\]` and `\[x\]` must;
- escaped brackets around prose with one of `= + < >` or an underscore inside a word, such as `\[debug=true\]`, `\[C++\]`, `\[<tag>\]` or `\[my_var\]`, show as math.

The content is read character by character, a line break as any other, since Pandoc wraps lines inside brackets too. The shared cases pin all of these. The server's port reads the content the same way, character by character, with no pattern.

## Math repairs

When KaTeX rejects a formula, the renderer tries three things in turn (`src/lib/mathrepair.ts`), and shows what it got with a mark that says which:

1. **Rules** (*auto-repaired*). Deterministic repairs, chosen by KaTeX's error: commands KaTeX lacks (`\mbox`, `\emph`, `\abs`, `\norm`, `\eqref`, `\R`, `\eps`, `tabular`, `eqnarray`, …), labels, references and spacing commands dropped, `\text{…}` arguments escaped, braces and `\left`/`\right` balanced, bare `%`, `#` and `&` escaped, double scripts nested (`x_i_j` as `x_{i_j}`), dangling `_` and `^` dropped, `&` and `\\` outside an environment wrapped in `aligned` or `gathered`, display-only environments made inline ones in inline math, and an unknown command shown as an operator's name (`\Tr` as `\operatorname{Tr}`). The first candidate KaTeX renders is shown. A rule changes a command only where its backslash starts one (in `\\R`, a line break and an R, nothing changes), a removal never joins a command to the letters after it (`\d\label{x}ef` becomes `\d ef`, not `\def`), and a candidate that brings in a definition, or a command KaTeX gates by trust that the formula did not have, is dropped.
2. **The reviewer model** (*model-repaired*), when the server has one. The page asks the server once for each formula, in the background, and keeps up to 2,000 answers until it reloads (the server keeps them too, so asking again for one the page dropped costs no model call): one request at a time until the server says it has a model, then two. When the server says it has none, or the session ends or reaches its limit, the requests still waiting are dropped at once, and so is any answered `busy` after that. An answer of `busy` is asked again after 2, 4 and 8 s, then given up for the page. Meanwhile the formula is approximated, and its mark is dashed. When answers come, KaTeX checks them, and the texts that show those formulas render again, at most once a frame (once a second in a hidden tab). An answer that brings in a definition, or a command KaTeX gates by trust, is not shown.
3. **An approximation** (*approximated*). The formula in Unicode, as nearly as plain text allows: Greek letters and operators, sub- and superscript digits and letters, blackboard bold, fractions as `a/b`, matrices and cases flattened, and unknown commands by their names.

Pressing a mark shows why (KaTeX's error, and who repaired it or why nothing did) and the formula as written, with its delimiters, to read and copy; bidirectional controls in it, and in KaTeX's error, which quotes it, show as their code points, as in code, in the mark's title too. The text never changes: repairs are a view, as the rendering is. The marks are buttons, and the app's own script opens them, so the page needs no inline script. A mark and what it shows sit above anything a formula draws, so `\rlap{\colorbox{white}{…}}` cannot hide them. A repair is rendered within the text's math limits ([showing untrusted text](#showing-untrusted-text)), and a formula over 5,000 characters is not repaired.

Copying uses the renderer's original formula body before trimming, repair or macro expansion, with its parser delimiters. In living-document display math, the markers of quotes and lists are left out by the same source extraction used for rendering. Bidirectional controls stay visible as code points in the diagnostic panel; the copy-only escaped metadata keeps their original characters. No source revision is changed. The one copy handler of the app runs only for a single nonempty selection inside the app that contains formula metadata, uses a cloned range, and needs no asynchronous clipboard permission, route or content-policy exception. Table copying keeps the selected DOM's outer newlines and tab-separated cells; code-only copying keeps the browser's own newline handling. Checked with native copy and paste in a browser ([report](../.agents/docs/agent-reports/workspace-copy-latex-2026-10-12.md)).

**The endpoint.** `POST /api/v0/math-repair` takes `{"tex", "display", "error"}`: one formula without its delimiters, at most 4,000 characters, whether it is displayed, and KaTeX's error, cut to 500 characters. Optional `project` and `macro_context` identities must appear together; the project is validated and both identities are bounded. They isolate the repair cache, grant no authority to read sources and do not go to the model. Requests without them are still accepted. Like every API route it needs the session, and like every POST the server's own origin. It answers `{"enabled": false, "tex": null, "reason"}` when the server has no reviewer model, and the page then asks no more; otherwise `{"enabled": true, "model", "tex", "reason", "cached"}`, where `tex` is the repair or null and `reason` says why there is none (`no usable repair`, `busy`, a gateway failure). The answer may include an optional [producing-input descriptor](DISPLAY-REPAIR-PROVENANCE.md): the fixed system message, empty tools and formatter that produced the first answer, carried through cache reuse. It holds no formula or session identity and is not a durable record of model calls; the browser may ignore it.

**The model.** Requests go to the reviewer model the settings name, through the model gateway, and to no other model: never a session's or a subagent's, whose output is what is being repaired. Settings, as for the harness's `run`:

| Setting | Meaning |
| --- | --- |
| `--model-url` | The model gateway's OpenAI-compatible base URL (the LiteLLM proxy, D61), as the gateway answers it: no redirect is followed |
| `--reviewer-model` | The reviewer model's alias at the gateway; `off`, `none`, `false`, `0` or `disabled` turn model repairs off |
| `--api-key-file` | A file holding the workspace's key for the gateway |

Without the three, or with `--reviewer-model off`, model repairs are off and the server never connects to the gateway. The key is a secret: the server sends it only in the gateway's `Authorization` header, and never returns it. Its log lines about failed repairs hold only the gateway's status or the kind of failure, never the gateway's text, which can quote the key; any other line that would hold the key holds `[the gateway key]` instead. The key file must hold one line of visible ASCII characters, or the server does not start, and it warns when others can read the file. Give the workspace a LiteLLM virtual key of its own, with its own spending limit. The server follows no redirect from the gateway, which would carry the formula elsewhere, takes any answer but a 2xx as an error, and reads at most 1 MiB of an answer. These are rules of the orchestrator's model client, so they hold for the harness's own calls too: a `--model-url` that the gateway redirects, such as an `http://` address it sends on to `https://`, fails both the workspace's repairs and `run`, which then needs the address the redirect names.

**Limits** (`packages/workspace`, `mathrepair.py`):

| Limit | Value |
| --- | --- |
| A formula sent | 4,000 characters, with KaTeX's error cut to 500 |
| An answer | 512 tokens, within 25 s, and 1 MiB from the gateway |
| Requests to the model at once | 2, with 8 more waiting; beyond that the answer is `busy`, which the page asks again later |
| Answers kept | 512, the least recently used dropped first; a repair or a refusal until the server stops, a failure (a timeout, the gateway's error, an answer it cannot read, anything else) for an hour, as the user settled for living documents (LD-46) |
| Requests to the model for one browser session | 60 in any hour; beyond that the endpoint answers 429, and the page asks no more |

A formula the server has answered costs nothing more, whichever session asks. The model's answer is cleaned of code fences, with an info string such as `latex` (on a fence of one line, only a known one: `latex`, `tex`, `math` or `katex`), and of delimiters, and refused if it is empty, the formula unchanged, more than three times as long plus 200 characters, or `UNFIXABLE`, the model's word for a formula it cannot repair without changing its meaning. It is untrusted LaTeX like any other: KaTeX renders it in its untrusted mode, and the page shows it marked as the model's.

**What leaves the machine.** With model repairs on, each formula that neither KaTeX nor the rules can render, with KaTeX's error and whether it is displayed, goes to the model gateway, and from there to the provider behind the reviewer alias. Nothing else of the text goes: not the sentence around the formula, nor the project, session or run. A formula from embargoed or restricted work goes too, so leave model repairs off, or point the reviewer alias at a model the data may reach, when such work is shown.

The repairs, the approximation and the endpoint are adapted from the user's own `cscience-crypto` (`src/web/cscience-math.js` and `pkg/platform/lib/math-repair.mjs`, with their tests), contributed under the MIT License; each file says so. Its patches to the upstream Web UI are not part of it.

### Differences from the specification

The [living documents](specs/living-documents.md) spec states this behavior for the platform (LD-41 to LD-48). The workspace differs from it in three ways:
- it reads LaTeX's delimiters while parsing, where LD-41 drafts a conversion before parsing, and saves a living document's text in the dollar form with a conversion checked against that parse ([living documents as built](LIVING-DOCUMENTS.md#changes));
- the repair endpoint is the workspace server's, with cache isolation by project and effective context and a limit for each browser session, where LD-45 and LD-46 put it in the control API with records of each call and the project's model budget;
- a formula that defines a macro is shown as written, where LD-28 has the GUI apply a definition made inside a span to that span alone, with a warning. The GUI refuses definitions because one can multiply KaTeX's work many times over within its limit on expansions (the 480 characters that took 35 s), and because macros belong in the project's settings and the document's front matter, which LD-28 provides. Whether to keep the refusal, or apply definitions within a span under a tighter limit, is a point for the user.

Document revision saves schedule durable math checks with this KaTeX and these settings. Rejected formulas in eligible agent and worker blocks create mechanical `warn`/`math` findings with exact provenance, suggested deterministic remedies and delivery only when a run closes ([document math checks](MATH-CHECKS.md)). The operator must configure the renderer; without it, checks stay inconclusive.

## Checks

```bash
cd web/workspace
python3 ../../scripts/check_licenses.py --npm package-lock.json
npm ci --ignore-scripts
python3 ../../scripts/check_licenses.py --npm package-lock.json --installed
npm run format:check    # prettier; npm run format rewrites src
npm run check    # svelte-check and tsc
npm test         # vitest
npm run build
```

CI runs these in the `web` job. The license check reads the lockfile before anything is installed: a version 3 lockfile, in which every package must declare a tier 1 license ([licensing](LICENSING.md)), come from the npm registry with a SHA-512 integrity hash, and, unless it is optional, need no install script. License expressions are read with their precedence, so `(MIT OR Apache-2.0) AND GPL-3.0-only` fails. After `npm ci`, `--installed` also checks each installed package's own `package.json`, which the integrity hashes cover, against the lockfile's license and version.

The notice tests compare two builds byte for byte, check every emitted file against the notice manifest, and check the primary notice texts, the runtime helpers, all sixty fonts and embedded copyright and reserved names. Negative cases cover missing or changed licenses, lockfile and install mismatches, unknown modules, helpers, styles and assets, modified fonts, outside origins and public-directory copying. SIL OFL applies to the unmodified font assets and embedded glyph-derived SVG assets; the Apache 2 and MIT embedded helpers fit the platform-code allowlist.

The app's tests (vitest, next to each module in `src/lib`) cover the shared parser cases through both parsers, the attachment of the server's IDs, macros and their bounds, the Markdown component's escaping, links, images and math budgets, the math delimiters, repairs and approximation, the caches, the requests for model repairs, the routes and the polling panels. The server's tests (`packages/workspace/tests`) cover login and sessions, the origin and host checks, the headers, the static files, every API route against stores the orchestrator's own code wrote, the worker proxies against fake peers, the math repair service against a scripted model and a fake gateway, the command end to end, and that the API table above matches the registered routes. Clipping a text to its box is a stylesheet rule, so it is checked in a browser.

Packages are untrusted downloads. `web/workspace/.npmrc` turns install scripts off, saves exact versions and refuses a Node version the tools do not support. To add or update one, pick a version some weeks old and resolve with a cutoff date a little after it, then run the license check:

```bash
npm install --ignore-scripts --before=2026-10-07T00:00:00Z some-package@1.2.3
```

## Not built

- Chat with an agent from the GUI, and a live transcript of a run in progress; detailed plan progress history and plan approval.
- Rich-text editing, automatic merging of source conflicts, declining suggestions in the GUI, and restoring deleted documents; artifact and PDF anchors and annotation export; and the ledger and board ([research workspace](RESEARCH-WORKSPACE.md)).
- Approvals in the GUI, with re-authentication for high-risk ones (D54), and policy revisions from the GUI. Provisioning operations and permission requests are shown but cannot be answered or changed from the GUI.
- Previews of rich artifacts on a separate origin (D54), and the PDF viewer.
- Saving Markdown with its math in the dollar form beyond living documents: ledger entries, and direct edits of document files (LD-41, LD-42, LD-50).
- Math checks for ledger entries and for reviewer, USER and platform prose; automatic compaction of check history; and idle dispatch of checks (the rest of LD-53 to LD-55, D82). Durable records of math-repair model calls and their budget (LD-46).
- KaTeX in a Web Worker, with a time limit that ends a formula's render ([the backlog](../.agents/BACKLOG.md) says why it waits).
- The end of a session without `session close`.
- TLS, sessions that survive a restart, and the GUI on another machine than the harness (W5).
