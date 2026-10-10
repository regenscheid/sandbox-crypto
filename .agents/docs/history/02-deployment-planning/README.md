# Sandbox Science

Working folder for a cryptography/math research workbench with independently deployable orchestration and sandboxed compute.

**Status: planning only, 2026-10-10.** No application, runtime, or deployment has been implemented or tested here.

## Agreed direction

CScience is the functional baseline. Preserve its useful research behavior, built-in skills that organize the work, automatic reviews, and automatic Conda tool provisioning. The main change is the execution architecture: run the orchestrator/harness in its own container and execute research code and data processing in separate worker containers. Package the web GUI separately so each layer can run locally or remotely, including on Kubernetes.

The first engineering question is whether CScience's existing execution boundary can be adapted while retaining its research loop. A replacement harness or new UI is conditional on that investigation, rather than an initial requirement.

Evaluate NVIDIA OpenShell as the execution backend behind an adapter. Its documented Docker/Podman and Kubernetes drivers fit the intended placements, but adoption requires actual lifecycle, persistence, and isolation checks. The evaluation baseline is [v0.1.3](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.3). OpenShell does not supply the research workflow or automatic scientific review. A trusted monitor should correlate supported request/response inspection with task, tool, and package activity.

## Read first

| Document | Purpose |
| --- | --- |
| [Execution plan](docs/EXECUTION-PLAN.md) | Requirements, proposed topology, and the first decisions to discuss |
| [Architecture](docs/ARCHITECTURE.md) | Worker API, state, transport, and local/Kubernetes placement |
| [Product](docs/PRODUCT.md) | Functional baseline, skills, and automatic review requirements |
| [Reuse assessment](docs/REUSE-ASSESSMENT.md) | CScience and the located OpenScience Desktop fork |
| [Monitoring and provisioning](docs/MONITORING.md) | Trusted traffic/content monitoring and isolated Conda installs |
| [OpenShell evaluation](docs/OPENSHELL-EVALUATION.md) | Verified documentation and runtime feasibility checks |
| [Roadmap](docs/ROADMAP.md) | Ordered implementation gates |
| [Decisions](docs/DECISIONS.md) | Confirmed requirements, recommendations, and open choices |
| [Deployment examples](examples/deployment-profiles.json) | Illustrative placements; not executable manifests |
| [Run request](examples/run-request.json) | Illustrative batch contract; not an OpenShell request |

The earlier project was located at [regenscheid/open-science-crypto](https://github.com/regenscheid/open-science-crypto). Its execution contracts, lifecycle work, and persistent-environment proposal are relevant references; its end-to-end agent execution remains unproven. The [crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills) repository is another useful domain source.

## First proof

Keep the orchestrator in container A. Use the ordinary research tools to execute shell commands and two stateful Python cells in sandbox B, exchange project files, and perform a reviewer reproduction in fresh sandbox C. Repeat against a Kubernetes execution pool through the same tool interface. Verify that research code executes outside container A.

Portability initially means redeploying services, reconnecting storage, and restoring or reproducing work. Live kernel memory migration requires explicit checkpoint support.
