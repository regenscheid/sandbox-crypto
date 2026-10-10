# Working decisions

Date: 2026-10-10. “Confirmed requirement” comes from the user's request; other choices remain revisable through the listed gates.

| ID | Status | Choice | Reason / revisit trigger |
| --- | --- | --- | --- |
| D01 | Confirmed requirement | Cryptography/math first, extensible to other sciences | User clarified the initial domain |
| D02 | Confirmed requirement | Independently place web UI, orchestrator/harness, and compute workers in local or remote containers | Central deployment requirement |
| D03 | Proposed | Browser UI plus owned control-plane API and versioned domain contracts | Avoid desktop lifecycle and backend API coupling |
| D04 | Evaluate first | OpenShell v0.1.3 as the first sandbox backend behind an adapter | Fits documented runtime/policy direction; adopt after M1 |
| D05 | Proposed | Trusted worker controller outside execution sandboxes; outbound worker transport | Allows hosted control with private local compute and narrow infrastructure privileges |
| D06 | Proposed | Immutable image profiles with sandbox-local functional capability checks | Prevent environment names/host inventory from becoming unsupported capability claims |
| D07 | Proposed | PostgreSQL metadata/events and a storage abstraction; modest database-backed job queue initially | Portable durable state without premature queue infrastructure; revisit on measured contention/load |
| D08 | Proposed | Authoritative run/provenance capture in workers and control plane | Execution history must survive closed browsers and headless operation |
| D09 | Proposed | Redeployment/reproduction first; explicit checkpoints for live state | OCI images do not carry process memory, host datasets, or credentials |
| D10 | Open | External sandboxed harness vs narrow owned harness | M3 requires proof of tool confinement, recovery, and provider compatibility |
| D11 | Open | Selective OpenScience UI extraction vs refactoring its core | Need the user's earlier project and a thin extraction experiment |
| D12 | Working name | `sandbox-science` | Neutral folder name; branding remains open |
| D13 | Proposed | Single-user/self-hosted deployment first | Establish correct boundaries before team/public hosting |

## Stack candidates

Start the execution spike in Python with the matching OpenShell SDK. React/TypeScript is a sensible UI candidate if useful OpenScience UI code is retained. FastAPI is a candidate for an owned Python control plane; retaining selected Rust services is also possible if the reuse assessment justifies it. These are implementation candidates, not a committed polyglot architecture. Select the smallest coherent stack after the execution and source-extraction spikes.

Keep provider protocols behind a harness/model adapter; model discovery must describe actual tool support, context/output limits, and reasoning options. The current CScience Anthropic-compatible routing should not become an unavoidable application-wide protocol. No provider subscription/OAuth flow is assumed transferable to a new application.

## Outstanding user-specific inputs

- Folder/repository for the earlier OpenScience Desktop effort, including the meaningful changes to preserve.
- First local/remote target machines and whether Docker, Podman, or Kubernetes is preferred there.
- First model gateway/provider and whether project/model context must stay local.
- Initial exact-algebra/lattice tasks and any licensed tool requirement.

These inputs refine implementation. The planning files and a deterministic runtime spike do not require choosing a final product name, UI theme, broad science catalog, or public hosting arrangement.

