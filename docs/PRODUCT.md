# Product requirements

Date: 2026-10-10. This is a planning document. No new functionality has been implemented.

## Functional baseline

The user finds CScience functionally strong and wants better sandboxing and deployment flexibility. Preserve its research behavior, particularly the built-in skills that organize work and the idea of automatic reviews. Container placement should change where execution happens while keeping the research workflow familiar.

The initial domain is cryptography and mathematics. Typical work connects literature, assumptions, exact algebra, parameter analysis, computational experiments, and written conclusions. Other sciences should be supported through additional skills and environment profiles later.

## Projects and workbooks

Each project normally owns a persistent execution container provisioned from a base image. A session is a workbook with its own conversation, research state, reviews, files, and kernels. Sessions use the project container by default and may request a separate container. New dedicated session containers start from the base image and have independent tool/dependency state. Both modes can share same-project data.

Preserve idle project/session containers or enough state to restore their files, tools, configuration and durable research state. Each workbook has its own working directory plus shared project storage with deliberate writes and conflict detection. Closing the GUI must not stop active computations. Live kernel recovery remains best effort with explicit state-loss reporting.

Keep container/dependency state, session kernel state, and project data as explicit ownership boundaries. See [projects and sessions](PROJECTS-AND-SESSIONS.md).

## Shared research workspace

Each project owns a research board/ledger and living documents shared across its workbooks. Record questions, conjectures, assumptions, claims, experiments, counterexamples, decisions, and review findings with stable IDs, evidence links, history, and status. Keep proof, exact computation, bounded testing, numerical evidence, source assertions, and synthetic illustrations distinct.

The frontier tracks the best known results of each type from published research and the project's own investigation. Published results are recorded with their sources and scope without independent checking; project-generated results retain their separate evidence/review requirements. Preserve comparison conditions and superseded history. See [frontier semantics](RESEARCH-WORKSPACE.md#frontier-results-and-published-research).

The user can edit documents directly, leave anchored comments, and request agent revisions. Preserve attributed versions and reconcile concurrent edits using revision checks. Claims/reviews pin evidence revisions; presentation links may explicitly follow the latest version. An edited assumption should mark dependent results and reviews for reconsideration.

LaTeX must remain editable/copyable source throughout chat, workbooks, documents, comments, board entries, reviews, and exports. Native Sage kernels and integrated PDF parsing/navigation are integral to the cryptography/math profile. PDF references should open the exact document/page/region, and extraction should retain parser provenance and uncertainty. See [research workspace](RESEARCH-WORKSPACE.md) and [feature plan](FEATURE-PLAN.md).

## Skills and work organization

Skills are part of the research workflow, not merely a package list. Retain useful planning, literature/PDF exploration, computation, figure, and report conventions. Assess existing built-in and user-authored skills against the worker API so their commands, files, environment requests, and handoffs remain usable across deployment profiles.

The existing crypto-analysis-skills pack is already used with CScience and remains in its own repository. Register/load it externally and supply the capabilities its workflows need. The user can update its host bindings for a clean new platform API; exact legacy calls/file layouts are not required. Do not embed or maintain a duplicate catalog here. Its board/knowledge protocol, loading probes, evidence gates and capabilities inform [external skills](EXTERNAL-SKILLS.md).

Skill-driven execution uses the same worker boundary as direct tool execution. Capability guidance must describe the selected worker's actual environment, rather than tools installed beside the orchestrator.

## Automatic provisioning and monitoring

Preserve automatic Conda installation of needed tools, including project-specific dependencies. Run resolution, downloads, hooks, and builds inside governed worker environments; record environment changes and functional checks.

A trusted monitor outside the research workload should watch outgoing/incoming activity with task and execution context, beyond destination/port rules. Inspect supported requests/responses, record coverage gaps, and detect suspicious data movement or dependency behavior. Policy selects blocking, escalation for permission, or alerting while continuing. See [monitoring](MONITORING.md).

## Automatic reviews

Automatic reviews should behave similarly to current Claude Science. Preserve its automatic checkpoint and terminal review scheduling, constrained reviewer context, role settings, finding severity, author response and confirmation flow. Record actual active configuration separately from inspected source defaults, and drive equivalent triggers from durable worker/artifact events. A separate reproduction capability is an extension rather than a replacement schedule or mandatory computation in every automatic review.

