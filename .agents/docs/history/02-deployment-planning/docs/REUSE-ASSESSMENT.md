# Source and reuse assessment

Date: 2026-10-10. This is source inspection, not a successful build, container test, or completed port. The user's clarified priority is to preserve CScience behavior while improving execution isolation and deployment.

## Sources inspected

1. Local CScience: `~/Git_Projects/cscience-crypto`, branch `dev`, HEAD `063919ffab886e6eb70f12dfda8168fec44d9d1a`, package `0.8.0`, pinned upstream `0.1.60`. Checkout clean when inspected.
2. Previously extracted Linux runtime/cache for upstream `0.1.60`, distribution metadata CScience `0.7.0`. Read-only inspection established existing remote-compute machinery; it does not validate the current build or a replacement worker backend.
3. User's private [regenscheid/open-science-crypto](https://github.com/regenscheid/open-science-crypto), `main` HEAD `a7e39ea09cac2b9ef29e10e463de6d70ab82526b`, committed 2026-08-26, package `0.4.0`. Read repository metadata/tree, progress, execution modules, RFC, and selected history. The project has been located; no build or full upstream-base diff was performed.
4. Public [regenscheid/crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills), README and repository description. Relevant research workflow/knowledge content; not validated as an imported catalog.
5. Public Open Science Desktop, [ai4s-research/open-science](https://github.com/ai4s-research/open-science), `master` commit `aa7f27667c5f4fffdfa583066beed5214fc03e2a`, 2026-10-08. Selected browser, agent-adapter, provenance, and harness source inspected. This newer upstream is distinct from the user's older fork.

## CScience: functional baseline and first adaptation target

| Area | Value to preserve | Worker adaptation question |
| --- | --- | --- |
| Existing orchestration, skills, delegation, and reviews | User values the research behavior and work organization | Can tool execution move behind a worker interface without replacing the research loop? Document review triggers and context first |
| Shell, persistent Python/R, workspace handling, and P20-P31/P38-era fixes | Useful tool semantics and practical failure handling | Where do processes and filesystem calls need a remote executor/transport implementation? |
| Conda and managed environment provisioning | Automatic installation of needed tools | Can resolution, hooks, installs, environment mutations, and functional probes all run inside the selected worker? |
| SSH/BYOC compute machinery in the cached pinned runtime | Existing submission, results, transfer, and provenance concepts | Can this be extended to ordinary local tool/kernel paths rather than remaining optional remote jobs? |
| `pkg/platform/lib/profiles.mjs`, capabilities and algebra probes | Crypto/lattice/Sage requirements and diagnostic fixtures | Move executable capability checks into the actual worker; host visibility is insufficient |
| Math rendering/highlighting and regression cases | Preserve mathematical presentation | Keep the current GUI initially if feasible; extract utilities only if a UI change becomes necessary |
| Gateway/model role settings and managed runtime guidance | Main/delegated/reviewer routing and tool advice | Preserve provider compatibility and advertise actual worker capabilities |
| `src/patches.mjs` and extraction/packaging | Current mechanism for adapting a pinned runtime | Measure added patch surface, release sensitivity, and maintainability before choosing a new harness |

The runtime is a pinned upstream distribution with compatibility patches, not an independently owned orchestration source tree. Direct adaptation feasibility remains open. Preserve behavior as the acceptance criterion even if some internals later need replacement. No runtime binary, extracted skill assets, private state, or credentials have been copied into this planning folder.

## Located OpenScience Desktop fork

The fork contains substantial Rust execution work and versioned execution schemas. The tree has 35 files under its execution module; this count describes the inspected tree, not 35 independently verified user-authored changes.

| Candidate | Reuse value | Limits / next validation |
| --- | --- | --- |
| Execution-spec, event, capability, permission-receipt schemas | Scoped request/result contracts and approval evidence | Adapt to persistent kernels, authenticated remote transport, and OpenShell capabilities |
| `staged_agent_execution.rs`, ownership journal/result store, transport and lifecycle modules | Reconciliation, ACK/replay, cleanup, staged files, and provenance patterns | Startup authority exists, but the inspected agent-serving route is not complete; public authority operations expose shutdown |
| OCI enrollment, image attestation, policy/broker modules | Isolation requirements and failure-case fixtures | OpenShell may replace much of the custom backend; avoid preserving two overlapping enforcement implementations |
| `docs/rfc/secure-workbook-execution.md` | Persistent per-project environments; package/network grants; proxy/approval workflow | Phase-two design proposal, not demonstrated implementation |
| Existing GUI and crypto/domain content | Possible future extraction | Native Tauri and older OpenCode coupling need targeted assessment if CScience UI/harness retention fails |

The inspected network implementation supports no-network and full/bridge modes; restricted filtered/proxy networking was explicitly unsupported in the relevant commit. The RFC proposes a richer persistent environment and policy model. Historical progress notes describe incomplete Linux parity and an arm64-focused verified image; none of this was retested here. Do not present the fork as a ready Linux/Kubernetes worker service.

Sources: [execution module](https://github.com/regenscheid/open-science-crypto/tree/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/apps/desktop/src-tauri/src/execution), [persistent environment RFC](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/docs/rfc/secure-workbook-execution.md), [progress](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/PROGRESS.md).

## Skills and later alternatives

The crypto-analysis-skills README describes investigation/assessment, discovery, validation, formalization, completion gates, and project/session knowledge records. Those conventions are relevant to the requested skills and automatic reviews. Curate and test them with actual worker execution instead of assuming catalog size establishes workflow quality.

Newer upstream Open Science Desktop has browser/headless and agent-runtime surfaces worth retaining as alternatives. However, its browser provenance path returns early outside Tauri, and its web transport assumes same-origin API configuration with persistent browser token handling. A working browser chat alone is insufficient evidence of durable research runs or independently hosted GUI support. [Provenance source](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/provenance.ts), [web transport](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/webMode.ts).

Finish a focused base diff before assigning extraction costs. Check the applicable source terms before copying code or assets. Session/database/credential migration is outside this planning pass.
