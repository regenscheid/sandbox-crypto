<!-- SPDX-License-Identifier: MIT -->
# GUI and isolated math distribution notices

`manifest.json` pins the package versions, declared terms and SHA-256 of the
complete primary notice files read from the locked local npm installation. It
also pins the unmodified KaTeX CSS and all sixty distributed font files. The
production build determines which packages actually appear in emitted chunks,
including Vite and Rolldown helpers, and emits their complete notices. It refuses
unknown origins/assets and missing or changed inputs. There is no build-time
network fetch. Vite's complete upstream license file includes notices for its
own build dependencies; copying it intentionally includes those extra notices.

KaTeX 0.18.9's package root is MIT, but its distributed fonts' TTF name records
explicitly name SIL OFL 1.1, the copyright holders and reserved font names. The
build extracts those exact statements from the hash-checked TTF counterparts of
the emitted fonts, and appends the complete OFL license body. It does not modify
or rename the fonts. The generic example copyright header in the official OFL
download is excluded from the emitted text because it does not describe these
fonts; the upstream download itself is retained byte for byte here.

KaTeX's `src/utils.ts` explicitly attributes its `hyphenate`/`escape` helpers to
Facebook's React under Apache 2. Its `src/stretchy.ts` identifies embedded
glyph-derived SVG assets under OFL, including Design Science/Khan copyright.
Those exact source statements and the prebundled `dist/katex.mjs` are hash-pinned;
the output retains both attributions, full Apache text and full OFL body even
without external font assets. Other KaTeX runtime entry points require separate
notice review. Svelte's emitted `src/internal/shared/utils.js` likewise attributes
its `is_promise` adaptation to `then/is-promise` under MIT; its exact statement,
source hash and complete upstream copyright/license notice accompany that helper.

Additional primary notices, retained byte for byte:

- `React-Apache-2.0.txt`: React's Apache text at
  [commit `e9e6b9b`](https://github.com/facebook/react/blob/e9e6b9b9b7558f1bc972f5cfb7b396d396a5508f/LICENSE)
  (the v0.3.0 tag), 11,357 bytes, SHA-256
  `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`.
- `is-promise-MIT.txt`: the source linked by Svelte's attribution, at
  [commit `ec9bd8a3`](https://github.com/then/is-promise/blob/ec9bd8a3f576324a1343069a2d6c2fa35a623939/LICENSE),
  1,058 bytes, SHA-256
  `44191656d296391e0ec97e32f5385f0d02b6f2992694082d22ea04ba0f66f9e4`.

Both were retrieved on 2026-10-12; URLs/dates/hashes are in the manifest. The
adaptation comments do not identify the original upstream revisions, so these
pins establish the primary license text, not an invented exact code lineage.

`OFL-1.1.txt` was downloaded on 2026-10-12 from the license maintainer's
[official plaintext](https://openfontlicense.org/documents/OFL.txt), linked by its
[official text page](https://openfontlicense.org/open-font-license-official-text/).
It is 4,599 bytes, SHA-256
`1d361a8f8e8ce6e68457dcd93fb56e162e6baa3bbb7e7573a290d44399f6b57e`.
The source URL/date/hash are also in the manifest. OFL records the existing
font/glyph-derived SVG asset terms; it adds no platform code dependency or
license allowance. Apache 2 and MIT are already allowed for platform code.

The PDF viewer's build (`vite.viewer.config.ts`, the `viewer` distribution) ships
PDF.js with the complete notices of the files it copies: PDF.js's Apache license,
the character maps' and Foxit fonts' BSD licenses, and the JBIG2 and OpenJPEG
licenses with their PDF.js build notices. `pdfjsAssets` in the manifest lists, by
SHA-256, every PDF.js file that may ship: the packed character maps, the Foxit
fonts, the two image decoders' JavaScript fallbacks and the worker. The build
fails when one is missing or modified, or when any other PDF.js file appears; the
Liberation fonts (GPL-2.0 with exceptions), QuickJS, the scripting sandbox and
WebAssembly are refused by name. Bundled PDF.js code is only `build/pdf.mjs`, its
core API, pinned by SHA-256 in `pdfjsRuntime`, and the worker's `?url` import: an
import of the scripting sandbox, the viewer components or the legacy build fails
the build. The GUI's own build refuses PDF.js entirely.

When updating npm packages, review their primary notice files and any bundled
components before updating these pins. Review any new stylesheet, copied asset,
virtual runtime helper or public-directory content explicitly.

The isolated `dist-math` build uses the same collector and primary inputs, with
code-only KaTeX attribution: root MIT, React-derived Apache helpers and the
OFL glyph-derived SVG assets. It ships no external font/CSS files or Svelte
runtime. Its six files are `mathcheck.mjs`, the unchanged `check-math.mjs`,
`LICENSE-KaTeX.txt`, `LICENSE-Sandbox-Science.txt`, `THIRD-PARTY-NOTICES.txt`
and `license-manifest.json`. The manifest hashes every other distributed file;
the established `copy-math-licenses.mjs` build entry point now verifies exact
file coverage and bytes after emission. Missing/changed inputs or uncovered
files fail the build. The checked launcher hash pins its existing external
`node:path`/`node:url` imports; Node itself is operator-provided and is not copied.
The renderer and launcher runtime behavior is unchanged. The GUI default
collector output stays unchanged; no second license-text collection is introduced.

The figure checker's build (`vite.figurecheck.config.ts`, the `figure-check`
distribution) writes `dist-math/figures`: `figurecheck.mjs`, the unchanged
`check-figures.mjs`, `LICENSE-Sandbox-Science.txt`, `THIRD-PARTY-NOTICES.txt` and
`license-manifest.json`, which names the distribution `figure-checker`. The bundled
packages are the figure frame's Vega, Vega-Lite and their dependencies, with the
notices the manifest pins; `figureLauncher` pins the launcher's bytes and its
`node:path`/`node:url` imports, and `copy-math-licenses.mjs` verifies this directory
as it does the math checker's.
