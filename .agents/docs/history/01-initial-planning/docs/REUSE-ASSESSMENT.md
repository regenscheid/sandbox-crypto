# Reuse assessment

Date: 2026-10-10. This is source triage, not a claim that the candidates have been ported, licensed for redistribution, or validated in containers.

## Sources actually inspected

1. Local `/home/andy/Git_Projects/cscience-crypto`, branch `dev`, HEAD `063919ffab886e6eb70f12dfda8168fec44d9d1a`, package version `0.8.0`, pinned Claude Science runtime `0.1.60`. The checkout had no reported changes during inspection.
2. Public [ai4s-research/open-science](https://github.com/ai4s-research/open-science), default branch `master`, commit `aa7f27667c5f4fffdfa583066beed5214fc03e2a`, committed 2026-10-08. Read README, progress/design notes, repository tree, and selected adapter/web/provenance/harness files. No build or runtime verification.
3. The user's earlier project based on OpenScience Desktop: **location unknown; its own changes remain unassessed**. Searches of the visible Git_Projects, Codex document/project directories, and registered local projects did not identify it. This is not evidence that it has been lost or contains no reusable work.

## CScience candidates

| Source | Recommendation | Adaptation needed |
| --- | --- | --- |
| `pkg/platform/lib/profiles.mjs`; `test/unit/profiles.test.mjs` | Strong candidate: environment profile requirements, inventory, capability resolution, functional lattice smoke-test cases | Move inventory/probes into the actual worker sandbox; key evidence by image/platform/policy, replace mutable host Conda assumptions with locked images |
| `pkg/platform/lib/capabilities.mjs`; capability tests | Reuse capability vocabulary, tool diagnostics, real-path lessons, environment invocation needs | Host discovery is diagnostic evidence only. Do not mount host installations automatically or treat host visibility as sandbox readiness |
| `pkg/platform/lib/algebra-probe.mjs`; algebra probe tests | Reuse the small Sage/Magma/GP/GAP calculations as starting fixtures | Current probes intentionally run on the host. Run new checks through the execution adapter and revise timeout/resource expectations |
| `src/web/cscience-math.js`; math/Markdown tests | Candidate pure math delimiter/TeX helpers and regression inputs | Separate pure rendering utilities from DOM hooks and the `/api/cscience/math-repair` service; preserve original scientific text and expose any repair |
| `src/frontend-highlighting.mjs` | Candidate Sage/Magma grammar definitions | Extract grammar data; discard bundle traversal and AST injection stages |
| `pkg/platform/lib/gateway.mjs`, `model-classes.mjs`, `model-preferences.mjs` | Reuse provider catalog/role/context-budget lessons and test cases | Design protocol-neutral provider capability records instead of importing upstream registration fields or hard-coded model families |
| `SCIENTIFIC_RUNTIME_RELIABILITY_SURVEY.md` | Reuse failure taxonomy and evaluation scenarios | Historical survey is dated September 21 and predates current profile implementation. Do not copy its claims about missing functionality into current status |
| `src/patches.mjs`, runtime extraction/branding/install packaging | Preserve as reference, not the new core | Coupled to upstream bundles/releases and desktop installation layout; they do not provide independent scheduling or worker execution |

The most valuable salvage is reliable behavior and evaluation fixtures, not the compatibility distribution's topology. Independently check ownership and applicable licenses before importing code. No upstream Claude Science binary, extracted assets, or private runtime state has been copied into this project.

## Open Science Desktop candidates

Upstream now includes a browser/headless mode and a replaceable agent-runtime surface, so it deserves more than a blanket rewrite decision. Its README identifies Tauri/React, a bundled OpenCode runtime, remote compute, artifacts, and an MIT top-level license with separately licensed bundled components. These are upstream claims, not validation of the user's fork. [Pinned README](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/README.md)

| Source area | Reuse prospect | Required boundary change |
| --- | --- | --- |
| `apps/desktop/src/` viewers, conversation/pane components, file and run displays | Candidate UI modules, especially rendering/math/file presentation | Replace Tauri/absolute-path calls with API-backed project and artifact interfaces; serve browser assets independently |
| `packages/sdk/src/runtime.ts`, `base-runtime.ts`, `OpenCodeClient.ts`, `acp/` | Agent adapter contract and event-normalization reference | Rename provider-specific domain types, persist events server-side, enforce scoped tools and execution routing |
| `crates/osd-core/` runtime/project/gateway modules | Candidate endpoint/storage/process-supervision patterns | Audit host process execution, credential handling, filesystem scope, and coupled lifecycle assumptions |
| `apps/desktop/src/lib/provenance.ts`; Rust provenance/run modules | Candidate derivation rules, artifact history views, and schema reference | Make worker/orchestrator evidence authoritative and independent of a desktop/browser event consumer |
| `apps/desktop/src/lib/webMode.ts` | Useful evidence that UI has a browser transport path | Replace desktop-injected configuration and broad persistent bearer-token patterns with the new deployment/auth contract |
| `runtime/harness/`, `crates/osd-core/src/harness.rs` | Candidate project scaffolding and research conventions | `harness.rs` seeds workspace instructions; it is not itself the agent loop, scheduler, or sandbox execution engine |
| Skills and connectors | Curate relevant domain content and connector definitions | Check each license/dependency, run local processes in governed environments, broker network credentials and data access |
| Desktop installers, native automation, personal browser integration | Defer | These are optional product integrations after independent container deployment works |

Two concrete source observations matter:

- `recordProvenance()` returns when `!isTauri`, matching the README's headless provenance limitation. A successful browser conversation is insufficient proof of a recorded run. [Pinned implementation](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/provenance.ts)
- The browser transport derives its API origin from the page origin and stores a gateway token in localStorage. Serving that UI from a different host requires deliberate transport/auth refactoring. [Pinned implementation](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/webMode.ts)

The early technical design explicitly mixes implemented and planned functionality. Use code and the current progress log to establish status. Do not infer that “remote compute” means every local shell, notebook, agent, or plugin already runs in a container. [Technical design](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/docs/TECHNICAL_DESIGN.md)

## How to assess the earlier project once located

1. Identify its actual upstream/base commit, branch, dirty state, and remotes without resetting anything.
2. Compare source changes to that base, distinguishing user-authored modules from generated/bundled output.
3. Inventory crypto/math features, agent/provider adapters, skills, tests, data models, and deployment work.
4. Map each candidate to the new UI, control, harness, worker, or domain-profile boundary.
5. Measure a thin extraction: render a useful project/artifact view against a stub workbench API; run one adapter tool through OpenShell.
6. Choose selective extraction, an upstream fork with boundary refactoring, or a new core with reused UI based on that result. Keep costs and rejected assumptions in the decision log.

Do not migrate sessions, credentials, databases, host Conda environments, or personal browser profiles as part of source salvage. State migration is a separate, explicit task after compatibility and export requirements are known.

