# Research UI refinements — 2026-10-17

Source: `30bb89b9`, branch `codex/research-ui`. Signed local commits only; no push, merge or deployment. The primary checkout and personal research services are unchanged.

## Implemented

- Uploads accept 100 MiB per file, retaining immutable USER originals with 2 GiB/project and 8 GiB/store byte quotas. Schema 67 preserves prior records and their immutability triggers. Browser uploads use raw bodies with bounded metadata; legacy JSON uploads remain 8 MiB. Retry identities survive the 300-second browser deadline.
- Worker protocol 9, bounded larger write frames and the `shared/uploads/` exception carry the full original into other project environments. Ordinary read/reply bounds remain. Older surviving daemons without sufficient capacity refuse clearly while the original remains available for retry.
- Transfer admission bounds large allocations. Cancellation-safe offloads retain admission until their actual thread completes. A caller waiting behind a large executor write cannot close the healthy channel on its own timeout. Original downloads retain admission through socket completion and close a timed-out partial response.
- The research pane expands to half the browser window by pointer, keyboard or button. Documents has search, kind/scope/sort controls, compact rows and named closable tabs. Visited editors stay mounted, preserving drafts even when browser storage is unavailable. Upload filtering explicitly covers loaded pages.

## Validation

- **2,194 web tests pass**, across 112 files, including filters, keyboard tabs, drafts without storage, half-window resizing, uploads and retries. Svelte/TypeScript and formatting checks pass. Production app, PDF viewer and figure builds pass; existing bundle-size advice and happy-dom figure diagnostics remain.
- **336 focused Python tests passed** before the final cancellation hardening; **75 affected tests passed afterward**, including upload/storage/channel/shared-sync and the full-size local daemon case. Pyright, ruff, repository hygiene, licenses and generated-protocol checks pass. These are focused checks, not a new full-repository Python acceptance run.
- Authenticated browser on synthetic state: an actual file picker upload retained **104,857,600 bytes**. Reading back the retained bytes produced SHA-256 `bb051ecf3aff8116972febc166f66cbdbcc9cad139facc8bd7b30aadeb58bd9a`, identical to the input. The attachment displays **100.0 MiB** and was removed from the message draft without sending it.
- At a 1,280-pixel browser viewport, the research pane measures 640 pixels with no horizontal page overflow. Scope filtering, opening two document tabs and rendered math were inspected. Automated tests cover draft close/reopen without storage and resizing after viewport shrink.
- A local test transfers 100 MiB through worker HTTP → daemon bridge → executor → shared synchronization → a second environment. This does **not** qualify a live OpenShell deployment. No research model run was started.
- Independent GPT-6.1-Sol review cleared UI and upload changes. Findings fixed: unsupported deleted-document filtering, executor closure by waiting requests, and offloaded allocations outliving admission after cancellation.

## Ledger proposal

Local Codex used `frontend-design` and `impeccable-design-polish` to generate `ledger-redesign.html` and design notes in its Sandbox Crypto project. The proposal has 14 explicit synthetic entries, title-led list rows, search/type/status/review filters, an evidence/related/history inspector, and an optional board. Research status remains distinct from evidence standing. Existing entry kinds/statuses and access to issues/frontier are preserved in the design.

The generating run passed 18 model/interaction checks. A real browser additionally verified composed search/type filters, evidence tabs and claim/task boards, with no console errors or horizontal page overflow at 1,280 pixels. This remains a proposal: production ledger code, pagination, routes and live data are unchanged. Integration waits for owner feedback.

## Runtime and limits

The loopback synthetic preview remains running on port 29183 for the owner's ongoing UI review. The local design service remains available. These are review surfaces, not production deployment. No live OpenShell sandbox was created. UTF-8 document import remains limited to 1 MiB; automatic PDF extraction and upload deletion remain unimplemented. A timed-out request may retain its server admission slot until already-started storage/encoding finishes, preventing retries from accumulating large buffers.

## Evidence

Raw evidence and generated design files remain outside git on the local development host. Files are identified by basename and SHA-256; screenshots contain synthetic data.

| Evidence | File | SHA-256 |
| --- | --- | --- |
| Upload/backend suite before final cancellation fix | `larger-uploads-final2-tests.log` | `884badb719a407df447120198b5b55219e5042f8810a40ef8c833a33d2f2f296` |
| Final affected backend suite | `larger-uploads-drain-tests.log` | `1874604e71106d3ec4ff8ebdef9055abac40d794e7f15e6bed96cb909a6599af` |
| Final Python type check | `larger-uploads-drain-pyright.log` | `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332` |
| Combined web suite | `ui-refine-final-tests.log` | `4ebd57dcf170689ae0a2b8e2dd5af3509c79cc44746714071bc7e5b6d9dbecf9` |
| Svelte/TypeScript check | `ui-refine-final-check.log` | `7c216dfcffceae17f69ec2910460d114d677d4f33dd2f0ccd532740876b90f19` |
| Production build | `ui-refine-final-build.log` | `c65780c4e8b2aeee2214da7acaf745b5f258dc3adb310a35f30b16942335e1e3` |
| Browser checks | `refinement-browser-checks.json` | `1bef920e540a146d85b4980b4f5b6a8809141f8c9894bd50a7405e4b20141865` |
| Full-size retained upload | `upload-100MiB-stored.png` | `b4b13442b4880414990632c2570ef06f5d4864d087afcd6d00978a6616bfc915` |
| Document library and tabs | `documents-refined.png` | `3fb839d237129649a980eb01311f208bc6bf2e5a130e14f1e784b0c972caf6ce` |
| Ledger proposal | `ledger-proposal.png` | `8dc9e1a9364176625d2dc7feba05974d5c0971cb082fabb873f69f52b2fe416c` |
| Ledger prototype source | `ledger-redesign.html` | `74377c101bdeb5ba64a7a89c5262df969722397edc971756440aef1476065b7e` |
