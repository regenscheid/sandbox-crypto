# Documentation index

Every page in `docs/`, by kind:

- **Reference** pages describe the system as built. They change in the same pull request as the code they describe.
- **Design and plan** pages hold the plan and the designs it was built from. They are dated, and say at the top what is built since.
- **Specifications** state required behavior and how to test it ([index](specs/README.md)).
- **Historical** pages record assessments made before the code existed.

Progress and history are not kept here: current state is in [STATUS](../.agents/STATUS.md), work follows the [refactoring plan](../.agents/refactor-plan/PLAN.md), and lab reports are in the [research record](../.agents/docs/README.md).

## Reference

| Page | What it covers |
| --- | --- |
| [Quickstart](QUICKSTART.md) | A local Sandbox Science with one command, `sandbox-science up`: what it needs, its config, logging in, a first chat, and stopping it |
| [Decisions](DECISIONS.md) | The decision register, the source of truth for each decision's status |
| [Threat model](THREAT-MODEL.md) | Assets, threat sources, trust zones and isolation tiers (D43) |
| [Licensing](LICENSING.md) | License tiers for dependencies, and the license checks |
| [Compatibility](COMPATIBILITY.md) | Lockstep releases, the protocol-version check, and how requests keep their identity |
| **Worker** | |
| [Worker](WORKER.md) | The worker service: API, executor, sandboxes, provisioning, PDFs, shared data, daemon and recovery |
| [Background jobs](BACKGROUND-JOBS.md) | Job tools, waits, watcher check-ins, notices and limits |
| [Artifacts](ARTIFACTS.md) | The artifact inventory, bounded capture and output observations |
| [Kernel revisions](KERNEL-REVISIONS.md) | A kernel's start revision compared with its environment's package revision |
| [Provisioning observations](PROVISIONING-OBSERVATIONS.md) | The worker's read-only provisioning observation routes |
| [Explicit Conda upgrades](PROVISIONING-UPGRADES.md) | The `upgrade` provisioning action |
| [Recovery fence](RECOVERY-FENCE.md) | Refusing new work after an install leaves packages uncertain (CP-13) |
| [Installer artifacts](INSTALLER-ARTIFACTS.md) | Installer step observations and sanitized log artifacts |
| [Provisioning events](PROVISION-EVENTS.md) | Provisioning events written to the event store |
| [Lockfile differences](LOCKFILE-DIFFERENCES.md) | The worker's comparison of two retained lockfiles |
| [Revision packages](REVISION-PACKAGES.md) | The worker's read of a revision's packages and declared licenses |
| [Pip lockfile licenses](PIP-LOCKFILE-LICENSES.md) | Pip license declarations recorded in lockfiles |
| [Base profile images](IMAGES.md) | The base images, their contract, build and tests |
| **Policy, monitoring and packages** | |
| [Policy file](POLICY.md) | The project policy file and how requests are decided |
| [Policy middleware](POLICY-MIDDLEWARE.md) | The OpenShell middleware that decides each sandbox request |
| [Package cache](PACKAGE-CACHE.md) | The pull-through package cache for research sandboxes |
| [Event store](EVENT-STORE.md) | The hash-chained store of monitor evidence, its record kinds and CLI |
| **Orchestrator** | |
| [Session plans](SESSION-PLANS.md) | Session plans and progress in the control store |
| [Plan editing](SESSION-PLAN-EDITING.md) | The USER plan-change route |
| [Questions to the user](QUESTIONS.md) | The `ask_user` tool and the questions it keeps |
| [MAIN prompt provenance](MAIN-PROMPT-PROVENANCE.md) | Content pins for the MAIN system prompt and tools |
| [Reviewer prompt provenance](REVIEWER-PROMPT-PROVENANCE.md) | Rubric and tool pins on reviewer calls |
| [Watcher prompt provenance](WATCHER-PROMPT-PROVENANCE.md) | Content pins on job watcher calls |
| [Research framings](STOCK-RESEARCH-PROMPTS.md) | The explicit research templates for MAIN |
| [Pilot computations](PILOT-COMPUTATIONS.md) | MAIN guidance to try a small case before a large computation |
| [Planning evaluation](PLANNING-EVALUATION.md) | The opt-in scripted planning and progress evaluation helper |
| **Documents and review** | |
| [Living documents](LIVING-DOCUMENTS.md) | The document store: parser, block IDs, revisions, patches, comments and work requests |
| [Table parser parity](TABLE-PARSER-PARITY.md) | Unicode table parsing shared by the server and the browser |
| [Project macros](PROJECT-MACROS.md) | The project's math macro setting and front matter |
| [Project ledger](LEDGER.md) | The ledger's entries, revisions, links and mutations as built |
| [Project memory](MEMORY.md) | The memory's entries, versions, proposals and the user's decisions as built |
| [Skills](SKILLS.md) | Importing skill packs from git or an archive, their reports, and the projects' pins |
| [Math checks](MATH-CHECKS.md) | Durable math checks of document revisions, and the isolated renderer |
| [Comment math](COMMENT-MATH.md) | Math checks of comment and reply bodies |
| [Finding prose math](FINDING-PROSE-MATH.md) | Math checks of MAIN fix and rebut explanations |
| [Consistency review](CONSISTENCY-REVIEW.md) | The automatic consistency reviewer |
| **Workspace GUI** | |
| [Workspace](WORKSPACE.md) | The research GUI and its server: running it, security, API, views and rendering |
| [Workspace session plans](SESSION-PLAN-WORKSPACE.md) | The read-only plan route |
| [Plan editor](SESSION-PLAN-EDITOR.md) | The GUI's structured plan text editor |
| [Workspace kernels](WORKSPACE-KERNELS.md) | The passive session kernel route and panel |
| [Provisioning workspace API](PROVISIONING-WORKSPACE.md) | The workspace's read-only provisioning routes |
| [Provisioning view](PROVISIONING-WORKSPACE-VIEW.md) | The project's Environments panel |
| [Lockfile comparison route](LOCKFILE-DIFF-WORKSPACE.md) | The workspace proxy for a lockfile comparison |
| [Lockfile comparison view](LOCKFILE-DIFFERENCES-VIEW.md) | The GUI view comparing two retained lockfiles |
| [Workspace revision packages](REVISION-PACKAGES-WORKSPACE.md) | The workspace read of a revision's packages and licenses |

