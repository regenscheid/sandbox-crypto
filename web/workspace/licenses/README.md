<!-- SPDX-License-Identifier: MIT -->
# GUI distribution notices

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

When updating npm packages, review their primary notice files and any bundled
components before updating these pins. Review any new stylesheet, copied asset,
virtual runtime helper or public-directory content explicitly. The separate
`dist-math` notice copier is unchanged: it currently copies only the root KaTeX
MIT and project MIT files. Complete embedded-component coverage for that separate
distribution remains a follow-on before it is distributed.
