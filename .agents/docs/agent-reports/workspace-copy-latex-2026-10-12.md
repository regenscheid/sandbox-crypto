# Original-LaTeX clipboard acceptance — 2026-10-12

Copying a formula from the shared Markdown renderer now puts its original LaTeX
on the plain-text clipboard, including parser delimiters and body whitespace.
The production workspace passed **19 native keyboard copy/paste cases** against
its actual rendered document and unsaved source editor. The disposable browser,
two servers, private login file and marked temporary state were removed.

## Source and validation

- Independently cleared signed source:
  `837962af8a428362a6843dda216180f38307bbb9`, with parents `1902131` and actual
  main `05ba353` (merged PR #91). Feature/helper blobs match the cleared feature
  parent; license/server/build inputs match main.
- Fresh combined gates passed **501 web tests**, including **29 copy cases**,
  **195 focused Python repair/parser cases**, **24 post-build math cases**, both
  production builds, Svelte/TypeScript with zero errors/warnings, locked uv/npm
  installs, Ruff format/lint, strict Pyright, Python/npm licenses and generated
  protocol checks. The archive preserves the exact two gate logs.
- The broad backend Python result, **9,749 passed / 43 opt-in skips / two
  existing warnings** in 327.71 seconds, is explicitly reused from signed
  `123b2e6`. Packages, scripts, CI, Python configuration/lock and npm dependency
  inputs are unchanged. PR #90 and #91 passed fresh Python 3.12/3.13/web/security
  CI before merge; this feature still requires its own fresh exact-head CI.
- The new Python browser helper is separately checked: disposable DocumentStore
  preflight, independent review and four mocked ownership/cleanup probes, then
  actual two-runner acceptance. Its SHA-256 is
  `1f872f235339d3bd077425e7d5cfc78d87e9390f1a1b46ae99f80fce47649d24`.

The renderer stores safely escaped original formula source before display repair
or macro expansion. The handler clones the selected range, expands partial math
endpoints in that clone and substitutes original source in its plain-text
projection. It preserves the live selection and rendered HTML clipboard flavor.
Mixed prose keeps block boundaries; tables use tabs between cells and newlines
between rows. Hidden repair text and closed details do not become visible prose.
Ordinary editors, code and diagnostic-only selections retain browser handling.

## Actual browser acceptance

Root ran the production build in the actual workspace aiohttp application with
marked temporary project/document state. A synthetic loopback repair peer
returned a repaired `x^2` for broken `\frac{1` and an unusable answer for the
matrix fallback. Exactly two synthetic requests occurred. No real model, worker,
VM, connector or research code ran.

Selections used real browser DOM Ranges set through developer CDP. Root then
pressed native **Ctrl+C**, cleared the actual unsaved Markdown source editor,
pressed **Ctrl+V**, and compared both clipboard plain text and the visible pasted
value. No fabricated copy event or supplied clipboard payload was used.

The cases exercised valid inline/display formulas, heuristic/model repairs,
Unicode fallback, skipped macro definitions, quoted display math, partial valid
and skipped formulas, mixed prose with two formulas, mixed rendering states,
table boundaries, ordinary inline/fenced code, a diagnostic label, source-editor
copy and document navigation. Every explicit range comparison was unchanged;
rich HTML remained present. A passive copy observer confirmed browser-default
handling for ordinary fenced code, diagnostics and source-editor copy.

Two initial inspection expectations were corrected without product changes:
the table selection includes existing outer DOM newline text nodes, and the
browser's ordinary fenced-code copy omits its terminal newline. The archived
cases retain these notes. Zero browser warning/error console entries occurred.

The [screenshot](copy-latex-browser-2026-10-12.jpg) shows the native pasted
original source above the rendered heuristic, model-repaired, approximated and
skipped views. The draft was discarded; the document remained at revision 1.

## Evidence and cleanup

[Evidence archive](workspace-copy-latex-evidence-2026-10-12.json): **40,290 bytes**,
SHA-256 `5f0514b7634172ea69bd0f37cb0f30f6846d307bb8e22c6d1a16411b083c1cb6`.
Seven exact UTF-8 records retain browser cases, final DOM, both gate logs,
independent review summary, cleanup and all **70** built-file hash pins. Each
record carries its byte count and SHA-256. The separate screenshot is 146,494
bytes, SHA-256 `1958ef05b66151b26cab51bb8eef0cd153a967e6f02588226c32715ee9bbbf68`.
Exact disposable login values were checked absent from exported evidence; the
private file and temporary database are excluded.

At **18:38:03.217625 UTC**, both application runners were closed, both owned
ports were free, the fixture directory and private info file were absent and
the transient unit was inactive. The owned browser tab had already closed.

This accepts the focused clipboard behavior in the exercised in-app browser.
It is not full Markdown round-trip serialization or proof across untested
browsers/platforms. No document revision, repair answer, dependency, backend
route, CSP or worker behavior changed. Fresh feature CI and merge remain the
delivery gates after this local acceptance.
