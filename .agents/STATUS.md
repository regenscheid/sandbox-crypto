# Status

Updated 2026-10-24. For the owner; one screen. Details: [BACKLOG](BACKLOG.md), [PLAN-CHANGES](PLAN-CHANGES.md), [decisions](../docs/DECISIONS.md).

## Phase and focus

- `k8s-sync` is the integration branch. It is `k8s` plus `origin/dev` (`3918ccd0`), then the settled architecture (D95-D103, D92 amended) built on top. It is pushed to `origin/k8s-sync`; no PR is open and nothing is merged into `dev`.
- `dev` is unchanged except PR #520. `main` is unchanged.
- The nightly runs the workflow file on `main`, so the sharded flake-measuring jobs reach it only when merged there. Nightly CI is not yet green.
- No cluster, personal instance or preserved SNOVA service has been touched by this work.

## Built and verified (offline tests only)

- The full suite passes offline: Python, web, math and figure checks, static and license checks.
- Effect classes, per-server classifications, argument rules, lockdown modes and the drift test (D95, D96).
- Grants of a class, a host or a package source, with cards for every request kind (D97).
- Reads-free defaults: GET/HEAD on any port, upload-pack anywhere, reviewed read-only POST list (D95).
- A child's fence covers outward effects only (D99).
- Automatic retries and one Resume for the whole tree; idle suspension off by default (D100).
- Worker, policy middleware and package cache as one unit (D101).
- Budgets only track; soft caps are check-ins; one local cost enforcement (D102).
- One logging setup for every service, Pod labels and log-collection steps (D103).
- Persistent kernels are the default for computation; one OpenShell version constant.
- The operator checklist and the backup and restore runbook are written.

## Built but not verified live

- Everything Kubernetes-specific: the bundle, runtime manager, Longhorn snapshots, log collection, Agent Sandbox, OpenShell 0.1.5 on the cluster.
- The direct MCP path (agent to server through OpenShell and the middleware, D98): a prototype that passes offline only.
- Ports admitted on demand, and the workspace answering network and package requests, against a real gateway.
- Soft-cap check-ins and loop detection with a real model.
- Kata on your nodes; the checklist's commands and manifests are unrun.
- The unit on babbage and the compiled lockdown policy.

## Decided but not built

- Run the babbage lab for the direct MCP path, then your decision to retire the MCP proxy or keep it (D98, A2).
- The workspace's review view for classifications, and proxy-side drift findings (A3).
- Cards for the direct MCP path's requests (A4).
- Read connectors for delegated children (A6).
- The web usage view and a real-model check of the check-ins (A8).
- Measure shell against kernel use and steer agents to the kernel (A10).
- First deployment and its three proofs (B3); durability extras, load balancer and Authentik (B4); dashboards, alerts and canaries (B5); job sandboxes (B6, D93).
- A second instance for another trust group (D94).

## Operator-side prerequisites

1. Node rebuild to Talos 1.13.12 with Kata on the amd64 nodes: done, per the owner. The repo's checklist names Talos 1.13 and the `kata-containers` extension only.
2. Cluster preparation per [KUBERNETES-PREP](../docs/KUBERNETES-PREP.md), unverified until run.
3. Image republish: the published images do not include this branch; rebuild, publish and pin by digest.

## Boundaries that stand

- Images stay private on ghcr. No push of `dev` or `main` by agents.
- SNOVA and its archive stay preserved; its stopped monitor is not recreated.
