# Research UI validation — 2026-10-17

Source: `7297336f`, branch `codex/research-ui`, based on foundation `631dc246`. Local signed commits only; no push, merge or deployment. The primary checkout and personal services are unchanged.

## Delivered

Project/session sidebar, central chat and optional resizable Documents/Plan/Agents/Activity pane. Embedded revisioned document editing, retained drafts, current-head notices, shared Markdown/KaTeX and parent/child activity access. File picker/drop uploads retain immutable USER originals, use bounded quotas and retry identities, and deliver copies through the existing scoped worker API before queuing attachment manifests. Explicit supported-text import creates a separate document; binary originals download without inline rendering. No new dependency.

OpenScience web UI (`synthetic-sciences/openscience`, Apache-2.0, `722ad051b7f601bab9e33749aedf942faf8c4737`) supplied interaction references. Its LICENSE/NOTICE and target files were inspected. Components here are independent Svelte implementations; no upstream source, prompts, skills or assets are copied. OpenDesign Cloud's project-list call was unavailable (transport closed); no design service operation was created.

## Checks

- **811 passed:** all workspace tests plus orchestrator control-store and run-state tests, one pytest invocation with four workers. Normal opt-in flags unset. Includes original byte preservation, USER provenance, scope/auth/origin denials, upload limits, invalid UTF-8 names, closed sessions, replay and changed-copy conflicts, lost write acknowledgements, and question/manifest budgets.
- **2,190 passed:** full web suite (111 files). New cases exercise retained composer drafts, review deep links, resize keys, mobile focus containment including simultaneous drawers, upload deadlines/retries/IDs, unload protection and Unicode manifest representation.
- Svelte/TypeScript: zero errors/warnings. Pyright: zero errors/warnings. Ruff format/lint, generated API types, repository hygiene and installed license checks pass. Production app, PDF viewer and figure builds succeed; existing large-bundle advisory remains. Existing happy-dom figure-frame diagnostics do not fail tests.
- Real authenticated browser against a disposable fixture: desktop chat/document inline and display math, retained unsaved Markdown across pane switches, agent cards, real file picker upload, attachment retention across sessions, safe no-worker error, and explicit Markdown document import. At 390 × 844: no horizontal overflow; navigation Shift-Tab wrapping, navigation-to-inspector focus transfer and Escape returning to Plan verified. Final reload had no new console errors; one earlier missing-CSS entry was caused by replacing the preview build while its old page remained open.
- GPT-6.1-Sol independent UI and upload-boundary reviewers cleared all medium-or-higher findings after fixes. No model or research run was started. Worker delivery was exercised through a protocol-speaking test fixture, **not a live OpenShell upload lab**.

## Limits and teardown

Up to eight attachments per message, 8 MiB per file; originals are quota bounded. Text document import is UTF-8 `.md`, `.markdown`, `.txt`, `.tex` up to 1 MiB. Automatic PDF extraction/import and upload deletion are not implemented. File bytes pending transfer survive internal navigation but not browser termination; the app warns while any queue is pending. Real OpenShell upload qualification remains before integration.

The local preview unit was stopped, its port 29183 verified closed, the browser tab closed and the viewport override reset. Synthetic state and evidence remain outside git for inspection; no personal runtime was started.

## Evidence

Raw files are outside git on the local development host, identified by basename and SHA-256 below. Screenshots show explicitly synthetic research content.

| Evidence | File | SHA-256 |
| --- | --- | --- |
| workspace/control/run-state tests | `ui-workspace-final.log` | `0d5e515e9d82483b00a9f2f25797b5588de8173acc8a7b10e926aac4ae5732cc` |
| web tests | `ui-tests.log` | `170526d69427a9a128efeb05d079e57a28d7897bbf3d54a1aa1ac6dd8f6b5f76` |
| Svelte/TypeScript check | `ui-check.log` | `7c216dfcffceae17f69ec2910460d114d677d4f33dd2f0ccd532740876b90f19` |
| Python type check | `ui-pyright.log` | `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332` |
| production build | `ui-build.log` | `0bbdd04ccc71e87a163ab2d600e7bb669a0d2933b1088378fa53668873d58b80` |
| browser checks and teardown | `browser-checks.json` | `5cae001692e339fe17a1d97274971ff2226b614ff02616e281eaf761f73876aa` |
| desktop preview | `desktop.png` | `d1ca7aadae7c4f1a04d998cb8bf8ac34610790a9e0e103cdf00908d961f78b1e` |
| mobile preview | `mobile.png` | `6461915a17c180df649e95a50b7d5551df4af46a491786acff4464c6e7d96bea` |
