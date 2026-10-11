# Projects, sessions, and persistent containers

Date: 2026-10-10. The ownership model is confirmed by the user; storage and provisioning mechanisms below are proposed and untested.

## Confirmed model

A project normally receives its own persistent execution container, provisioned from a base image. A session is a workbook belonging to that project. Sessions normally use the project container, and may instead receive a separate container. Sessions in either mode can share data with other sessions in the same project.

```mermaid
flowchart LR
  P[Project] --> C[Default persistent project container]
  C --> A[Workbook A: own kernels]
  C --> B[Workbook B: own kernels]
  P --> S[Optional container for workbook C]
  S --> K[Workbook C: own kernels]
  C <--> D[(Project data and published artifacts)]
  S <--> D
```

The orchestrator/harness remains in a separate service container. These containers hold research execution, not copies of the entire orchestrator.

## Ownership and state

| Object | Owner and lifetime | Contents |
| --- | --- | --- |
| Project | Durable research boundary | Base-image selection, default environment, shared data/artifacts, policies, session index |
| Default project environment | Project-owned, persistent | Execution container binding, Conda/tool state, environment revision |
| Session/workbook | Belongs to one project | Conversation, plan, notebook/cells, reviews, execution history, workspace, environment binding |
| Dedicated session environment | Optional; session-owned and linked to the project | Separate container and dependency state; persist for continued workbook use by default |
| Kernel | Session-owned inside its selected environment | Live Python/R/Sage variables and process state until reset or failure |
| Shared project data | Project-owned, independent of any single session/container | Reusable inputs, sources, code, datasets, and published results |
| Research knowledge and documents | Trusted project state, independent of execution containers | Shared board/ledger, living documents, comments, source anchors, evidence/review histories |

Sessions in the default container share installed tools and dependency changes, but have separate kernel namespaces. Coordinate Conda mutations and report the environment revision each execution used. A separate session container provides independent process/dependency state while project data access is granted separately.

Logical environment IDs survive replacement of a concrete container. Record both identities in execution and monitor events. Switching a workbook to another container requires an explicit kernel reset, replay, or supported checkpoint; do not silently claim that live variables followed it.

## Data sharing

Use a project data namespace with session workspaces and a shared area. Proposed paths are logical paths inside execution environments, not paths on the orchestrator host:

```text
project/
  shared/                 reusable sources, inputs, code, and published results
  sessions/<session-id>/  workbook files, intermediate work, and outputs
```

A separate session container can access project data through scoped mounts, a storage API, or explicit transfer. The same API should resolve project references on local and remote pools. Cross-host sessions cannot assume access to a host-local volume. Kubernetes storage access modes and the selected storage driver constrain which mounts can be shared. [Persistent volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)

Confirmed access model: each workbook has its own working directory plus shared project storage. Read shared inputs, write the session workspace, and publish versioned results into the shared area. Support deliberate edits of shared files/directories with atomic publication or conflict/version checks; avoid silent last-writer-wins overwrites. Data sharing within the project's existing grant should not require approval for every file.

Session directories in a shared container organize work; they are not automatically security boundaries between sessions. Strong session separation requires an independently governed container and appropriately scoped data access. Different projects have no implicit data or environment sharing.

Verification reproductions can receive read-only snapshots of selected project/session evidence and a fresh output workspace, avoiding hidden state or concurrent changes in the producing workbook. The ordinary consistency reviewer uses recorded evidence without computation. Shared ledger/document tools resolve project references for either container mode; an agent edit still requires a valid document revision and appropriate project scope.

## Provisioning and persistence

Record the base profile (`minimal` or `crypto-base`, D46) and its image digest at project creation. Automatic Conda installation evolves the project's tool environment within its grants. Keep project data and managed dependency prefixes in durable storage; capture environment specifications and any necessary derived image or reprovisioning steps for other filesystem changes. Docker volumes have a lifecycle independent of the container, whereas removing a container removes its writable layer. [Docker volumes](https://docs.docker.com/engine/storage/volumes/)

A newly created dedicated session container starts from the project's recorded base image, as confirmed by the user. Record its base-image digest; do not seed it from the default project container's evolved environment. It can access shared project data while installing its own needed tools. Do not share a mutable Conda prefix between containers while claiming they have independent dependencies.

