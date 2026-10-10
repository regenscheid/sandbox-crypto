# Working decisions

Date: 2026-10-10. Confirmed requirements come from the user. Recommendations remain subject to feasibility checks.

| ID | Status | Decision | Reason / next evidence |
| --- | --- | --- | --- |
| D01 | Confirmed | Cryptography/math first, extensible to other sciences | Initial research domain |
| D02 | Confirmed | Web GUI, orchestrator/harness, and compute workers independently placeable in local or remote containers | Includes a single local machine and partial/full Kubernetes hosting |
| D03 | Confirmed | CScience is the functional baseline | The user is satisfied with its research functionality; execution architecture is the central problem |
| D04 | Confirmed | Preserve useful built-in skills and automatic reviews | User specifically values work organization and review |
| D05 | Evaluate first | Adapt CScience execution hooks while retaining the research loop | Trace local shell, kernels, provisioning, files, and existing compute-provider interfaces |
| D06 | Evaluate first | OpenShell v0.1.3 behind a worker adapter | Validate lifecycle, persistent storage, policy, and Kubernetes behavior |
| D07 | Confirmed | Normally one persistent container per project, provisioned from a base image | User specified project ownership and persistence |
| D08 | Recommended | Worker-initiated authenticated control transport | Supports local workers behind NAT with hosted orchestration |
| D09 | Recommended | Pinned base images plus recorded project dependencies and environment snapshots | Allow exploratory installs while making reproductions explicit |
| D10 | Recommended | Authoritative execution/review evidence outside the browser | Survives closed UI and records interrupted or failed work |
| D11 | Open | Retained runtime versus a compatible independent harness | Decide after measuring patchability and functional parity; a rewrite is not the default |
| D12 | Open | Persistence implementation and project file synchronization | Preserve existing state in an adaptation spike; do not assume a database migration is necessary |
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
| D38 | Confirmed | Initial deployments support hosted Kubernetes and local containers; local Kubernetes is acceptable | Local targets include Docker Desktop Kubernetes on macOS or a local Kubernetes environment on Babbage Linux; direct Docker/Podman is not required for the first local path |
| D39 | Confirmed | Local execution is not limited to Kubernetes | Docker or another container runtime is acceptable if execution and network isolation policies can be enforced; backend choice follows evidence |
| D40 | Recommended | Share resource identity, artifacts/revisions, typed anchors, durable event delivery and scoped decisions across research features | Avoid competing histories, queues and evidence formats; preserve domain-specific schemas and lifecycles |
| D41 | Recommended | Use common execution/environment services and kernel adapters for research, provisioning, PDF/TeX work and optional reproduction | Retain the existing Python protocol initially; evaluate native Sage's Jupyter kernel; new dedicated environments and restores remain distinct |
| D42 | Recommended | Start with logical control modules rather than a separate service/database for every feature | Independently package GUI, orchestration and trusted worker/policy infrastructure; retain the supported state store during adaptation |

## Decisions deliberately deferred

Do not select a new model framework, UI framework, database, or broad science catalog before the CScience execution feasibility spike. If a new implementation proves necessary, use the smallest stack that preserves the valued behavior and worker contract.

Provider/authentication compatibility with the retained runtime must be checked in that spike. Separate role settings for main agents, delegated agents, and reviewers should remain usable.

## Discussion order

The [consistency review](CONSISTENCY-REVIEW.md) and [detailed implementation plan](IMPLEMENTATION-PLAN.md) provide the next engineering steps. Start with execution tracing and the shell/Python worker slice; the remaining topics can be refined against that evidence rather than requiring another broad planning round.

1. Select a representative cryptography paper/task for the PDF → Sage → document → review workflow.
2. Define result/issue/frontier categories and how assumption changes flag dependent work.
3. Validate idle environment preservation/restoration and conflict-aware shared writes; select implementation details using those requirements.
4. Configure concrete install/network/monitor rules and permission scopes; the supported monitor actions are confirmed.
5. Evaluate local runtime candidates and the hosted Kubernetes profile against the same execution/network policy contract. Map current Science review configuration to durable events; separate reproduction remains an extension.

The earlier project no longer needs locating. Its implementation status and extraction costs still need validation.
