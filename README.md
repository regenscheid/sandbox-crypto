# Sandbox Science

Working folder for a cryptography/math research workbench with independently deployable orchestration and sandboxed compute.

**Status:** the worker, policy services, harness and web workspace are built in part and run on OpenShell VMs on babbage; the project ledger, with its views, is built in part and other deployment targets are not built yet. Chat with the main agent in the GUI, delegation of checks and tasks, MCP connectors, skills in runs and a viewer for published PDFs are built. [STATUS](.agents/STATUS.md) has the current state, the [refactoring plan](.agents/refactor-plan/PLAN.md) the next steps, and the [documentation index](docs/README.md) every page. Development follows [the conventions](.agents/CONVENTIONS.md).

## Terminology

- **CScience**: the user's patched distribution of Claude Science (upstream 0.1.60, `cscience-crypto` package 0.8.0), which applies compatibility patches to an extracted, proprietary upstream runtime bundle. It is the *behavior reference* (D03). **No Claude Science code, prompts, skill text, schemas or assets are used**; the needed parts are reimplemented (D11, D56). The user's own wrapper code in `cscience-crypto` (for example capability profiles and math-rendering helpers) may be reused only where it embeds no upstream code.
- **babbage**: the user's Ubuntu 26.04 machine (host name; amd64, Ryzen 395, 128 GB), the phase 1 target. KVM is available, so it can use OpenShell's MicroVM driver. OpenShell requires Landlock ABI ≥ 3 (Linux 6.2+) and active qualification on the actual kernel; check it on babbage, along with Ubuntu's AppArmor restriction on unprivileged user namespaces, which affects rootless runtimes.
- **Isolation tier**: the strength of the boundary an execution actually ran with; see [threat model](docs/THREAT-MODEL.md).

## Agreed direction

CScience is the behavior reference, not a code base. Reimplement its useful research behavior: work-organizing skills, plans and delegation, automatic reviews, and automatic Conda tool provisioning. The new implementation is MIT-licensed and uses no Claude Science code (D11, D45, D56). The main architectural change is: run the orchestrator/harness in its own container and execute research code and data processing in separate worker containers. Package the web GUI separately so each layer can run locally or remotely, including on Kubernetes.

A project normally owns a persistent execution container created from a base image. Sessions/workbooks use that container by default, can request their own container, and can share project data in either mode. See [project/session ownership](docs/PROJECTS-AND-SESSIONS.md).

New dedicated session containers start from the base image. Automatic reviews should follow current Claude Science behavior. Monitor policy supports blocking, escalation for permission, and alerting while work continues.

Idle project/session environments must retain their containers or enough state to recreate them with files, tools and configuration. Workbooks have separate working directories plus shared project storage with conflict detection. Initial deployments target hosted Kubernetes and local containers. Local Docker/other runtimes or Kubernetes on macOS/Docker Desktop or babbage (Ubuntu 26.04) are acceptable if execution and network isolation policies can be enforced.

Delivery is staged by placement (D53): phase 1 is the complete feature set, including crypto-research-mcp, on babbage. Phase 2 is the user's mixed-arch Kubernetes test cluster. Phase 3 covers the Mac, more MCP servers and remote-access decisions. Server-side code is Python (D60). Models go through a LiteLLM proxy, OpenAI first and Anthropic Messages later (D61). Permissions come from a permissive-by-default policy YAML with an escalation timeout that falls back to deny (D62). Living documents are Markdown with LaTeX math (D65).

The initial research workspace includes living documents with direct user edits and anchored comments, a project-wide board/ledger, LaTeX throughout the interface and outputs, native Sage kernels, and navigable mathematical PDFs. Preserve Science-style automatic consistency review. A separate fresh-environment verification/reproduction role is a proposed extension, rather than a requirement for every automatic review.

The first engineering step is a written behavior specification of the CScience features worth keeping, plus a minimal new harness whose tools execute only through the worker API. The harness is built from MIT-compatible components ([licensing](docs/LICENSING.md)); OpenScience Desktop (MIT) is the first code-reuse candidate. The GUI is new as well, kept deliberately small at first.

Evaluate NVIDIA OpenShell as the execution backend behind an adapter. Its documented Docker/Podman and Kubernetes drivers fit the intended placements, but adoption requires actual lifecycle, persistence, and isolation checks. The evaluation baseline is [v0.1.3](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.3). OpenShell does not supply the research workflow or automatic scientific review. A trusted monitor should correlate supported request/response inspection with task, tool, and package activity.

## Read first