Resuming an existing persistent session environment retains that environment's recorded state. Creating a new session container and resuming an existing one are distinct lifecycle operations. Environment snapshots remain useful for explicit reproduction and recovery, rather than the initialization source for new dedicated sessions.

Confirmed idle behavior: preserve project and dedicated-session containers, or preserve enough state to recreate their environments. Retain project/session files, installed tools/dependencies, configuration, environment identity and durable research records. Keeping every container continuously running is not required. Idle timeout and resource-retention settings can be configurable. The worker implements the first option for OpenShell environments: with `suspend_after`, it stops an idle environment's sandbox, which keeps its writable disk, and starts it again on the next use ([worker](WORKER.md#idle-suspension)).

A restoration record identifies the base-image digest, platform, changed filesystem/image state, durable volumes/data versions, environment configuration and installed-tool state. The proposed read-only root filesystem with declared writable volumes (D48, [architecture](ARCHITECTURE.md#storage-dependencies-and-relocation)) reduces "changed filesystem state" to those volumes. Capture changes outside managed Conda prefixes as well. A package list alone is insufficient when other files/configuration changed. Verify a restored environment before discarding its only complete source state; failed capture/restore leaves a visible preserved or recovery-needed environment.

Use one environment manager for creation, provisioning, capability probes and capture/restore. Keep new-environment creation and restoration as distinct operations. Optional reproduction jobs can consume a captured specification through this same manager; they do not need another lifecycle implementation. Shared writable mounts require a managed write/lease or explicit conflict copies where revision-checked publication cannot cover mutations. See [implementation W3](IMPLEMENTATION-PLAN.md#w3--make-projectsession-state-durable-m2).

Closing the GUI does not terminate active computations or discard state. Live kernel recovery remains best effort, with explicit restart/state-loss reporting; stopping or recreating a container does not itself preserve process memory. A new session container still starts from the base image, while restoring an existing logical environment restores that environment's captured state.

## As built: the harness's project records

The harness keeps a record of each project in its state directory (`packages/orchestrator`, `projects.py`). A record holds:
- the project's base profile and policy revision;
- its default environment, `<project>-default`;
- its sessions, each with the environment it runs in and when it was last closed.

```bash
sandbox-science-harness project create --state-dir state --project p1 --profile minimal --policy sha256:...
sandbox-science-harness run --state-dir state --project p1 --session a ... "task"
sandbox-science-harness run --state-dir state --project p1 --session c --dedicated ... "task"
sandbox-science-harness session close --state-dir state --project p1 --session a --worker ... --token-file ...
sandbox-science-harness project revise --state-dir state --project p1 --policy sha256:... --worker ... --token-file ...
```

- **Running a session.** A session runs in the default environment, with its own kernels and workspace. With `--dedicated`, a new session gets an environment of its own, `<project>-<session>`, from the same base profile. A session keeps the mode it started with.
- **Environments.** Before a session runs, the harness has the worker create its environment, or revise it to the project's current policy revision (D73). A worker that does not manage environments, such as the local one, takes the ID as it is.
- **Closing a session** stops its kernels; running it again opens it again. Files stay.
- **Revising a project** records the new revision. With `--worker`, it also revises the project's environments at once; otherwise each is revised when a session next runs there.
- **Concurrency.** Records are JSON files rewritten atomically under a lock, so concurrent harness processes keep each other's sessions.
- **Shared files.** A write to the shared area names the version it replaces (`expected_sha256`, or `create_only` for a new file). So of two sessions that read the same version, the second writer gets `conflict`, rereads and tries again; nothing is silently overwritten. The conformance suite checks this with two sessions.

Not yet built:
- shared data across environments, which waits on the user's choice (worker-brokered copies with conflict copies, or explicit transfer);
- environment revisions that count package changes;
- capture and restore.

## Backend feasibility checks

Verify local and hosted pools support persistent data and dependency state, idle suspend/restore, multiple sessions/kernels in the project container, dedicated session provisioning, scoped same-project sharing, concurrent file publication, and truthful restart/reset behavior. Local Kubernetes is an accepted initial execution path. Test monitor attribution when two sessions share one sandbox: sandbox identity alone cannot identify the requesting workbook.

The illustrative [project/session record](../examples/project-sessions.json) contains two workbooks sharing the default container and a third with its own persistent environment. It is not an implemented API or deployment manifest.
