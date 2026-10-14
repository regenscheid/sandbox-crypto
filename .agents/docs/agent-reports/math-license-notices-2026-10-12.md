# Isolated math distribution notices — 2026-10-12

The isolated math build now ships complete checked notices for its actual
code-only KaTeX bundle. It reuses the GUI's pinned primary texts and exact local
adaptation statements. The renderer, launcher and two established license files
remain byte-identical to the baseline built before this change.

## Source and validation

- Independently reviewed signed source:
  `b19cfb2ba9e91f75cca2be820a035c14955ef821`, directly from merged main
  `05ba353daec9266f3f37d4c51fd269568580d54a` (PR #91).
- Source gates passed **486 web tests**, including **41 notice cases** (27 GUI
  and 14 math), **24 post-build math cases**, both production builds, locked npm
  installation with scripts disabled, lock/installed license checks and
  Svelte/TypeScript checks. Locked uv sync, Ruff formatting/lint, strict Pyright,
  Python licenses and generated protocol checks also passed.
- Actual-main union `fe19020d2fe2ee522fc7695007b7644b96e582d9` has parents
  `785fb191` and merged PR #92 main `b2f2e627`. Notice/build inputs remain exact
  `b19cfb2`; incoming GUI/helper source remains exact main. PR #92 passed all four
  fresh CI checks on signed `d547d500` (run `38292786944`).
- Fresh combined gates passed **515 web tests**, Svelte/TypeScript checks and the
  GUI production build. Every one of its **70 output files** matches both the
  root copy-accepted distribution and its archived `837962af` pins byte for byte.
  All six math files and their build/runtime/backend inputs are unchanged, so
  the completed source math build/24 cases and fast checks are explicitly reused
  rather than repeated after this union.
- Full Python **9,749 passed / 43 opt-in skips / two existing warnings** is
  explicitly reused, not rerun. Backend sources/tests, scripts, CI, Python/npm
  configuration and lockfiles, images/examples, math-source inputs and the
  launcher are byte-identical to main `05ba353` after the union. Incoming GUI
  sources match main `b2f2e627` and retain the copy slice's accepted bytes.
  The copied acceptance helper is inherited unchanged from independently
  reviewed PR #92, with its existing focused/CI evidence. The original full
  local run completed at signed `123b2e6`; fresh PR #90 and #91 CI passed in runs
  `38287920059` and `38290115991`. This slice changes build-time notice tooling,
  tests and metadata, not backend or renderer execution.

The final observed bundle has six source origins: KaTeX's checked
`dist/katex.mjs` and project `api.ts`, `macros.ts`, `mathcheck.ts`,
`mathoptions.ts`, and `mathrepair.ts`. It has no emitted static or dynamic
external imports, injected runtime helpers, Svelte runtime, CSS or external font
assets. The unchanged launcher imports `node:path` and `node:url`; Node remains
operator-provided and is not copied into this distribution.

## Applicable notices and exact coverage

The shared collector retains KaTeX's complete root MIT file, exact local
React-derived helper attribution with the full pinned primary Apache-2.0 text,
and exact Design Science/Khan glyph-derived SVG attribution with the complete
OFL body. OFL applies to those existing assets even without font files. It adds
no platform-code license allowance. Project MIT covers the project modules and
launcher. No second license collection or build-time network fetch is introduced.
[Primary provenance](../../../web/workspace/licenses/README.md) preserves the
limits of the adaptation comments: the pinned upstream license revisions do not
invent an exact original code lineage.

The complete directory contains six files. `license-manifest.json` records
all other files and their SHA-256; the established copier build entry point now
verifies exact filenames and bytes after Vite writes them.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `mathcheck.mjs` | 531,415 | `2091762bb88d332906540ef726015221fc839d4d4965652f6c53616cef03478f` |
| `check-math.mjs` | 1,794 | `623911cf4f65425523e07f56f80d86deda1e26aa629ce2c8ae1e363ee61e202b` |
| `LICENSE-KaTeX.txt` | 1,107 | `766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3` |
| `LICENSE-Sandbox-Science.txt` | 1,075 | `1cc1550e75ddedbceee3e3ae0e9fc0f1f3be047a833a1e442ec0f338afef7f85` |
| `THIRD-PARTY-NOTICES.txt` | 17,457 | `60e2a8b0edafbbd4ead2c64ee1bee2353e2e82b106e030e27f39f968387d10da` |
| `license-manifest.json` | 2,234 | `67eb2c2834c2ecff580d708577a2c577a680b221be0d109f1eddb672b8c42938` |

Actual-build tests verify deterministic builds, renderer bytes equal to the
same build without the notice plugin, exact launcher bytes, complete manifest
coverage, applicable Apache/OFL attribution and absence of inapplicable Svelte
notices. Negative cases refuse missing/changed primary or adaptation inputs,
changed/missing distribution bytes, uncovered files, incomplete manifests,
originless chunks and external imports. Independent read-only review also
compared default GUI output against the parent collector and verified all four
baseline files unchanged.

Root read-only acceptance at **18:45:26.486765 UTC** independently checked all
six emitted files, five nonmanifest hashes, full pinned Apache/OFL bodies,
unchanged baseline bytes and absent Svelte. Its first OFL containment assertion
mistakenly included the upstream template's example copyright header; inspection
was corrected to the complete license body already emitted by the reviewed
collector. No product, test-source, service or build change followed.

## Evidence and remaining delivery gates

Evidence archive (archived outside git, sha256 `bbe0cead6866`): **61,846 bytes**,
SHA-256 `bbe0cead68664f2c734df7ea92b26784e2a16e2f07ed4117183ad2d2649012ff`.
Its 28 exact UTF-8 records total 54,101 underlying bytes and retain the fresh
logs, full generated notices/license/manifest text, pre-change baseline, emitted
origins, complete file/input comparisons, root acceptance and final combined
GUI gates with every accepted-file hash. Renderer/launcher runtime hashes
are retained; their full generated JavaScript is not duplicated in the archive.

No real model, VM, listener, deployment or research execution ran. The existing
post-build Node checker cases ran locally. Actual main is integrated and combined
checks pass; exact combined review and fresh exact-head PR CI/merge remain required. No renderer, dependency,
platform-code allowlist or security/deployment behavior changed.
