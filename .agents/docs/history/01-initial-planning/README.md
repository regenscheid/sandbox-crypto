# Sandbox Science

Working name for an independent scientific research workbench, beginning with cryptography and mathematics and designed to support other sciences later.

**Status: planning only, 2026-10-10.** This folder contains a proposed architecture, source assessment, and implementation gates. It contains no running application, installed runtime, or deployment manifests.

## Direction

Build a browser workbench whose web UI, orchestrator/harness, and compute workers can be deployed independently in local or remote containers. Run generated code, notebooks, build tools, and document processing in explicitly governed execution environments. Record enough evidence to reproduce a result after changing machines or model providers.

Evaluate NVIDIA OpenShell as the first sandbox backend behind an owned adapter. Its role is execution isolation and policy enforcement; the workbench still owns research projects, conversations, scheduling, scientific environments, artifacts, and reproducibility. OpenShell v0.1.3 is the evaluation baseline, verified against the [release published October 9](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.3).

Use existing code selectively. CScience offers relevant mathematical rendering, environment profiles, capability resolution, and regression cases. Upstream Open Science Desktop offers a possible UI and agent adapter starting point. The user's earlier OpenScience Desktop project has not yet been located, so its changes remain unassessed.

## Read first

| Document | Purpose |
| --- | --- |
| [Product](docs/PRODUCT.md) | Initial research workflows and user experience |
| [Architecture](docs/ARCHITECTURE.md) | Component boundaries, placement, storage, and execution contract |
| [OpenShell evaluation](docs/OPENSHELL-EVALUATION.md) | Verified documentation, gaps, and a concrete feasibility spike |
| [Reuse assessment](docs/REUSE-ASSESSMENT.md) | What appears reusable and what still needs inspection |
| [Roadmap](docs/ROADMAP.md) | Ordered milestones with observable completion gates |
| [Decisions](docs/DECISIONS.md) | Working choices and unresolved questions |
| [Deployment examples](examples/deployment-profiles.json) | Illustrative placements; not executable configuration |
| [Run request example](examples/run-request.json) | Proposed domain contract; not an OpenShell API request |

## First proof

Run one deterministic finite-field calculation in a pinned sandbox image, capture code/input/output digests and execution evidence, and reproduce it on a second machine using the same workbench API. Establish the sandbox boundary and remote transport before adding an autonomous research loop.

The core portability promise is redeployment and reproducible execution. Moving a running process, notebook memory, or active GPU computation is a separate capability requiring explicit checkpoint support.