| Document | Purpose |
| --- | --- |
| [Quickstart](docs/QUICKSTART.md) | Running a local Sandbox Science with one command: `sandbox-science up` |
| [Documentation index](docs/README.md) | Every page in `docs/`, as reference, design, specification or historical |
| [Detailed implementation plan](docs/IMPLEMENTATION-PLAN.md) | Shared contracts, component ownership, ordered work packages and evidence gates |
| [Planning consistency review](.agents/planning-consistency-review-2026-10-10.md) | The 2026-10-10 review of planning drift and reuse of infrastructure and standard components (historical) |
| [Analysis archive](.agents/docs/README.md) | The agent source investigations, lab reports and acceptance reports; earlier plan snapshots are the git tags `plan-03-pre-review` and `plan-04-reviewed` |
| [Feature plan](docs/FEATURE-PLAN.md) | What to incorporate from the three research investigations and how to verify it |
| [Source study](docs/SOURCE-STUDY.md) | Pinned evidence from Claude Science, Claude Code/Desktop, and OpenScience Desktop |
| [Research workspace](docs/RESEARCH-WORKSPACE.md) | Living documents, shared ledger, LaTeX, native Sage, and navigable PDFs |
| [Workspace GUI](docs/WORKSPACE.md) | The GUI as built (D80): building and running it, login, what the browser may do, math repairs, living documents, the API and views |
| [Living documents](docs/LIVING-DOCUMENTS.md) | Documents as built: the parser shared with the browser, block IDs, revisions and patches in the control store, the agents' document tool |
| [Consistency review](docs/CONSISTENCY-REVIEW.md) | The automatic consistency reviewer as built: triggers, snapshots, findings and their delivery |
| [External skills](docs/EXTERNAL-SKILLS.md) | Platform requirements inferred from the existing crypto-analysis-skills pack; no embedded catalog |
| [Execution plan](docs/EXECUTION-PLAN.md) | Requirements, proposed topology, and the first decisions to discuss |
| [Projects and sessions](docs/PROJECTS-AND-SESSIONS.md) | Persistent project containers, optional session containers, and data sharing |
| [Architecture](docs/ARCHITECTURE.md) | Worker API, state, transport, and local/Kubernetes placement |
| [Worker](docs/WORKER.md) | Execution, sandbox lifetimes, package provisioning, shared data and reconnectable executor daemons |
| [Background jobs](docs/BACKGROUND-JOBS.md) | Job tools, waits, watcher check-ins, durable notices and current limits |
| [Product](docs/PRODUCT.md) | Functional baseline, skills, and automatic review requirements |
| [Reuse assessment](docs/REUSE-ASSESSMENT.md) | CScience and the located OpenScience Desktop fork |
| [Monitoring and provisioning](docs/MONITORING.md) | Trusted traffic/content monitoring and isolated Conda installs |
| [OpenShell evaluation](docs/OPENSHELL-EVALUATION.md) | Verified documentation and runtime feasibility checks |
| [Roadmap](docs/ROADMAP.md) | Ordered implementation gates |
| [Decisions](docs/DECISIONS.md) | Confirmed requirements, recommendations, and open choices |
| [Threat model](docs/THREAT-MODEL.md) | Assets, threat sources, trust zones, isolation tiers and data classes (D43) |
| [Policy file](docs/POLICY.md) | Permissive-by-default YAML project policy, precedence, escalation timeout ([example](examples/policy.yaml)) |
| [MCP connectors](docs/MCP-CONNECTORS.md) | OAuth (primary), bearer and custom-header auth for MCP servers; connector auth service and the MCP proxy that all MCP traffic goes through |
| [Licensing](docs/LICENSING.md) | MIT goal, dependency tiers, runtime-installed GPL tools, components to avoid |
| [Base profile images](docs/IMAGES.md) | The `minimal` and `crypto-base` images, and the `orchestrator` and `mcp-proxy` images of the harness and the MCP proxy: contract, pinned sources, building and tests |
| [Deployment examples](examples/deployment-profiles.json) | Illustrative placements; not executable manifests |
| [Run request](examples/run-request.json) | Illustrative batch contract; not an OpenShell request |

The earlier project was located at [regenscheid/open-science-crypto](https://github.com/regenscheid/open-science-crypto). Its execution contracts, lifecycle work, and persistent-environment proposal are relevant references; its end-to-end agent execution remains unproven. The existing [crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills) pack stays external. Its workflows inform platform capabilities and compatibility tests; its content is not embedded here.

## First proof

Run the new harness in container A. First route an ordinary shell command and two stateful Python cells to sandbox B, exchange a file, interrupt work and reconnect through the harness's tools. Observe where research code actually executes. Then add native Sage, PDF processing and the same worker contract on local and hosted Kubernetes targets. The complete product fixture connects a PDF source, shared ledger claim, LaTeX living document, user edit/comment, and automatic consistency review across workbooks. A proposed reproduction extension can reuse that execution path with fresh pinned inputs.

Portability initially means redeploying services, reconnecting storage, and restoring or reproducing work. Live kernel memory migration requires explicit checkpoint support.

## License

This project is released under the MIT License (see [LICENSE](LICENSE), D45). Platform code uses permissive dependencies only. GPL research tools such as Sage are installed at runtime into worker environments, or built into the private `crypto-base` image, rather than shipped. AGPL components are avoided. See [licensing](docs/LICENSING.md).
