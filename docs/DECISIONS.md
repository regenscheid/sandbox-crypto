# Working decisions

Date: 2026-10-10. Confirmed requirements come from the user. Recommendations remain subject to feasibility checks.

| ID | Status | Decision | Reason / next evidence |
| --- | --- | --- | --- |
| D01 | Confirmed | Cryptography/math first, extensible to other sciences | Initial research domain |
| D02 | Confirmed | Web GUI, orchestrator/harness, and compute workers independently placeable in local or remote containers | Includes a single local machine and partial/full Kubernetes hosting |
| D03 | Confirmed (amended 2026-10-10) | CScience is the **behavior** reference, not a code base | The user values its research functionality. The needed behavior is reimplemented; no Claude Science code, prompts, skill text, schemas or assets are used (D11, D56) |
| D04 | Confirmed | Preserve useful built-in skills and automatic reviews | User specifically values work organization and review |
| D05 | Superseded by D11 | ~~Adapt CScience execution hooks while retaining the research loop~~ | The user decided to reimplement rather than patch the proprietary runtime |
| D06 | Evaluate first | OpenShell v0.1.3 behind a worker adapter | Validate lifecycle, persistent storage, policy, and Kubernetes behavior |
| D07 | Confirmed | Normally one persistent container per project, provisioned from a base image | User specified project ownership and persistence |
| D08 | Recommended | Worker-initiated authenticated control transport | Supports local workers behind NAT with hosted orchestration |
| D09 | Recommended | Pinned base images plus recorded project dependencies and environment snapshots | Allow exploratory installs while making reproductions explicit |
| D10 | Recommended | Authoritative execution/review evidence outside the browser | Survives closed UI and records interrupted or failed work |
| D11 | Confirmed | Implement an independent harness; do not retain or patch the Claude Science runtime | User decision (2026-10-10). Build from MIT-compatible components (D55); OpenScience Desktop upstream (MIT) is the first reuse candidate |
| D12 | Open | Persistence implementation and project file synchronization for the new control application | No retained store constrains it. Start with a single-node embedded store (SQLite, public domain) behind a repository interface; move to PostgreSQL only when multi-replica control is needed |
| D13 | Located, assessment partial | Prior fork: regenscheid/open-science-crypto, main a7e39ea | Contracts/lifecycle/RFC are relevant; base diff and end-to-end runtime remain to validate |
| D14 | Working name | sandbox-science | Folder name, not final branding |
| D15 | Recommended | Single-user/self-hosted initial deployment; redeployment/reproduction before live memory migration | Matches the immediate use case |
| D16 | Confirmed | Preserve automatic Conda tool installation inside isolated worker environments | The convenience is valuable and increases the importance of isolation |
| D17 | Confirmed | Monitor incoming/outgoing behavior beyond basic network reachability policy | Trusted external monitoring with task/execution context |
| D18 | Confirmed | Monitor intervention is policy-based: block, escalate for permission, or alert while continuing | Effective rules select the action; preserve rule/reason, context, decision and operation state |
| D19 | Confirmed | Sessions are workbooks; use the project container by default and optionally receive a separate container | Preserve workbook identity independently of container binding |
| D20 | Confirmed | Sessions can share data within their project in either container mode | Project data lifetime/access is separate from kernel/container state |
| D21 | Recommended | Separate kernels per session; dedicated session containers have independent dependency state | Avoid variable collisions and shared mutable environment surprises |
| D22 | Confirmed | Newly created dedicated session containers start from the base image | Use the recorded base-image revision; project data sharing is separate from inherited tool/process state |
| D23 | Confirmed | Living documents support direct user edits, comments, and agent revisions | User requested the collaborative document workflow; protect concurrent edits and retain attribution |
| D24 | Confirmed | Project-wide shared research board/ledger | Shared claims, evidence, tasks and history must span sessions and containers |
| D25 | Confirmed | Preserve LaTeX in the GUI and outputs | Equations/matrices remain editable source across documents, comments, workbooks and exports |
| D26 | Confirmed | First-class SageMath and PDF parsing/navigation | Integral to most initial projects; early functional gates rather than late science extensions |
| D27 | Recommended | Separate consistency review, mathematical reproduction review, and action/security monitoring | Inspected Science reviewer is evidence-consistency checking, not independent computation or network inspection |
| D28 | Recommended | Revision-checked document patches and immutable evidence references | Improve warning-only stale saves; superseded findings are distinct from confirmed fixes |
| D29 | Recommended | Curated versioned skills and explicit evidence types | Pin effective procedures; synthetic illustrations cannot satisfy empirical or proof gates |
| D30 | Confirmed | crypto-analysis-skills remains external; do not embed its content | Already used with CScience; study its requirements rather than duplicate its catalog |
| D31 | Recommended | External pack registration and a versioned platform contract | Preserve actual load traces, structured outcomes and one authoritative project history; adapters are implementation choices |
| D32 | Confirmed | External skills can be modified for the new platform | Prefer clean APIs and portable project resource IDs; exact CScience functions/file layouts need not constrain the design |
| D33 | Confirmed | Frontier means the best known results of each type, from published research and project investigation | Retain category, assumptions, comparison measures, origin and superseded history |
| D34 | Confirmed | Published results do not require independent checking | Record source and scope directly; project-generated results retain separate evidence/review policy |
| D35 | Confirmed | Automatic reviews should behave similarly to current Claude Science | Preserve checkpoint/terminal scheduling, reviewer scope, findings and response/confirmation flow; reproduction is a separate extension |
| D36 | Confirmed | Preserve idle project/session containers or enough captured state to recreate their environments | Retain files, installed tools, configuration and durable research records; verify restore before discarding the only environment copy |
| D37 | Confirmed | Session working directories plus shared project storage, deliberate shared writes and conflict detection | User accepted the proposed cross-session file model |
| D38 | Confirmed | Initial deployments support hosted Kubernetes and local containers; local Kubernetes is acceptable | Local targets include Docker Desktop Kubernetes on macOS or a local Kubernetes environment on babbage (Ubuntu 26.04); direct Docker/Podman is not required for the first local path |
| D39 | Confirmed | Local execution is not limited to Kubernetes | Docker or another container runtime is acceptable if execution and network isolation policies can be enforced; backend choice follows evidence |
| D40 | Recommended | Share resource identity, artifacts/revisions, typed anchors, durable event delivery and scoped decisions across research features | Avoid competing histories, queues and evidence formats; preserve domain-specific schemas and lifecycles |
| D41 | Recommended (amended) | Use common execution/environment services and kernel adapters for research, provisioning, PDF/TeX work and optional reproduction | With no retained Python protocol to preserve, use the Jupyter messaging protocol (jupyter_client/ipykernel, BSD-3) for Python, R and Sage alike; kernel sockets stay private to the worker. New dedicated environments and restores remain distinct |
| D42 | Recommended (amended) | Start with logical control modules rather than a separate service/database for every feature | Independently package GUI, orchestration and trusted worker/policy infrastructure; one store for the control application initially (D12) |
| D43 | Proposed (review, revised) | Adopt the [threat model](THREAT-MODEL.md), whose primary concern is **agentic workers overreaching** (acting beyond their task, scope or grants); record the isolation tier of every execution; default tier 3 (microVM) for hosted, tier 2 (gVisor or separate worker VM) for local, tier 1 only with explicit acceptance | User clarification: prompt injection from papers is a lesser concern. Downloaded and agent-written code still runs, so a shared kernel with trusted services remains the weakest point |
| D44 | Proposed (review) | Egress architecture before inspection: no direct internet from sandboxes; package installs through an allowlisted pull-through mirror; code/paper downloads through a read-only fetch proxy (git `upload-pack` only); proxy-side DNS; non-allowlisted destinations escalate and are never alert-only by default | Content inspection cannot separate key material from legitimate crypto output; reachability design is enforceable and auditable |
| D45 | Confirmed | Prefer permissive licenses; aim to publish under MIT; avoid AGPL; use GPL only where it cannot affect the project's license | User requirement (2026-10-10). Policy tiers in [licensing](LICENSING.md) |
| D46 | Proposed (review) | Ship a thin, permissive-only base worker image; install GPL research tools (Sage, fpylll, G6K, PARI) at runtime from conda-forge into the environment volume; platform code talks to them only over process/Jupyter protocol boundaries | User note: many research dependencies can be installed at runtime. Keeps the project MIT-clean while preserving D16/D26 |
| D47 | Proposed (review) | Treat the orchestrator as semi-trusted: egress only to the model gateway and policy-governed MCP endpoints; its MCP and fetch calls follow the same policy as workers; no engine/cluster credentials; evaluate running it as an OpenShell sandbox with providers | It reads untrusted content and acts on model output; worker sandboxing does not cover connector-based exfiltration |
| D48 | Proposed (review) | Read-only root filesystem plus declared writable volumes (Conda prefix, home, project shared, session workspace, tmpfs); idle preservation = image digest + volumes + lockfile; provide rebuild-from-lockfile | Makes D36 verifiable and confines persistence of a malicious install hook; runtime system packages require a base-image change |
| D49 | Proposed (review) | Each pool has a locally configured policy ceiling that remote control can only narrow; local enrollment and ceiling changes need local user action | Hosted-control/local-compute otherwise gives the hosted service code execution on the user's machine under whatever policy it requests |
| D50 | Proposed (review) | Define data classes (public, project-private, embargoed, restricted dataset) that govern permitted model endpoints, pools and storage | Embargoed vulnerability findings and sensitive datasets need routing rules beyond worker sandboxing |
| D51 | Proposed (review, revised) | Write behavior specifications for the CScience features to reimplement, in the project's own words from observed behavior and public documentation; build worker, proxy, mirror, monitor and secrets infrastructure in parallel with the harness | Replaces the earlier go/no-go criteria for patching, which D11 made moot. The infrastructure track does not depend on harness details |
| D52 | Proposed (review) | Name a reference fallback backend (Kubernetes + gVisor/Kata RuntimeClass + default-deny network + Envoy egress + Tetragon/Falco; locally rootless Podman/Docker + `runsc`); the worker API must be satisfiable by OpenShell and this stack | OpenShell is v0.1.3 and pre-1.0; an adapter with one implementation inherits its assumptions; independent telemetry adds defense in depth |
| D53 | Proposed (review) | Stage placement certification (all-local → all-hosted → hosted-control/local-compute) and define a minimum useful release; other combinations remain design-compatible | Keeps D02 as a design constraint while making the first usable release achievable for one person |
| D54 | Proposed (review) | Render untrusted artifacts on a separate origin in sandboxed iframes with CSP; approvals need fresh authentication and are built from monitor-observed facts | The GUI renders untrusted content and is also the approval surface |
| D55 | Proposed (review) | Build the harness from MIT-compatible parts: Anthropic client SDKs (MIT) or a model gateway such as LiteLLM (MIT core) for model access, the MCP SDKs (MIT) for tools, and own code for skills, plans, delegation, review scheduling and findings. Evaluate OpenCode (MIT) and OpenScience Desktop (MIT) for reuse | The Claude Agent SDK is not MIT-clean: the Python wrapper is MIT but bundles the proprietary Claude Code CLI, and the TypeScript SDK is proprietary |
| D56 | Proposed (review) | Reimplementation hygiene: do not copy code, prompt text, skill text, SQL migrations or assets from the Claude Science bundle or Anthropic source-available skills; implement from the behavior specs (D51) and public documentation | The agent reports cite line anchors in the proprietary bundle; they are evidence of behavior, not material to transcribe |
| D57 | Proposed (review) | Credentials for workers come from OpenShell providers: placeholder values in the sandbox, real values resolved by the proxy only for bound endpoints, fail-closed on mismatch, gateway-managed refresh for short-lived tokens. Back the store with the gateway's encrypted database locally and `kubernetes-secrets` (or OpenBao via the vault driver, to verify) when hosted | User requirement: a secrets manager should limit direct access to API keys. HashiCorp Vault is BUSL-1.1, so prefer Kubernetes Secrets or OpenBao (MPL-2.0) |
| D58 | Proposed (review) | Route external services through MCP endpoints declared with `protocol: mcp` and per-project method/tool allowlists; attach their credentials as providers; prefer least-privilege, read-only tokens | User expects most external services to be MCP. A placeholder still lets the agent do anything the token permits at that endpoint, so tool-level rules and token scope carry the restriction |