Source inspection found CScience's built-in reviewer primarily checks consistency between claims, transcripts, logs, and artifacts, without executing computations. Preserve this constrained role and its checkpoint/terminal scheduling. Evaluate a separately configured mathematical verification/reproduction role as an extension. See the exact evidence in [source study](SOURCE-STUDY.md).

The proposed review record includes the claim or deliverable, evidence snapshot, review type, reviewer model/settings, findings, reproduction attempts where applicable, author response, and unresolved issues. Reviews should check assumptions and distinguish proof, exact calculation, finite testing, numerical evidence, and conjecture.

A verification reviewer can use a fresh sandbox to rerun an experiment without the producing kernel's hidden state. Review execution follows the same placement and project policy as other work. A failure to reproduce or unavailable environment is visible. Preserve claimed-fix confirmation and distinguish superseded findings from confirmed corrections. Repeated reviews need a bounded completion policy; reviewer agreement is not itself proof of a cryptographic claim.

## Execution compatibility

| Existing behavior to preserve | Container requirement |
| --- | --- |
| Shell and scientific tools | Run on selected workers with useful stdout/stderr and working-directory behavior |
| Persistent Python/R sessions | Maintain kernel identity and state across cells; report restart/state loss explicitly |
| Native Sage cells and exact mathematics | Preserve Sage preparsing, domains, exact results, matrix output, and LaTeX representations |
| PDF sources and anchored evidence | Parse/render in workers; retain page/region references and cached derivatives for GUI navigation |
| Environment/package management | Install into an authorized worker/project environment and record resulting dependencies |
| Project files and agent handoffs | Use scoped paths and deliberate synchronization across hosts |
| Figures, documents, and artifacts | Produce and collect them through workers, with lineage and bounded transfer |
| Main, delegated, and reviewer agents | Retain coordination and role-specific model settings; route execution consistently |
| Research MCP connectors | Preserve useful access with explicit network/credential scope and governed subprocess execution |

Exact algebra/Sage, lattice/fpylll, parameter estimators, and optional licensed Magma are early environment targets. Availability requires a functional check in the selected worker; a host installation alone is insufficient.

## Acceptance criteria

- Orchestration and research code execution run in separate containers.
- Ordinary research tool calls work against hosted Kubernetes and a local container pool in the initial deployments. Local Docker/other runtimes or Kubernetes on macOS/Docker Desktop or Babbage Linux are acceptable when execution and network policies are enforced.
- Interactive kernels, skills, file handoffs, and automatic reviews remain usable in both profiles.
- Projects retain their base-image-derived environment and installed tools; sessions can share the default container or use a dedicated one.
- Sessions in either mode can exchange same-project data without sharing live kernel variables.
- The web GUI can be hosted separately and closing it does not stop orchestrator work or lose run records.
- Hosted orchestration can use a local worker without publishing its container-engine API.
- Files, dependencies, and session state have explicit persistence and recovery rules.
- Idle environments are preserved or demonstrably restored with their files, installed tools and configuration; failed snapshots/restores do not silently discard state.
- Automatic consistency reviews preserve Science's configured triggers, scoped evidence, findings and confirmation lifecycle. If the proposed reproduction extension is enabled, it can rerun a small exact result in a fresh environment through the same worker API.
- The default cryptography profile executes genuine Sage syntax and exact finite-field/matrix work inside both local and cluster workers.
- PDF citations navigate to exact source revisions/pages/regions; mathematical extraction can be checked against rendered crops.
- Workbooks share a durable research board/ledger and living documents independently of their containers.
- User edits/comments survive agent revisions and concurrent-edit reconciliation; LaTeX source survives GUI rendering and exports.
- Worker loss, kernel restart, cancellation, partial output, and failed review are reported accurately.
- An unavailable sandbox never silently causes code execution in the orchestrator container.
- Moving services requires configuration/state transfer rather than changing research scripts.

## Later scope

Broader science coverage, new UI design, public multi-tenant service, GPUs, and live process migration follow evidence from the initial deployment and compatibility work. Kubernetes placement and persistent kernels are part of the initial design, not late optional extensions.
