# GUI distribution notices acceptance — 2026-10-12

The production GUI distribution now retains complete checked notices for its
actual bundled modules, runtime helpers and existing font/SVG assets. The actual
workspace HTTP server served all **70 distribution files** byte for byte,
including the notice text, project MIT license and file manifest. Its disposable
listener and state were completely removed.

## Source and validation

- Independently reviewed signed source:
  `1032cd4c640a830ade7e80747b3c81e0a5ba5f03`, with parents `6b2b6bb` and actual
  main `c6ccce61` (merged PR #90).
- Fresh validation passed **472 web tests**, including **27 notice cases**,
  **24 post-build math cases**, both production builds, locked npm installation
  with scripts disabled, lock/installed licenses and Svelte/TypeScript checks.
  Locked uv sync, Ruff formatting/lint, strict Pyright, Python licenses and
  generated protocol checks also passed.
- Full Python **9,749 passed / 43 opt-in skips / two existing warnings** is
  explicitly reused from signed `123b2e6`, where it completed in 327.71 seconds.
  All Python/runtime/test/configuration and math-checker inputs are unchanged.
  PR #90's fresh Python 3.12/3.13/web/security CI passed on `be360b7`, run
  `38287920059`. This slice did not repeat the broad local Python suite.
  Fresh exact-head PR CI remains required before merge.

Actual emitted module origins select ten package notices, including Vite and
Rolldown helpers. The build pins complete primary notice files, declared package
versions/terms, KaTeX CSS, sixty exact font files and project MIT. Missing,
changed or uncovered inputs fail the build. Actual-build tests verify two
byte-identical builds and complete emitted-file coverage; negative cases include
unknown modules/helpers/styles/assets, changed fonts, mismatched lock/install
metadata and public-directory copying.

Independent review found and verified corrections for embedded React-derived
KaTeX helpers and Svelte's `then/is-promise` helper. Exact local adaptation
statements accompany pinned full primary Apache/MIT text. KaTeX's glyph-derived
SVG assets retain their source attribution and full OFL body even in a code-only
build without external fonts. Font attribution preserves the distributed
copyright/reserved-name statements. [Pinned provenance](../../../web/workspace/licenses/README.md)
records the primary sources and limits of their code-lineage evidence.

## Distribution delivery and cleanup

The root check started the actual workspace aiohttp application on a disposable
loopback port with marked temporary state, no worker and no model. Every built
file returned HTTP 200 with exact on-disk bytes and the existing security headers.
The two license text files returned `text/plain`; the generated manifest covered
every other emitted file with its SHA-256. No new server route was needed.

The three new distribution files are:

| File | Bytes |
| --- | ---: |
| `THIRD-PARTY-NOTICES.txt` | 145,528 |
| `LICENSE-Sandbox-Science.txt` | 1,075 |
| `license-manifest.json` | 18,773 |

At **18:14:20.931549 UTC**, the application runner was closed, its port was free
and the marked temporary state directory was absent. This was HTTP distribution
acceptance; no browser, VM, real model or research code ran.

## Evidence and limits

Evidence archive (archived outside git, sha256 `52ea2e5fc7e5`): **208,015 bytes**,
SHA-256 `52ea2e5fc7e5a3b62c5cc1306e74e71c91403f83060f474a4e63e2b440cb7c2e`.
Its 21 exact UTF-8 records include the generated notice/license/manifest files,
all fresh gate logs, source-input comparison, independent review summary and
per-file HTTP results. Each record carries its length and SHA-256. The disposable
login value and temporary database are excluded.

This closes the GUI notice-shipping prerequisite. No dependency, platform-code
license allowance, runtime/API behavior or isolated math-check notice copier
changed. The latter still copies root KaTeX/project MIT notices; complete
embedded-component coverage for that separate distribution remains an explicit
backlog item. There is no automatic network fetch during the build; dependency
or asset updates require review of their pinned notice inputs.
