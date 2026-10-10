# Product scope

Date: 2026-10-10. All behavior below is proposed unless described as source evidence elsewhere.

## Initial user and workflow

The first user investigates cryptographic constructions, mathematical claims, parameters, and implementations. They need to connect literature and standards to executable experiments, inspect intermediate work, and preserve exact assumptions and limitations.

The workbench should support this loop:

1. Create a project with a question, source material, objective, and acceptance criteria.
2. Build a source-backed plan, including what needs computation and which environments can perform it.
3. Run bounded experiments in a selected local or remote sandbox; inspect code, output, resource use, and errors.
4. Compare results with independent calculations or reviewers and distinguish evidence from conjecture.
5. Produce a report, figures, tables, and a reproducible experiment bundle.

Claude Science is a product reference for integrated scientific work and auditable artifacts, rather than a runtime dependency. See the [Anthropic announcement](https://www.anthropic.com/news/claude-science-ai-workbench).

## Initial research capabilities

| Workflow | Candidate environment | Evidence required |
| --- | --- | --- |
| Finite fields, polynomial arithmetic, exact algebra | SageMath; selected PARI/GP, GAP, Singular tools | Exact inputs, tool versions, verified identities and output |
| Lattice experiments | Python with fpylll/fplll; Sage where appropriate | Seeds, precision, basis, parameters, reduction settings, actual library smoke tests |
| Parameter and complexity analysis | SymPy, GMPY2, mpmath; pinned estimator repositories | Formula/source, estimator revision, assumptions, numerical precision |
| Implementation experiments | Explicit compiler/toolchain image | Source revision, compiler/options, target CPU, resource limits; no cross-machine timing equivalence claim |
| Literature and standards research | Existing research MCP services through a broker | Source identifiers, access date, excerpts/locations, version/status |
| Reports and figures | Dedicated document/plotting image | Source data, producing script, environment, artifact digests |

Magma is an optional operator-managed integration. Validate its licensing and execution requirements on the actual target before offering it. Do not make unrestricted host execution the fallback for a failed sandbox.

## Workbench experience

Start with a project list and a project screen containing conversation/plan, files/artifacts, and execution details. Add a separate environments/workers view to show availability, supported capabilities, current reservations, and measured checks. A run should expose the machine, image, policy, inputs, logs, result, and a reproduce action.

The user should be able to select an allowed execution location without changing the research task. Show data movement and model routing when those choices affect where project content goes. A remote web server does not imply that computation or inference is remote; display their locations separately.

Within an already authorized project policy, ordinary code execution should proceed without repeated per-command approval. Ask for a concrete policy expansion, additional dataset access, new credential scope, or new data destination when needed. Store that grant with its scope and expiry. Policy enforcement must remain outside the model's control.

## MVP acceptance criteria

- Closing every browser tab does not stop work or lose run records.
- A UI container can be redeployed without changing project/session state.
- An orchestrator/harness container can be restarted and recover durable sessions and reconcile active runs.
- The same run contract works against one local and one remote worker pool.
- A worker executes generated code only inside an approved sandbox; failure to establish isolation prevents execution.
- A healthy host installation is not confused with an executable sandbox capability. Probe inside the chosen execution environment.
- A run records failed, interrupted, timed-out, and cancelled attempts as well as successful attempts.
- Each result links to its input snapshot, code, environment, policy, and measured execution evidence.
- A reviewer can reproduce a small exact result in a fresh environment without relying on the producing agent's notebook memory.
- A project can be exported with manifests and artifacts without including infrastructure credentials.

## Later scope

Extend through environment profiles, domain skills, artifact viewers, and evaluation fixtures. Stateful notebooks, GPU workers, proof assistants, teams, cluster scheduling, and optional desktop shells follow the initial execution path. Public multi-tenant hosting and live process migration require separate designs.

