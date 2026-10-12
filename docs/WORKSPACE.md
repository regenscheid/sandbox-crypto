# Research workspace

The workspace is the research GUI (W4, D80). It shows the harness's records: the projects, their sessions and environments, and each session's runs, with the agent's messages, its tool calls and their results, and the LaTeX in them rendered; and the project's living documents, each revision with the server's block IDs, and who changed what ([living documents](#living-documents)). LaTeX that KaTeX rejects is repaired where it can be, by rules or by the reviewer model, and marked ([math repairs](#math-repairs)). It has two parts:

- `web/workspace/`: the app the browser runs. Svelte 5 with TypeScript, built by Vite into static files: an `index.html`, one script, one stylesheet and KaTeX's fonts.
- `packages/workspace` (`sandbox_science.workspace`): the server, in the project's Python on aiohttp, with the command `sandbox-science-workspace`. It serves the built files and a JSON API, handles login, and asks the reviewer model for math repairs when it is configured to.

Session pages also show live background jobs, waits and watcher notes, with cancellation ([background jobs](#background-jobs)). Documents can be edited as Markdown source or by block, with conflicts and retained patches reviewed explicitly. Comments, the ledger and board, and approvals are not built yet ([not built yet](#not-built-yet)).

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

Open it in a browser on the same machine. `--state-dir` is the directory the harness keeps its records in (`sandbox-science-harness … --state-dir`); the server reads project and run records. With a worker connection, it also reads job control state and can cancel a job through the worker. A session's runs appear once the harness keeps them, which it does for every `run --state-dir` ([run records](#run-records)).

`--listen` defaults to `127.0.0.1:8200`, and port 0 picks a free port. The server refuses an address that is not loopback unless `--allow-remote` is given. It speaks plain HTTP, so on another address the login URL and the session cookie cross the network unencrypted, and it then accepts any host name ([authentication](#authentication)). Placing the GUI on another machine is W5's work.

Math repairs by the reviewer model are off unless three settings name the model gateway, as the harness's `run` does ([math repairs](#math-repairs)):

```bash
uv run sandbox-science-workspace serve --state-dir harness-state --static web/workspace/dist \
    --model-url http://127.0.0.1:4000 --reviewer-model reviewer --api-key-file workspace.key
```

**The server's network reach.** It listens where `--listen` says. It can contact the model gateway at `--model-url` for math repairs, and the worker at `--worker` for live job views and cancellation. Without either setting it opens no outbound connection.

For work on the app itself, `npm run dev` serves it with Vite and passes `/api` and `/login` to a server on port 8200. Log in through the dev server's port. Logging out does not work there, since the dev server is another origin, and Vite's pages do not carry the server's headers.

The app is a directory of its own, `web/`, rather than a member under `packages/`: uv takes every directory in `packages/` for a Python project, and pyright and pytest search it, while the app's toolchain is Node's. Each browser app gets its own directory under `web/`.

## Authentication

Login works like Jupyter's, for one user on their own machine (threat model, zone Z4: the GUI is trusted by the user, the content it shows is not).

- **Login token.** At each start the server makes a random token (32 bytes, 43 URL-safe characters) and prints the login URL once, on standard output; it neither logs nor stores the token. A restart makes a new token and ends every session.
- **Login.** `GET /login?token=…` compares the token in constant time. A match sets the session cookie `sandbox_science_workspace-<port>` (32 random bytes; HttpOnly, SameSite=Strict, Path `/`, and Secure over HTTPS) and redirects to `/`, so the token leaves the address bar. Browsers send a host's cookies to every port on it, so the name carries the port, and two workspaces on one machine keep their sessions apart. The server keeps only the cookies' SHA-256 digests, in memory. A session lasts until the server stops or the user logs out (`POST /api/v0/logout`); past 64 sessions, the oldest ends.
- **The API needs the session.** Every route under `/api/` answers 401 without it.
- **Changes need the server's own origin.** A request with any method other than GET or HEAD must carry an `Origin` equal to the server's own (scheme and `Host`); a missing or `null` origin is refused. Nothing answers CORS: no route takes preflights or sets `Access-Control-*` headers. SameSite=Strict also keeps the cookie off requests started by other sites. The routes that take a POST are logout, [math repairs](#math-repairs), and [job cancellation](#background-jobs); approvals and edits will.
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
- **Math** is written with dollars, or with LaTeX's own delimiters: `\(…\)`, `\[…\]`, and display environments such as `\begin{align}…\end{align}` written without delimiters ([where math begins and ends](#where-math-begins-and-ends)). It goes to KaTeX in its untrusted mode (`trust: false`: `\href`, `\url`, `\includegraphics` and `\html…` show in red as written, and do nothing), with positive sizes capped at 20 em and at most 1,000 macro expansions. A formula that defines a macro (`\def`, `\gdef`, `\let`, `\newcommand` and the like) is shown as written: macros come from a document's front matter ([living documents](#living-documents)) and are to come from the project (LD-28), and a definition can multiply KaTeX's work far beyond the formula's length within that limit (a formula of 480 characters took KaTeX 35 s). Commands are read token by token with KaTeX's own token pattern, so a `\verb|…|` body or a `%` comment defines nothing. A comment starts only at a token that is `%` alone, as in KaTeX: `%` with a combining mark is another token, which a macro such as `\@secondoftwo` can skip unparsed. Where KaTeX's lexer would stop with an error, the reading goes on, finding more rather than less. KaTeX reads a `%` in the link of `\href`, `\url` or `\includegraphics` as a character, and which `%` that is depends on how it parses the formula, so in a formula with one of them and a `%`, every name after a backslash counts. LD-28 has the GUI apply such a definition to its own span, with a warning; this differs ([below](#math-repairs)). A formula KaTeX cannot parse is repaired if it can be, and otherwise shown as a plain-text approximation; either way it is marked, and its LaTeX as written is behind the mark ([math repairs](#math-repairs)). Rendering never changes the text. KaTeX's MathML holds each rendered formula's LaTeX, for assistive technology; selecting and copying a formula copies the characters of its rendering, not its LaTeX.
- **Nothing a text draws leaves its own box.** KaTeX caps only positive sizes, so `\smash` and a negative `\raisebox` could draw an agent's text over the tool call below it; the stylesheet clips each rendered text to its box (`contain: paint`).
- **Limits.** Text over 200,000 characters is shown as written. One text sends KaTeX at most 1,000 formulas and 20,000 characters of math, and no formula over 5,000 characters, since KaTeX's output is about 150 times its input; math beyond that is shown as written. The repairs of one text's formulas send KaTeX at most 20,000 characters more; beyond that, its formulas are approximated without them. Texts render in goes of 250 ms, each a task of the page's thread, with the Markdown, KaTeX and the page's update all counted, as when a page first shows all its texts. Within a go, texts render one after another. Once its time is spent, no further text starts: one that comes later shows as it was last rendered, or as written if it never was, and waits for its turn in a later go. KaTeX stops too, between formulas, and the math it had no time for shows as written, marked *not rendered yet*, until a later go renders the text again, starting where it stopped. The first text and the first formula of a go always render, so each go gets further, and a go can run over by one text's Markdown and one formula. So however much text and math a page has, its first view holds the page's thread about a quarter of a second, and the browser draws and takes input between goes. A formula whose repairs had no time waits for a later go too, and the model is not asked for it before the rules have been tried. Without definitions, the slowest formula of 5,000 characters found takes KaTeX about 60 to 80 ms (alternating `\Huge` and `\tiny`). KaTeX renders on the page's own thread, so one formula cannot be interrupted; running it in a Web Worker, with a time limit that ends the worker, is in the backlog. The page keeps KaTeX's result for each formula, and the repairs' candidates and approximations, each within a count and a size, so a text rendered again sends KaTeX only what is new. The macros in force are part of what these results, and the model repairs, are kept by (LD-28).
- **Tool calls** show their arguments and results as plain text. A result's rich outputs (HTML, SVG, images) are not rendered.
- **Code**, in tool calls and in Markdown, shows bidirectional control characters as their code points, such as `⟦U+202E⟧`, so that code cannot read differently from what ran.

D54 puts previews of untrusted artifacts (HTML, SVG, notebook output, PDFs, and agent-written Markdown) on a separate origin, in sandboxed frames. This slice shows only conversation text, in the app's own origin, under the rules above and the policy that admits no inline script. Rich artifacts wait for the separate origin.

### Where math begins and ends

Comments and anchors are to address math spans, so the browser and the server must agree where each formula begins and ends (D65). The browser's rules (`src/lib/dollarmath.ts`) are a port of the `dollarmath` plugin of mdit-py-plugins 0.6.1, which the server uses with markdown-it-py ([living documents as built](LIVING-DOCUMENTS.md#parsing)), with these settings on both sides, and one rule of this project's own:

| Setting | Value | Effect |
| --- | --- | --- |
| `allow_space` | false | `$ x$` and `$x $` are not math, so amounts such as "$5 and $10" stay text |
| `allow_digits` | true | A digit may touch a dollar sign. The plugin's check for the other setting has no exact JavaScript twin |
| `double_inline` | true | `$$…$$` inside a paragraph is displayed math, as agents often write it |
| `allow_blank_lines` | false | A blank line ends a `$$` block, so a stray `$$` cannot swallow the rest of a text |
| `allow_labels` | true | `$$…$$ (eq:1)` labels an equation |
| A `$$` block ends within 200 lines of its opening line (this project's own) | | The plugin looks to the end of the text from every `$$` that does not close, which makes some texts take quadratic time; past the limit, `$$` is ordinary text, so such math is displayed within its paragraph |

The dialect is markdown-it's default (markdown-it-py: `js-default`) with HTML off. The cases in `src/lib/dollarmath.fixtures.json` pin the spans, and the server's parser passes every one, the line limit applied to the plugin's block rule ([living documents as built](LIVING-DOCUMENTS.md#parsing)). Of 120,000 random texts compared with the plugin, 4 gave other spans, all because markdown-it 15 and markdown-it-py 4.2 split a table differently when rare Unicode whitespace (the information separators, NEL, the line separator) starts its rows; with tables off, none did. The port keeps the plugin's quirks, which the cases record: a `$$` block in a block quote keeps the quote markers in its content (a document's view renders it without them), a block can end in the next list item, the text is the plugin's (stripped of all Unicode whitespace, except an image's description, which neither side strips), and a backslash that ends a text escapes a dollar sign that starts it.

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

**The endpoint.** `POST /api/v0/math-repair` takes `{"tex", "display", "error"}`: one formula without its delimiters, at most 4,000 characters, whether it is displayed, and KaTeX's error, cut to 500 characters. Like every API route it needs the session, and like every POST the server's own origin. It answers `{"enabled": false, "tex": null, "reason"}` when the server has no reviewer model, and the page then asks no more; otherwise `{"enabled": true, "model", "tex", "reason", "cached"}`, where `tex` is the repair or null and `reason` says why there is none (`no usable repair`, `busy`, a gateway failure).

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

The repairs, the approximation and the endpoint are adapted from the user's own `cscience-crypto` (`src/web/cscience-math.js` and `pkg/platform/lib/math-repair.mjs`, with their tests), contributed under the MIT License; each file says so. Its patches to the upstream Web UI are not part of it. The port fixes faults of the original: names such as `\constructor` rendered as JavaScript functions in the approximation, `\sqrt[{3}]` added `undefined`, nesting deep enough to exhaust the stack threw instead of falling back, the rules changed escaped backslashes and could join pieces into a command, and a `busy` answer was kept for good.

An adversarial review of this slice (2026-10-11) found no way to run script, load anything or make a link, and the key never reached the page. It found ten other issues, all fixed. The worst made the page freeze: every answer rendered the whole text again, and when the server had no model, every waiting formula did so at once (300 broken formulas froze a page for 37 s); a run of backslashes took quadratic time (199,000 took 6.8 s); and formulas with definitions made KaTeX work for many seconds. The others: a regular expression's backtracking on a model's answer; a key quoted by the gateway reaching the log in part; failures that were not kept, and so were asked for again on every load; redirects followed and answers read whole; a mark a formula could draw over; the cases above where these rules and dollar math's disagreed; and this page saying more than was true. A second review confirmed those fixes and found no critical, high or medium issue. Its eight smaller findings are fixed too:
- a `busy` answer that came after another answer turned repairs off left its formula waiting for good;
- KaTeX's second of time was for each text, not the page, so a page of 30 texts of the slowest formulas held the thread 9 s on its first view (now 0.33 s, with the rest rendered in later goes of at most 0.34 s each);
- escaped brackets around prose, such as `\[...\]`, `\[1, 2\]` and Pandoc's citations, still showed as math;
- a Python port of that rule could read a line break differently;
- a mark's message and title showed bidirectional controls raw where KaTeX's error quoted them;
- the refusal of definitions read `\verb` bodies and comments, which KaTeX does not;
- the page's caches were bounded by count alone, and its model repairs not at all;
- three smaller points: a fence of one line kept its info string, the harness's own calls stopped following redirects without the docs saying so, and the model could be asked for a formula whose rules KaTeX had not had time to try.

A third review, of those fixes, found one issue of medium severity and two of low, all fixed:
- the new reading of commands took every `%` for a comment. KaTeX reads `%` with a combining mark after it as a token of its own, which `\@secondoftwo`, `\TextOrMath` or `\@ifnextchar` can skip or peek at unparsed, and then defines what follows: a formula of 499 characters took 39 s. Commands are now read with KaTeX's own token pattern, and are refused wherever KaTeX's reading is uncertain. Against KaTeX's lexer on 228,947 token streams, and against KaTeX itself on three million random formulas, nothing it defines goes unrefused;
- each go rendered again every text that waited, Markdown not counted: 30 texts of 150,000 characters with four slow formulas each made 609 renderings in 24 s, with a task of 1 s. Texts now take turns, and only rendering that does work happens: 63 renderings in 12 s, no task over 340 ms;
- the bracket rule turned honest formulas written within a sentence, such as `\[ f(x) \]`, into text, and read Pandoc's escapes, such as `\[path\_to\_dir\]`, as math. The rule above now takes padding as math and Markdown escapes as text.

Fixing these turned up one more: since the second review, each `\[` within a paragraph was judged by reading its content up to the next `\]`, so many `\[` before one `\]` took quadratic time (160,000 characters, 19 s). An index of the text now judges each at once (37 ms).

The [living documents](specs/living-documents.md) spec states this behavior for the platform (LD-41 to LD-48); it is a draft whose main questions the user settled on 2026-10-11. This slice differs from it in four ways, for now: it reads LaTeX's delimiters while parsing, where LD-41 drafts a conversion before parsing (see above), and saves a living document's text in the dollar form with a conversion checked against that parse ([living documents as built](LIVING-DOCUMENTS.md#changes)); the endpoint is the workspace server's, with a cache for the server and a limit for each browser session, where LD-45 and LD-46 put it in the control API, with a cache for each project, records of each call and the project's model budget; copying a rendered formula does not copy its LaTeX (LD-44); and a formula that defines a macro is shown as written, where LD-28 has the GUI apply a definition made inside a span to that span alone, with a warning. The GUI refuses definitions because one can multiply KaTeX's work many times over within its limit on expansions (the 480 characters that took 35 s), and because macros belong in the project's settings and the document's front matter, which LD-28 provides: the renderer takes a document's front matter's macros, bounded ([living documents](#living-documents)). Whether to keep the refusal, or apply definitions within a span under a tighter limit, is a point for the user. The spec's math findings (LD-53 to LD-55, D82) are not built either: when an agent's text is saved, the server is to check its math with this KaTeX and these settings, and a formula it rejects goes back to that agent as a `math` finding, with the repair these steps find as the suggested fix, while the display shows that repair, marked.

## Living documents

The project's living documents and its sessions' workbooks show each revision and who changed what ([views](#views)). The current revision offers source and block editing, and workbook documents can move to project scope. Earlier live revisions offer an explicit revert. History previews retained patches against the current text and lets the user apply them with that exact revision as the guard. See [the reconciliation rules](LIVING-DOCUMENTS.md#changes).

Drafts retain their base revision in memory across views and in this browser tab's session storage across reloads. Conflicts show the newer text and author while keeping the draft. Resolving a whole-source conflict starts with the current text and retains the old draft separately; block resolution touches only the named block. A browser without storage retains the in-memory draft and warns before closing. Inputs are disabled while a save is pending.

Documents live in the harness's control store ([living documents as built](LIVING-DOCUMENTS.md)). Reads use a read-only connection; explicit user changes open a separate writable connection lazily. The API derives the user attribution itself; an agent or request body cannot choose it. The existing login cookie, exact same-origin check on writes, project lookup and scoped document lookup protect these routes.

**One parse on both sides** (LD-5, LD-9). A document is parsed by the text renderer's parser with two rules more, as the server parses it: YAML front matter at the very start of the text, and theorem-like containers, `::: lemma (label)` to `:::` (`src/lib/documentrules.ts`, ports of mdit-py-plugins' `front_matter` and `container`, MIT, credited in the file). The rules are registered in the server's order, and each block rule records where the blocks it makes lie and where their own text begins on each line (`src/lib/blockmap.ts`), from which the browser derives its block map as the server does: each block's kind, depth and lines, without trailing blank lines, and each formula's block, kind and TeX. The shared cases in `src/lib/documents.fixtures.json` pin both, and run through both parsers in CI, with the dollar and delimiter cases run through the document parser too. On 2026-10-11, 15,000 random documents built from the profile's constructs gave the same blocks, lines and formulas on both sides.

**The server's IDs** (`src/lib/attach.ts`). The revision's block map comes from the server with each block's ID, kind, depth, lines and status, and each formula's ID, kind and TeX. A block of the browser's parse takes the ID of the server's block of the same kind, depth and lines, in order, and its formulas take the server's span IDs when both read the same kinds and TeX. Each block's element carries its ID as `data-block`, shown at its top right while the pointer is over it, and each formula's carries `data-math`. Where the parsers disagree, the view says where and why, and that part keeps no ID and is outlined: it never guesses which block is which. The one disagreement known is in tables: markdown-it trims a table's lines with JavaScript's white space and markdown-it-py with Python's, so a header line that begins or ends with a byte-order mark, or with U+001C to U+001F or U+0085, is a table on one side and a paragraph on the other ([living documents as built](LIVING-DOCUMENTS.md#parsing)). A revision whose block map a parser of another version made comes without its formulas' TeX; its blocks still take their IDs, and its formulas theirs in order.

**What a revision changed.** A block the revision changed, added or moved is marked at its left, in the colors of its status; a first revision marks none. A displayed equation in a quote is rendered without the quote's markers, which its TeX keeps as the plugin reads it.

**Macros** (LD-28; `src/lib/macros.ts`). A document's math renders with its front matter's macros, as the server read them. KaTeX expands a macro wherever a formula uses it, and a few short macros that use one another can expand into a great many characters: three of them, written inside a formula, once froze a page for 39 s. So, on both sides:

- a document has at most 200 macros, each a control sequence defined in at most 1,000 characters;
- a macro whose definition defines another, as `\def` does, or uses one of its arguments more than once, which would let it multiply what it is given, is left out;
- the server also leaves out a macro that expands into itself, or to more than 2,000 characters with the macros it uses expanded, and warns of each on the revision;
- and as KaTeX renders a formula, each expansion of one of the document's macros counts its definition's characters: a formula whose macros expand to more than 2,000 characters in all is shown as written, with why.

A document's macros have a key, by their definitions, that KaTeX's results and the model repairs are kept by, so no formula shows a rendering made under other macros. The project's own macros, which LD-28 puts beneath a document's, are not built yet. A formula that defines a macro is still shown as written ([math repairs](#math-repairs)).

**Limits.** A document holds up to 1 MiB of text and 20,000 formulas, as the server keeps them; its math renders in goes as a text's does ([showing untrusted text](#showing-untrusted-text)), so a long document takes more goes, not longer ones. The same renderer shows it, with the same rules for links, images, raw HTML and math.

## API v0

All routes need the session and answer JSON. The views of the records are GET (and HEAD); the two routes that take a POST also need the server's own origin.

| Route | Answer |
| --- | --- |
| `/api/v0/projects` | `projects`: each project's ID, base profile, policy revision, default environment, times and number of sessions. A damaged record is listed with its `error` |
| `/api/v0/projects/{project}` | `project`: the same facts, its `sessions` (each with its environment, whether it is dedicated, when it was opened and closed, and how many runs it keeps), and its `environments`, the default first, each with the sessions in it |
| `/api/v0/projects/{project}/sessions/{session}` | The `session` and its `runs`, oldest first: each run's `name`, `run_id` and `started_at` |
| `/api/v0/projects/{project}/sessions/{session}/runs/{run}` | The run's record ([run records](#run-records)), at most 64 MiB |
| `/api/v0/projects/{project}/documents` | `documents`: the project's living documents and its sessions' workbook documents, without the deleted ones: each one's ID, name, title, scope and session, creator, current revision and whether agent patches wait as suggestions |
| `/api/v0/projects/{project}/documents/{document}` | One revision of a document, by its ID or, for one of the project's own, its name: the current one, or `?revision=N`. Its `document`, its `revision` (author, the user who accepted it, reason, base and parent, warnings, what was normalized, the parsers' versions), its `text`, its front matter's `title` and `macros`, and its `blocks`: each one's `id`, `kind`, `parent`, `depth`, `lines`, `status`, structural kind and attributes, with its formulas' `id`, `kind`, `status` and `tex`. `matched` is false when the server could not parse the revision as its block map was made, and the formulas then come without their TeX |
| `/api/v0/projects/{project}/documents/{document}/history` | Who changed what (LD-11): each revision with its author, reason and the blocks it changed, added, moved and removed; each block with the revisions that changed it; and the agents' patches with their states |
| `POST /api/v0/math-repair` | The reviewer model's repair of one formula ([math repairs](#math-repairs)) |
| `POST /api/v0/logout` | Ends the session: 204, no body |

Document writes use `POST /api/v0/projects/{project}/documents/{document}/changes`, with a strict action (`source`, `patch`, `revert`, `promote`, or `apply_patch`), `base`, `reason` and `operation_id`. The action carries respectively `text`, `operations`, `target`, `name`, or `patch_id`; unknown fields are refused. Results include `outcome`, revision, retained patch ID and structured conflicts. `GET /api/v0/projects/{project}/documents/{document}/patches/{patch}/preview` returns the retained operations, exact current `base`, current and proposed source, and whether the named operations can safely apply. A revision's blocks also include their editable Markdown `source`, without their containing block's indentation.

Errors are `{"error": {"kind", "message"}}` (document errors also carry `details`) with the worker API's kinds: 400 `invalid_request` for a malformed ID, run name, document name or revision number, or a math repair request that is not one, 401 `policy_denied` without a session, 404 `not_found`, 413 for a request body over 64 KiB (document changes allow 8 MiB of escaped JSON, while document text remains bounded to 1 MiB of UTF-8), 422 `unsupported` for a record over the size limit, 429 `unavailable` for a session that has asked for as many model repairs as it may for now, and 500 `internal` for a damaged record. The server reads the harness's records through the orchestrator's `ProjectStore`, and the documents through its `DocumentStore`, in a thread. JSON that a record cannot carry, a NaN from a tool's output say, is sent as a string.

Reading the harness's state directory directly is this slice's arrangement: the workspace runs beside the harness and imports the orchestrator's store (the one deployable member importing another, in the [conventions](../.agents/CONVENTIONS.md)). W5 gives the control application an API of its own for the GUI to use.

## Background jobs

With `--worker URL --worker-token-file FILE`, session pages show all active jobs and
20 recently ended jobs, including state, elapsed time, CPU and memory when available,
output head and latest tail, watcher progress notes and failures. The browser asks only
the workspace server, every three seconds. Keys never enter the browser; the server
uses the recorded session's environment and checks job ownership before viewing or
cancelling. The Cancel job button uses a POST under the same authentication and Origin
guard as other writes. Output and watcher text are escaped plain text.

Active waits show what the agent waits on, for how long, and the watcher alias, or say
that check-ins are off when no watcher was configured. An expired harness heartbeat is
shown as disconnected. Job completion and attention notes stay available after a wait
ends. Running jobs currently become lost when the worker restarts, until the executor
daemon is built. See [background jobs as built](BACKGROUND-JOBS.md) for the harness
API, durable notices, watcher budgets and the current message boundary and wake limits.

```bash
uv run sandbox-science-workspace serve --state-dir harness-state --static web/workspace/dist \
    --worker http://127.0.0.1:8100 --worker-token-file worker.key
```

Without a worker connection the jobs panel reports that live job views are unavailable.
Project, session, run and document views continue to work.

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
| `job_notice_ids` | Completion IDs represented by this run; acknowledged after the run is saved |
| `messages` | The conversation in the OpenAI chat format the harness uses, system prompt first |
| `tools` | One record per tool call, in order: its name, its arguments as the model wrote them, and its outcome (the worker's execution record, or an artifact, an error kind, …) |

The harness writes `--transcript` first. A record it cannot keep, such as one whose tool output is nested deeper than its schema follows, is reported, and the command exits 1. A run that ends in an error, such as an unreachable model gateway, keeps no record yet.

## Views

Hash routes, without a router library: `#/`, `#/projects/<project>`, `#/projects/<project>/sessions/<session>`, `#/projects/<project>/documents/<document>` with `/revisions/<number>` for an earlier revision, and `#/projects/<project>/documents/<document>/history`.

- **Projects:** each project's base profile, policy revision, number of sessions and last change.
- **Project:** its facts; its sessions, each with its environment (the project's default or its own), number of runs, and whether it is open; its documents, the project's and its sessions' workbooks'; and its environments with the sessions in each. These are the harness's records; the environments' live state is on the worker, which the workspace does not ask yet.
- **Session:** its environment and state, and its runs, oldest first. The newest is open; the others are fetched when opened.
- **Run:** when it ran and for how long, its environment and policy revision, and whether it answered. Then the system prompt (folded), the task, and the agent's messages, each with its tool calls: the arguments (code as a block), and the result the model read, laid out: state and exit code, stdout, stderr, outputs, the error and its traceback, platform notices, and where the call ran, with the isolation tier in effect. Anything else in the result shows as JSON, and the call's full record opens on request. The last message is the answer.
- **Document:** a revision, the current one unless the address names another: its title, name and scope, its number with links to the revisions before and after it and to the history, its author (with the user who accepted it when it was a suggestion), when it was made and why, whether its delimiters were saved in the dollar form, and its warnings (LD-30). Then where the browser's parse and the server's block map disagree, if they do, and the text, rendered with the server's IDs ([living documents](#living-documents)). A revision that deleted the document says so.
- **History:** the revisions, newest first, each with its author, reason and the blocks it changed; the revisions that changed each block, folded; and the agents' patches with their states, and the revisions they made.

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

The tests cover the shared document cases through the document parser, which also gives every dollar and delimiter case the text parser gives; the server's IDs attached to the browser's parse, the disagreements reported and the parts they concern left without IDs; a document's macros (rendering, the bounds, and KaTeX's results kept apart by the macros in force); a quoted equation rendered without the quote's markers; the routes; and the Markdown component (HTML escaped, links other than absolute http and https and mailto dropped, images never loaded, inline and displayed math, repaired and approximated math with its source behind the mark, KaTeX's untrusted commands and limits, the math budgets, goes in which texts take turns until the time is spent, those after them waiting as last rendered, each starting where it stopped and each go getting further, no model asked for a formula whose rules had no time, formulas that define macros, read token by token as KaTeX's lexer reads them, with KaTeX's own token pattern as the reference in the review's fuzz, KaTeX's results kept for the page, texts that used to take quadratic time or many seconds, bidirectional controls in code, in formulas shown as written and in KaTeX's errors), the math spans of both kinds of delimiters and how they combine (the dollar cases unchanged, the 200-line limit alike, escaped brackets), the repairs (escapes kept, no commands joined or brought in), the approximation, the caches (a count and a size; an entry heavier than the limit not kept), and the page's requests for model repairs (one until the server has a model, then two; dropped at once when it has none; told once a frame; `busy` asked again, but not once repairs are off; at most 2,000 kept), the routes and the transcript's layout. Clipping a text to its box is a stylesheet rule, so it was checked in a browser, against a formula that drew fake code over a tool call. The server's tests (`packages/workspace/tests`) cover the document routes against a control store the orchestrator's own store wrote (the documents of a project only, revisions by number, history, errors, the session they need, a revision another parser's version mapped, and the store never written), login and sessions, the cookie's name per port, the session on every spelling of `/api`, the origin and host checks, the headers, paths that try to leave the build directory, the API's answers from a state directory the harness's own store wrote, the math repair service against a scripted model and a fake gateway (the request it sends, cleaning answers quickly and of a fence's info string, the cache, two at a time and `busy`, timeouts, any failure kept for the hour, each session's limit, redirects not followed and answers read within a limit, the key kept out of answers and the log, and refused when it is not one line) and its endpoint and settings, the npm check, and the command end to end. Math repairs were also checked in a browser, against a fake gateway: formulas approximated while their repairs were pending, then rendered as the answers came. After the review, 300 and 800 broken formulas with no model on the server made one request and one render again, with the page's thread never held more than 70 ms, and a mark stayed above the box a formula drew over it. After the second, the review's page of 30 texts of four of the slowest formulas showed its first view after a task of 331 ms, rendered all 120 formulas in 9.9 s over 31 goes, none of them longer than 340 ms, and escaped brackets around `1` and `...` read as text. After the third, with Svelte's runtime in Node, the same texts with 150,000 characters of Markdown each finished in 12 s with no task over 340 ms. The document views were checked in a browser against a state directory the orchestrator's store wrote: the IDs shown over each block, the blocks a revision changed, the front matter's macros, a quoted equation, repaired formulas, a table the two parsers read otherwise shown as a mismatch, and the history, with no content-policy error in the console.

Packages are untrusted downloads. `web/workspace/.npmrc` turns install scripts off, saves exact versions and refuses a Node version the tools do not support. To add or update one, pick a version some weeks old and resolve with a cutoff date a little after it, then run the license check:

```bash
npm install --ignore-scripts --before=2026-10-07T00:00:00Z some-package@1.2.3
```

## Not built yet

- Rich-text editing, automatic source conflict merging, declining suggestions in the GUI, and restoring deleted documents; comments and anchors; the ledger and board ([research workspace](RESEARCH-WORKSPACE.md)); and review findings ([consistency review](specs/consistency-review.md)).
- The project's macros, beneath a document's (LD-28).
- Approvals in the GUI, with re-authentication for high-risk ones (D54), and policy revisions from the GUI.
- Previews of rich artifacts on a separate origin (D54), and the PDF viewer.
- Saving Markdown with its math in the dollar form beyond living documents, which are saved so ([living documents as built](LIVING-DOCUMENTS.md#changes)): ledger entries, and direct edits of document files (LD-41, LD-42, LD-50).
- Math findings: checking the math of saved text with the GUI's KaTeX, and sending what it rejects back to the agent that wrote it (LD-53 to LD-55, D82). The [isolated renderer prerequisite](MATH-CHECKS.md) exists; save scheduling, findings and delivery are not connected yet.
- Copying a rendered formula as its LaTeX (KaTeX's `copy-tex` does this); for now a repaired or approximated formula's LaTeX is copied from behind its mark.
- KaTeX in a Web Worker, with a time limit that ends a formula's render ([the backlog](../.agents/BACKLOG.md) says why it waits).
- Live updates while a run is in progress, and the environments' state from the worker.
- Records of runs that end in an error, and the end of a session without `session close`.
- TLS, sessions that survive a restart, and the GUI on another machine than the harness (W5).
