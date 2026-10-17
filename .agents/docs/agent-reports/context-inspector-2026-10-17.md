# Context inspector design — 2026-10-17

Scope: a Local Codex prototype of the owner's per-agent context-management
screen. The [specification](../../../docs/specs/context-inspector.md) is committed;
production UI, storage and APIs are unchanged. All preview records are synthetic.

## Result

The session and selected-agent secondary menus open a large embedded dialog.
Agent and run-attempt selectors keep the orchestrator, children and continuations
explicit. Usage shows reported input, separate working-view estimates, limits,
fold/summarization events and cached-input share with measurement coverage.
Current context shows checkpoint composition and nested folds, with links to
their original message spans. Original history searches before paging, supports
filters and explicit same-agent continuation searches, and separates original
model-visible tool text from separately retained full output.

The existing workspace remains mounted behind the dialog. Escape restores focus
without closing the document pane. Missing usage and opaque payloads are labeled
unavailable. The prototype uses a limited safe formatted-text subset; production
must connect the existing Markdown/math renderer.

## Validation and limits

Root browser checks in the in-app browser at 1280 × 720 verified the session
entry, separate child scope, graph and event presentation, a nested summary's
link to originals `o1-003` through `o1-008`, search reaching later-page messages,
and explicit continuation search reaching `o2-008`. Cache displays 32,000 /
46,000 = 69.6% with 3 / 6 known main calls; the source-review child displays
unavailable with 0 / 2 coverage. Escape returned focus to session actions and
preserved the open notebook and 640 px pane. The final Usage screenshot was
visually inspected. New and existing inline JavaScript pass Node syntax checks.

The design generator separately reported 18 inspector harness checks, 34
workspace regression checks, Brave interactions, focus-loop repair and
responsive checks from 360 to 1,920 px. These are generator-reported evidence,
not root browser coverage. An independent GPT-6.1-Sol source review found no
actionable medium-or-higher findings. No production tests, model calls, research
runs or new dependencies were needed for this design-only change.

Production work remains: durable context samples beyond the current first-100
estimate meter, explicit cache counters and provider normalization provenance,
authorized agent/attempt timeline and context projections, and full-archive
search independent of the research tool's retrieval-echo indexing exclusions.
Existing originals, fold records, usage journals, message reads and tool chunks
are building blocks; they do not constitute a complete inspector API.

## Evidence

Bundle `workspace-design-20261017`, outside git. The workspace entry requires
its context-inspector and existing pane-resize sibling assets. Ledger and shared
pane assets retain the hashes in the [previous report](pane-navigation-2026-10-17.md).

| File | SHA-256 |
| --- | --- |
| `context-final-workspace-redesign.html` | `ccfd08251498edbe94bad940b736aa1cf072a8e7c26e29b57b7d483b003da7b5` |
| `context-final-context-inspector.js` | `e9e2742dfedfab33a23b0a3e3336a68edf2cfb8506d33fa5c1e9f1e1e1ba865b` |
| `context-final-context-inspector.css` | `8193c374de9d44cf37f78bbc020ce78a0b52841af874b0fe0f98bebf29ce01fe` |
| `context-inspector-design-notes.md` | `aa5a0ed9490b57d9f6791823bec3d0f577220fd135875bcbbce69499891fbb55` |
| `context-inspector-usage.png` | `87440f37882d0369619dbb3af58169eece759e28875551954958b7d955f72798` |
| `context-browser-checks.json` | `3262016295a3977d19a23041ebc95c77d776d1ee5d847f6b5241c0d1d5d5bd56` |
| `context-workspace-final.json` | `609adf3ffcf9c2d454e986d793ab59ba3cdf922fe18b6dbd7f5491057d1864de` |
| `context-run-status.json` | `e8ad1c4190bf763f1f7d62038269d9fe26b8229cf4ab495bbc0d26cfd335745a` |
