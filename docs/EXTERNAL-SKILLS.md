# External skills: platform requirements

Date: 2026-10-10. The user already uses [crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills) with CScience. Its content stays in that repository and is not embedded, vendored, or maintained in this project. This document records platform implications from a read-only review, not a new copy of its methods or prompts.

Inspected revision: `4089e99415f349dde406c8e57a227715002ee166`. The repository describes 162 skills, discoverable through names/descriptions with bodies and resources loaded on demand. The user can modify the external skills for this platform. Define clean platform contracts and update their host bindings; exact CScience API compatibility is not a requirement. Historical 0.1.27/0.1.53 observations identify failure scenarios to test, not constraints to reproduce.

## Required integration surfaces

| External pack needs | Platform implication |
| --- | --- |
| Stable canonical skill IDs, flat sibling skill directories, on-demand references/scripts | Register external packs by repository/path plus pinned revision; preserve names and relative resources; do not duplicate the catalog in platform code |
| Discovery and actual body loading across model/provider configurations | Record successful discovery/load events and effective skill revisions. Test the intended model alias/effort; a fluent report or a skill-name mention does not establish loading |
| Host plans, progress, delegated tracks, questions and approvals | Define a versioned platform API and update external host bindings. Preserve durable gate ownership and answer routing; exact legacy function names are optional |
| Structured child outcomes, artifact references and lineage | Validate output schemas, retain raw returned payloads and deviations, and associate delegation/idea/session IDs. Preserve original results before interpretation or curation |
| Project-shared knowledge and per-session append-only idea records | Expose stable project/session identity and scoped durable storage across containers. Support append/idempotency, concurrency, recoverable derived views and explicit write failures |
| Independent checks before accepting new derivations | Link the exact candidate, assumptions, checker evidence and promotion decision. A child proposal or default consistency review alone cannot promote a new theoretical result |
| Revisiting unfinished ideas without losing negative evidence | Keep resource/time/context limitations distinguishable from demonstrated obstructions; session termination does not resolve research questions |
| Literature, standards, citation graphs, source bodies and attack references | Discover capabilities and provider bindings dynamically; keep source locators, dates and blocked routes. The reference MCP URL is configuration, not a platform constant |
| PDFs and other binary research inputs | Preserve typed MCP content/resources and support authenticated scoped file transfer. Avoid flattening binary results into descriptions or forcing large base64 strings into model context |
| Sage/estimators, optional licensed algebra and theorem provers | Probe actual worker capabilities, provision isolated environments, pin toolchains and expose unavailable checks accurately. Do not equate a planned formalization with a checked proof |
| Portable methods with host-specific execution guidance | Keep scientific procedure in the external pack; update a small host binding for the new API. A legacy compatibility shim is optional if it simplifies the adaptation spike |

The initial cryptography profile can select the existing external pack, without recreating its methods here. The platform should also accept other science packs using the same discovery, resource, execution and evidence surfaces.

## Shared board compatibility

The pack already records a project board in per-session append-only JSONL ledgers and generates per-idea notebook views. It also keeps shared knowledge, gaps and lessons. A project-wide reduction combines session histories. This is an existing external workflow, distinct from Claude Science's disabled built-in dashboard.

The native board can use a new platform-owned schema/API, with the external skills updated to use it. Existing records may be imported/exported deliberately; preserving the exact JSONL layout is optional. Choose one authority and avoid a second independent board. Map stable project/session/idea/lemma IDs, event provenance, source/evidence references, critique links and raw outcomes. Preserve history and distinguish proposed state from accepted state.

Prefer project-scoped resource IDs and trusted append/publication APIs over hard-coded filesystem paths. Update the external skills accordingly. A filesystem compatibility mode is optional and needs deliberate concurrency, failure/retry and durability semantics. Existing single-machine append behavior does not establish safe concurrent writes over arbitrary network storage.

## Compatibility gates

1. Register a pinned external pack with updated platform bindings and discover/load a skill plus resources without copying content into the platform repository.
2. Capture load evidence under the intended main/delegate/reviewer model configurations; keep pack routing checks distinct from scientific-quality evaluation.
3. Execute a bundled helper inside the selected worker and publish a versioned artifact with correct lineage.
4. Preserve an original structured delegate outcome, including incomplete/blocked work, and associate it with the correct board entry.
5. View/update one project record from shared-container and dedicated-container workbooks. Exercise duplicate submission, concurrent updates, missing grants and interrupted writes.
6. Retrieve a PDF through a configured connector and open it with page anchors without losing binary content.
7. Change a pack revision deliberately; new runs use the new pin while earlier execution/review records retain their original skill identity.

These are proposed acceptance tests, not completed runtime checks. No skills were installed or executed during this study.

## Primary source references

- [Pack overview and portability boundary](https://github.com/regenscheid/crypto-analysis-skills/blob/4089e99415f349dde406c8e57a227715002ee166/README.md)
- [Project knowledge interface](https://github.com/regenscheid/crypto-analysis-skills/blob/4089e99415f349dde406c8e57a227715002ee166/skills/workbench-knowledge/SKILL.md)
- [Research board protocol](https://github.com/regenscheid/crypto-analysis-skills/blob/4089e99415f349dde406c8e57a227715002ee166/skills/workbench-knowledge/reference/research-board.md)
- [Host adapter and measured constraints](https://github.com/regenscheid/crypto-analysis-skills/blob/4089e99415f349dde406c8e57a227715002ee166/skills/investigate/reference/claude-science.md), [environment reference](https://github.com/regenscheid/crypto-analysis-skills/blob/4089e99415f349dde406c8e57a227715002ee166/skills/investigate/reference/environment.md)
- [Capability-based research tools](https://github.com/regenscheid/crypto-analysis-skills/blob/4089e99415f349dde406c8e57a227715002ee166/skills/investigate/reference/tools.md), [completion evidence](https://github.com/regenscheid/crypto-analysis-skills/blob/4089e99415f349dde406c8e57a227715002ee166/skills/investigate/reference/completion-contract.md)

Return to the [feature plan](FEATURE-PLAN.md) and [research workspace](RESEARCH-WORKSPACE.md).
