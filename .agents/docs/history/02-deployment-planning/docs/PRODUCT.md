# Product requirements

Date: 2026-10-10. This is a planning document. No new functionality has been implemented.

## Functional baseline

The user finds CScience functionally strong and wants better sandboxing and deployment flexibility. Preserve its research behavior, particularly the built-in skills that organize work and the idea of automatic reviews. Container placement should change where execution happens while keeping the research workflow familiar.

The initial domain is cryptography and mathematics. Typical work connects literature, assumptions, exact algebra, parameter analysis, computational experiments, and written conclusions. Other sciences should be supported through additional skills and environment profiles later.

## Skills and work organization

Skills are part of the research workflow, not merely a package list. Retain useful planning, literature/PDF exploration, computation, figure, and report conventions. Assess existing built-in and user-authored skills against the worker API so their commands, files, environment requests, and handoffs remain usable across deployment profiles.

The existing crypto-analysis-skills repository supplies relevant investigation, validation, formalization, and knowledge-recording conventions. Its content is a reuse candidate; importing a large catalog is not a substitute for preserving how the orchestrator applies skills.

Skill-driven execution uses the same worker boundary as direct tool execution. Capability guidance must describe the selected worker's actual environment, rather than tools installed beside the orchestrator.

## Automatic provisioning and monitoring

Preserve automatic Conda installation of needed tools, including project-specific dependencies. Run resolution, downloads, hooks, and builds inside governed worker environments; record environment changes and functional checks.

A trusted monitor outside the research workload should watch outgoing/incoming activity with task and execution context, beyond destination/port rules. Inspect supported requests/responses, record coverage gaps, and detect suspicious data movement or dependency behavior. Its intervention policy is still being discussed. See [monitoring](MONITORING.md).

## Automatic reviews

Automatic review is a required part of the target research experience. First document and preserve the existing runtime's triggers, reviewer context, role settings, output handling, and revision behavior. Any changed review policy remains an explicit design decision.

The proposed review record includes the claim or deliverable, evidence snapshot, reviewer model/settings, findings, reproduction attempts, author response, and unresolved issues. Reviews should check assumptions and distinguish proof, exact calculation, finite testing, numerical evidence, and conjecture.

A reviewer can use a fresh sandbox to rerun an experiment without the producing kernel's hidden state. Review execution follows the same placement and project policy as other work. A failure to reproduce or unavailable environment is visible. Repeated reviews need a bounded completion policy; reviewer agreement is not itself proof of a cryptographic claim.

## Execution compatibility

| Existing behavior to preserve | Container requirement |
| --- | --- |
| Shell and scientific tools | Run on selected workers with useful stdout/stderr and working-directory behavior |
| Persistent Python/R sessions | Maintain kernel identity and state across cells; report restart/state loss explicitly |
| Environment/package management | Install into an authorized worker/project environment and record resulting dependencies |
| Project files and agent handoffs | Use scoped paths and deliberate synchronization across hosts |
| Figures, documents, and artifacts | Produce and collect them through workers, with lineage and bounded transfer |
| Main, delegated, and reviewer agents | Retain coordination and role-specific model settings; route execution consistently |
| Research MCP connectors | Preserve useful access with explicit network/credential scope and governed subprocess execution |

Exact algebra/Sage, lattice/fpylll, parameter estimators, and optional licensed Magma are early environment targets. Availability requires a functional check in the selected worker; a host installation alone is insufficient.

## Acceptance criteria

- Orchestration and research code execution run in separate containers.
- Ordinary research tool calls work against both a local pool and a Kubernetes pool.
- Interactive kernels, skills, file handoffs, and automatic reviews remain usable in both profiles.
- The web GUI can be hosted separately and closing it does not stop orchestrator work or lose run records.
- Hosted orchestration can use a local worker without publishing its container-engine API.
- Files, dependencies, and session state have explicit persistence and recovery rules.
- A reviewer can reproduce a small exact result in a fresh environment.
- Worker loss, kernel restart, cancellation, partial output, and failed review are reported accurately.
- An unavailable sandbox never silently causes code execution in the orchestrator container.
- Moving services requires configuration/state transfer rather than changing research scripts.

## Later scope

Broader science coverage, new UI design, public multi-tenant service, GPUs, and live process migration follow evidence from the initial deployment and compatibility work. Kubernetes placement and persistent kernels are part of the initial design, not late optional extensions.