## Decisions deliberately deferred

The harness is reimplemented (D11), so the model-access library, UI framework and store are now chosen in W0 rather than deferred. Use the smallest MIT-compatible stack that delivers the specified behavior and the worker contract (D55). Do not select a broad science catalog early. The worker, egress-proxy, mirror, monitor and secrets infrastructure (D51, D57) can proceed in parallel under the [licensing policy](LICENSING.md).

Separate model/role settings for main agents, delegated agents, and reviewers are part of the behavior spec.

## Discussion order

The [consistency review](CONSISTENCY-REVIEW.md) and [detailed implementation plan](IMPLEMENTATION-PLAN.md) provide the next engineering steps. Start with the behavior specification and the new-harness shell/Python worker slice; the remaining topics can be refined against that evidence rather than requiring another broad planning round.

0. Confirm or amend the review-proposed decisions D43–D58, the [threat model](THREAT-MODEL.md) and the [licensing policy](LICENSING.md).
1. Select a representative cryptography paper/task for the PDF → Sage → document → review workflow.
2. Define result/issue/frontier categories and how assumption changes flag dependent work.
3. Validate idle environment preservation/restoration and conflict-aware shared writes; select implementation details using those requirements.
4. Configure concrete install/network/monitor rules and permission scopes; the supported monitor actions are confirmed.
5. Evaluate local runtime candidates and the hosted Kubernetes profile against the same execution/network policy contract. Map current Science review configuration to durable events; separate reproduction remains an extension.

The earlier project no longer needs locating. Its implementation status and extraction costs still need validation; with the harness reimplemented, its MIT-licensed upstream becomes a more direct reuse source.
