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
| D07 | Recommended | Persistent project environment, session-owned kernels, disposable batch/review executions | Preserve interactive state and support fresh reproductions |
| D08 | Recommended | Worker-initiated authenticated control transport | Supports local workers behind NAT with hosted orchestration |
| D09 | Recommended | Pinned base images plus recorded project dependencies and environment snapshots | Allow exploratory installs while making reproductions explicit |
| D10 | Recommended | Authoritative execution/review evidence outside the browser | Survives closed UI and records interrupted or failed work |
| D11 | Open | Retained runtime versus a compatible independent harness | Decide after measuring patchability and functional parity; a rewrite is not the default |
| D12 | Open | Persistence implementation and project file synchronization | Preserve existing state in an adaptation spike; do not assume a database migration is necessary |
| D13 | Located, assessment partial | Prior fork: regenscheid/open-science-crypto, main a7e39ea | Contracts/lifecycle/RFC are relevant; base diff and end-to-end runtime remain to validate |
| D14 | Working name | sandbox-science | Folder name, not final branding |
| D16 | Confirmed | Preserve automatic Conda tool installation inside isolated worker environments | The convenience is valuable and increases the importance of isolation |
| D17 | Confirmed | Monitor incoming/outgoing behavior beyond basic network reachability policy | Trusted external monitoring with task/execution context |
| D18 | Open | Response to uncertain suspicious activity | Proposed: pause high-risk anomalies; log lesser ones; user preference pending |
| D15 | Recommended | Single-user/self-hosted initial deployment; redeployment/reproduction before live memory migration | Matches the immediate use case |

## Decisions deliberately deferred

Do not select a new model framework, UI framework, database, or broad science catalog before the CScience execution feasibility spike. If a new implementation proves necessary, use the smallest stack that preserves the valued behavior and worker contract.

Provider/authentication compatibility with the retained runtime must be checked in that spike. Separate role settings for main agents, delegated agents, and reviewers should remain usable.

## Discussion order

1. Persistent project environment and session/kernel ownership.
2. Project file authority, remote data transfer, and dependency installation grants.
3. Automatic review triggers, evidence available to reviewers, and reproduction policy.
4. First local/Kubernetes deployment pair and concrete target resources.

The earlier project no longer needs locating. Its implementation status and extraction costs still need validation.
