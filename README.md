# Sandbox Science

Working folder for a cryptography/math research workbench with independently deployable orchestration and sandboxed compute.

**Status: planning only, 2026-10-10.** No application, runtime, or deployment has been implemented or tested here.

## Agreed direction

CScience is the functional baseline. Preserve its useful research behavior, built-in skills that organize the work, automatic reviews, and automatic Conda tool provisioning. The main change is the execution architecture: run the orchestrator/harness in its own container and execute research code and data processing in separate worker containers. Package the web GUI separately so each layer can run locally or remotely, including on Kubernetes.

A project normally owns a persistent execution container created from a base image. Sessions/workbooks use that container by default, can request their own container, and can share project data in either mode. See [project/session ownership](docs/PROJECTS-AND-SESSIONS.md).

New dedicated session containers start from the base image. Automatic reviews should follow current Claude Science behavior. Monitor policy supports blocking, escalation for permission, and alerting while work continues.

Idle project/session environments must retain their containers or enough state to recreate them with files, tools and configuration. Workbooks have separate working directories plus shared project storage with conflict detection. Initial deployments target hosted Kubernetes and local containers. Local Docker/other runtimes or Kubernetes on macOS/Docker Desktop or Babbage Linux are acceptable if execution and network isolation policies can be enforced.

The initial research workspace includes living documents with direct user edits and anchored comments, a project-wide board/ledger, LaTeX throughout the interface and outputs, native Sage kernels, and navigable mathematical PDFs. Preserve Science-style automatic consistency review. A separate fresh-environment verification/reproduction role is a proposed extension, rather than a requirement for every automatic review.

The first engineering question is whether CScience's existing execution boundary can be adapted while retaining its research loop. A replacement harness or new UI is conditional on that investigation, rather than an initial requirement.

Evaluate NVIDIA OpenShell as the execution backend behind an adapter. Its documented Docker/Podman and Kubernetes drivers fit the intended placements, but adoption requires actual lifecycle, persistence, and isolation checks. The evaluation baseline is [v0.1.3](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.3). OpenShell does not supply the research workflow or automatic scientific review. A trusted monitor should correlate supported request/response inspection with task, tool, and package activity.

## Read first

| Document | Purpose |
| --- | --- |
| [Detailed implementation plan](docs/IMPLEMENTATION-PLAN.md) | Shared contracts, component ownership, ordered work packages and evidence gates |
| [Consistency review](docs/CONSISTENCY-REVIEW.md) | Resolved planning drift and opportunities to reuse infrastructure and standard components |
| [Durable analysis archive](.agents/docs/README.md) | Individual agent investigations, current planning copies and earlier working drafts |
| [Feature plan](docs/FEATURE-PLAN.md) | What to incorporate from the three research investigations and how to verify it |
| [Source study](docs/SOURCE-STUDY.md) | Pinned evidence from Claude Science, Claude Code/Desktop, and OpenScience Desktop |
| [Research workspace](docs/RESEARCH-WORKSPACE.md) | Living documents, shared ledger, LaTeX, native Sage, and navigable PDFs |
| [External skills](docs/EXTERNAL-SKILLS.md) | Platform requirements inferred from the existing crypto-analysis-skills pack; no embedded catalog |
| [Execution plan](docs/EXECUTION-PLAN.md) | Requirements, proposed topology, and the first decisions to discuss |
| [Projects and sessions](docs/PROJECTS-AND-SESSIONS.md) | Persistent project containers, optional session containers, and data sharing |
| [Architecture](docs/ARCHITECTURE.md) | Worker API, state, transport, and local/Kubernetes placement |
| [Product](docs/PRODUCT.md) | Functional baseline, skills, and automatic review requirements |
| [Reuse assessment](docs/REUSE-ASSESSMENT.md) | CScience and the located OpenScience Desktop fork |
| [Monitoring and provisioning](docs/MONITORING.md) | Trusted traffic/content monitoring and isolated Conda installs |
| [OpenShell evaluation](docs/OPENSHELL-EVALUATION.md) | Verified documentation and runtime feasibility checks |
| [Roadmap](docs/ROADMAP.md) | Ordered implementation gates |
| [Decisions](docs/DECISIONS.md) | Confirmed requirements, recommendations, and open choices |
| [Deployment examples](examples/deployment-profiles.json) | Illustrative placements; not executable manifests |
| [Run request](examples/run-request.json) | Illustrative batch contract; not an OpenShell request |

The earlier project was located at [regenscheid/open-science-crypto](https://github.com/regenscheid/open-science-crypto). Its execution contracts, lifecycle work, and persistent-environment proposal are relevant references; its end-to-end agent execution remains unproven. The existing [crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills) pack stays external. Its workflows inform platform capabilities and compatibility tests; its content is not embedded here.

## First proof

Keep the orchestrator in container A. First route an ordinary shell command and two stateful Python cells to sandbox B, exchange a file, interrupt work and reconnect through the retained tools. Observe where research code actually executes. Then add native Sage, PDF processing and the same worker contract on local and hosted Kubernetes targets. The complete product fixture connects a PDF source, shared ledger claim, LaTeX living document, user edit/comment, and automatic consistency review across workbooks. A proposed reproduction extension can reuse that execution path with fresh pinned inputs.

Portability initially means redeploying services, reconnecting storage, and restoring or reproducing work. Live kernel memory migration requires explicit checkpoint support.
