# Research workspace

The workspace is the research GUI (W4, D80). Its first slice is a read-only view of the harness's records: the projects, their sessions and environments, and each session's runs, with the agent's messages, its tool calls and their results, and the LaTeX in them rendered. It has two parts:

- `web/workspace/`: the app the browser runs. Svelte 5 with TypeScript, built by Vite into static files: an `index.html`, one script, one stylesheet and KaTeX's fonts.
- `packages/workspace` (`sandbox_science.workspace`): the server, in the project's Python on aiohttp, with the command `sandbox-science-workspace`. It serves the built files and a JSON API, and handles login.

Living documents, the ledger and board, comments and approvals are not built yet ([not built yet](#not-built-yet)).

## Building and running

```bash
cd web/workspace
npm ci --ignore-scripts      # exactly the packages in package-lock.json; no install scripts
npm run build                # writes dist/
cd ../..
uv run sandbox-science-workspace serve --state-dir harness-state --static web/workspace/dist
```

The server prints a login URL once:

```
Sandbox Science workspace on http://127.0.0.1:8200/
Log in by opening this URL; it works until the server stops:
    http://127.0.0.1:8200/login?token=…
```

Open it in a browser on the same machine. `--state-dir` is the directory the harness keeps its records in (`sandbox-science-harness … --state-dir`); the server reads them and changes none. A session's runs appear once the harness keeps them, which it does for every `run --state-dir` ([run records](#run-records)).

`--listen` defaults to `127.0.0.1:8200`, and port 0 picks a free port. The server refuses an address that is not loopback unless `--allow-remote` is given. It speaks plain HTTP, so on another address the login URL and the session cookie cross the network unencrypted, and it then accepts any host name ([authentication](#authentication)). Placing the GUI on another machine is W5's work.

For work on the app itself, `npm run dev` serves it with Vite and passes `/api` and `/login` to a server on port 8200. Log in through the dev server's port. Logging out does not work there, since the dev server is another origin, and Vite's pages do not carry the server's headers.

The app is a directory of its own, `web/`, rather than a member under `packages/`: uv takes every directory in `packages/` for a Python project, and pyright and pytest search it, while the app's toolchain is Node's. Each browser app gets its own directory under `web/`.

## Authentication

Login works like Jupyter's, for one user on their own machine (threat model, zone Z4: the GUI is trusted by the user, the content it shows is not).

- **Login token.** At each start the server makes a random token (32 bytes, 43 URL-safe characters) and prints the login URL once, on standard output; it neither logs nor stores the token. A restart makes a new token and ends every session.
- **Login.** `GET /login?token=…` compares the token in constant time. A match sets the session cookie `sandbox_science_workspace-<port>` (32 random bytes; HttpOnly, SameSite=Strict, Path `/`, and Secure over HTTPS) and redirects to `/`, so the token leaves the address bar. Browsers send a host's cookies to every port on it, so the name carries the port, and two workspaces on one machine keep their sessions apart. The server keeps only the cookies' SHA-256 digests, in memory. A session lasts until the server stops or the user logs out (`POST /api/v0/logout`); past 64 sessions, the oldest ends.
- **The API needs the session.** Every route under `/api/` answers 401 without it.
- **Changes need the server's own origin.** A request with any method other than GET or HEAD must carry an `Origin` equal to the server's own (scheme and `Host`); a missing or `null` origin is refused. Nothing answers CORS: no route takes preflights or sets `Access-Control-*` headers. SameSite=Strict also keeps the cookie off requests started by other sites. The only such route yet is logout; approvals and edits will need it.
- **The server's own names.** On a loopback address, every request must name the server in `Host`: `localhost`, `127.0.0.1`, `[::1]` or the address it listens on, with its port. A page whose own host name an attacker rebinds to 127.0.0.1 (DNS rebinding) is refused, and could not use the cookie in any case, since the cookie belongs to the name the user logged in with. With `--allow-remote` this check is off, because the names that reach the server are not known.
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

## Showing untrusted text

Everything the views show was written by an agent, a user, a paper or a program, so it is untrusted (threat T6). One renderer shows all text, the `Markdown` component (`src/lib/markdown.ts`), and the rest of the app shows text only as text.

- **Markdown** is markdown-it's default dialect (CommonMark, tables and strikethrough) with raw HTML off: tags in the text show as written.
- **Links** are kept only for absolute `http` and `https` addresses and `mailto`. Any other link, `javascript:` or relative, stays text. Links open in a new tab, without a referrer and without access to the app (`noopener noreferrer nofollow`). Bare addresses are not turned into links.
- **Images** become links and are never loaded, so text cannot make the browser fetch an address.
- **Math** goes to KaTeX in its untrusted mode (`trust: false`: no `\href`, `\url`, `\includegraphics` or `\html…`), with positive sizes capped at 20 em, at most 1,000 macro expansions, and no macros carried from one formula to the next. With `throwOnError: false`, a formula KaTeX cannot parse comes back as its source: it is shown as written, in a dashed box, with KaTeX's message beside it. An unknown command shows in red. Rendering never changes the text, and KaTeX's MathML keeps each formula's source, so copying a formula copies its LaTeX.
- **Nothing a text draws leaves its own box.** KaTeX caps only positive sizes, so `\smash` and a negative `\raisebox` could draw an agent's text over the tool call below it; the stylesheet clips each rendered text to its box (`contain: paint`).
- **Limits.** Text over 200,000 characters is shown as written. One text sends KaTeX at most 1,000 formulas and 20,000 characters of math, and no formula over 5,000 characters, since KaTeX's output is about 150 times its input; math beyond that is shown as written.
- **Tool calls** show their arguments and results as plain text. A result's rich outputs (HTML, SVG, images) are not rendered.
- **Code**, in tool calls and in Markdown, shows bidirectional control characters as their code points, such as `⟦U+202E⟧`, so that code cannot read differently from what ran.

D54 puts previews of untrusted artifacts (HTML, SVG, notebook output, PDFs, and agent-written Markdown) on a separate origin, in sandboxed frames. This slice shows only conversation text, in the app's own origin, under the rules above and the policy that admits no inline script. Rich artifacts wait for the separate origin.

### Where math begins and ends

Comments and anchors are to address math spans, so the browser and the server must agree where each formula begins and ends (D65). The browser's rules (`src/lib/dollarmath.ts`) are a port of the `dollarmath` plugin of mdit-py-plugins 0.6.1, which the server is to use with markdown-it-py, with these settings on both sides, and one rule of this project's own:

| Setting | Value | Effect |
| --- | --- | --- |
| `allow_space` | false | `$ x$` and `$x $` are not math, so amounts such as "$5 and $10" stay text |
| `allow_digits` | true | A digit may touch a dollar sign. The plugin's check for the other setting has no exact JavaScript twin |
| `double_inline` | true | `$$…$$` inside a paragraph is displayed math, as agents often write it |
| `allow_blank_lines` | false | A blank line ends a `$$` block, so a stray `$$` cannot swallow the rest of a text |
| `allow_labels` | true | `$$…$$ (eq:1)` labels an equation |
| A `$$` block ends within 200 lines of its opening line (this project's own) | | The plugin looks to the end of the text from every `$$` that does not close, which makes some texts take quadratic time; past the limit, `$$` is ordinary text, so such math is displayed within its paragraph |

The dialect is markdown-it's default (markdown-it-py: `js-default`) with HTML off. The cases in `src/lib/dollarmath.fixtures.json` pin the spans. On 2026-10-11 markdown-it-py 4.2.0 with mdit-py-plugins 0.6.1 gave the same spans for all of them but the one that tests the line limit. Of 120,000 random texts compared with the plugin, 4 gave other spans, all because markdown-it 15 and markdown-it-py 4.2 split a table differently when rare Unicode whitespace (the information separators, NEL, the line separator) starts its rows; with tables off, none did. The server's parser must pass every case when it is built, which for the line limit takes a small change to the plugin's block rule. The port keeps the plugin's quirks, which the cases record: a `$$` block in a block quote keeps the quote markers in its content, a block can end in the next list item, the text is the plugin's (stripped of all Unicode whitespace), and a backslash that ends a text escapes a dollar sign that starts it.

## API v0

All routes are GET (and HEAD), need the session, and answer JSON.

| Route | Answer |
| --- | --- |
| `/api/v0/projects` | `projects`: each project's ID, base profile, policy revision, default environment, times and number of sessions. A damaged record is listed with its `error` |
| `/api/v0/projects/{project}` | `project`: the same facts, its `sessions` (each with its environment, whether it is dedicated, when it was opened and closed, and how many runs it keeps), and its `environments`, the default first, each with the sessions in it |
| `/api/v0/projects/{project}/sessions/{session}` | The `session` and its `runs`, oldest first: each run's `name`, `run_id` and `started_at` |
| `/api/v0/projects/{project}/sessions/{session}/runs/{run}` | The run's record ([run records](#run-records)), at most 64 MiB |

Errors are `{"error": {"kind", "message"}}` with the worker API's kinds: 400 `invalid_request` for a malformed ID or run name, 401 `policy_denied` without a session, 404 `not_found`, 422 `unsupported` for a record over the size limit, and 500 `internal` for a damaged record. The server reads the harness's records through the orchestrator's `ProjectStore`, in a thread. JSON that a record cannot carry, a NaN from a tool's output say, is sent as a string.

Reading the harness's state directory directly is this slice's arrangement: the workspace runs beside the harness and imports the orchestrator's store (the one deployable member importing another, in the [conventions](../.agents/CONVENTIONS.md)). W5 gives the control application an API of its own for the GUI to use.

## Run records

With `--state-dir`, the harness keeps every run of a session, the same record `--transcript` writes, beside the projects' records:

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
| `stopped` | `answered`, or `turn_limit` when the model was still asking for tools |
| `final_text` | The answer |
| `messages` | The conversation in the OpenAI chat format the harness uses, system prompt first |
| `tools` | One record per tool call, in order: its name, its arguments as the model wrote them, and its outcome (the worker's execution record, or an artifact, an error kind, …) |

The harness writes `--transcript` first. A record it cannot keep, such as one whose tool output is nested deeper than its schema follows, is reported, and the command exits 1. A run that ends in an error, such as an unreachable model gateway, keeps no record yet.

## Views

Hash routes, without a router library: `#/`, `#/projects/<project>` and `#/projects/<project>/sessions/<session>`.

- **Projects:** each project's base profile, policy revision, number of sessions and last change.
- **Project:** its facts; its sessions, each with its environment (the project's default or its own), number of runs, and whether it is open; and its environments with the sessions in each. These are the harness's records; the environments' live state is on the worker, which the workspace does not ask yet.
- **Session:** its environment and state, and its runs, oldest first. The newest is open; the others are fetched when opened.
- **Run:** when it ran and for how long, its environment and policy revision, and whether it answered. Then the system prompt (folded), the task, and the agent's messages, each with its tool calls: the arguments (code as a block), and the result the model read, laid out: state and exit code, stdout, stderr, outputs, the error and its traceback, platform notices, and where the call ran, with the isolation tier in effect. Anything else in the result shows as JSON, and the call's full record opens on request. The last message is the answer.

## Checks

```bash
cd web/workspace
python3 ../../scripts/check_licenses.py --npm package-lock.json
npm ci --ignore-scripts
python3 ../../scripts/check_licenses.py --npm package-lock.json --installed
npm run check    # svelte-check and tsc
npm test         # vitest
npm run build
```

CI runs these in the `web` job. The license check reads the lockfile before anything is installed: a version 3 lockfile, in which every package must declare a tier 1 license ([licensing](LICENSING.md)), come from the npm registry with a SHA-512 integrity hash, and, unless it is optional, need no install script. License expressions are read with their precedence, so `(MIT OR Apache-2.0) AND GPL-3.0-only` fails. After `npm ci`, `--installed` also checks each installed package's own `package.json`, which the integrity hashes cover, against the lockfile's license and version.

The tests cover the Markdown component (HTML escaped, links other than absolute http and https and mailto dropped, images never loaded, inline and displayed math, a math error shown beside its source, KaTeX's untrusted commands and limits, the math budget, texts that used to take quadratic time, bidirectional controls in code), the math spans, the routes and the transcript's layout. Clipping a text to its box is a stylesheet rule, so it was checked in a browser, against a formula that drew fake code over a tool call. The server's tests (`packages/workspace/tests`) cover login and sessions, the cookie's name per port, the session on every spelling of `/api`, the origin and host checks, the headers, paths that try to leave the build directory, the API's answers from a state directory the harness's own store wrote, the npm check, and the command end to end.

Packages are untrusted downloads. `web/workspace/.npmrc` turns install scripts off, saves exact versions and refuses a Node version the tools do not support. To add or update one, pick a version some weeks old and resolve with a cutoff date a little after it, then run the license check:

```bash
npm install --ignore-scripts --before=2026-10-07T00:00:00Z some-package@1.2.3
```

## Not built yet

- Living documents, the ledger and board, comments and anchors ([research workspace](RESEARCH-WORKSPACE.md)), and review findings ([consistency review](specs/consistency-review.md)).
- Approvals in the GUI, with re-authentication for high-risk ones (D54), and policy revisions from the GUI.
- Previews of rich artifacts on a separate origin (D54), and the PDF viewer.
- Markdown parsed on the server with markdown-it-py and mdit-py-plugins, passing the shared math cases.
- Live updates while a run is in progress, and the environments' state from the worker.
- Records of runs that end in an error, and the end of a session without `session close`.
- TLS, sessions that survive a restart, and the GUI on another machine than the harness (W5).