## Design and plan

| Page | What it covers |
| --- | --- |
| [Roadmap](ROADMAP.md) | Milestones M0 to M5 and delivery phases |
| [Implementation plan](IMPLEMENTATION-PLAN.md) | Work packages W0 to W6, shared contracts and component ownership |
| [Architecture](ARCHITECTURE.md) | Component architecture and placement |
| [Execution plan](EXECUTION-PLAN.md) | Execution and deployment requirements and first decisions |
| [Projects and sessions](PROJECTS-AND-SESSIONS.md) | Project, session and environment ownership |
| [Product](PRODUCT.md) | Product requirements and acceptance criteria |
| [Feature plan](FEATURE-PLAN.md) | Research features to incorporate from the source study |
| [Research workspace](RESEARCH-WORKSPACE.md) | Requirements for documents, the ledger, Sage and PDFs |
| [External skills](EXTERNAL-SKILLS.md) | Platform requirements from the crypto-analysis-skills pack |
| [Monitoring](MONITORING.md) | Trusted monitoring and automatic provisioning |
| [MCP connectors](MCP-CONNECTORS.md) | MCP authentication and the MCP proxy |
| [Orchestrator prompts](ORCHESTRATOR-PROMPTS.md) | The prompt inventory, and proposed prompts and planning design |

## Historical

| Page | What it covers |
| --- | --- |
| [OpenShell evaluation](OPENSHELL-EVALUATION.md) | The 2026-10-10 assessment of OpenShell as the execution backend |
| [Source study](SOURCE-STUDY.md) | The 2026-10-10 study of CScience, Claude Code and OpenScience Desktop |
| [Reuse assessment](REUSE-ASSESSMENT.md) | The 2026-10-10 source and reuse inspection |

The 2026-10-10 planning consistency review is kept in [.agents](../.agents/planning-consistency-review-2026-10-10.md).
