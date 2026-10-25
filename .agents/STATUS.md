# Status

Updated 2026-10-25. For the owner; one screen. Details: [BACKLOG](BACKLOG.md), [PLAN-CHANGES](PLAN-CHANGES.md), [decisions](../docs/DECISIONS.md).

## Phase and focus

- `k8s-sync` is the integration branch. It is `k8s` plus `origin/dev` (`3918ccd0`), then the settled architecture (D95-D103, D92 amended) built on top. It is pushed to `origin/k8s-sync`; no PR is open and nothing is merged into `dev`.
- `dev` is unchanged except PR #520. `main` is unchanged.
- The nightly runs the workflow file on `main`, so the sharded flake-measuring jobs reach it only when merged there. Nightly CI is not yet green.
- A test instance runs on the cluster (2026-10-24): OpenShell 0.1.5, Agent Sandbox v1.0.3, Kata, the bundle and a capped model key. The personal instance and SNOVA are untouched.

## Built and verified (offline tests only)

- The full suite passes offline: Python, web, math and figure checks, static and license checks.
- Effect classes, per-server classifications, argument rules, lockdown modes and the drift test (D95, D96).
- Grants of a class, a host or a package source, with cards for every request kind (D97).
- Reads-free defaults: GET/HEAD on any port, upload-pack anywhere, reviewed read-only POST list (D95).
- A child's fence covers outward effects only; children get the read-class connector tools (D99).
- Automatic retries and one Resume for the whole tree; idle suspension off by default (D100).
- Worker, policy middleware and package cache as one unit (D101).
- Budgets only track; soft caps are check-ins; one local cost enforcement (D102).
- One logging setup for every service, Pod labels and log-collection steps (D103).
- Persistent kernels are the default for computation; one OpenShell version constant.
- The operator checklist and the backup and restore runbook are written.
- B9, legibility of agent work in the records (2026-10-25): every tool call names its purpose, kept in the archive and shown in result headers and fold skeletons; task briefs may list deliverables that the result accounts for, and children return tool lessons; review findings have five statuses with reviewer re-checks on later revisions, author dismissals and staleness, migrated in place; memory recall shows each note's basis, date and anchors. The workspace rendering of all this is B10.
- B10, legibility of agent work in the workspace (2026-10-25): the conversation shows each tool call's purpose with a result gloss, background work in one line, agent messages and children's outcomes as rows, named agents with a breadcrumb on a child's transcript, reviewer findings as compact cards with the five statuses, and the artifacts of each step with an Artifacts panel. Since then: the full finding card, one block per finished delegation, composer completion and each kernel's current cell are built; a per-message model choice is not.
- B3's defects found live, fixed offline (2026-10-25): the runtime manager waits for the gateway and reports what failed; an image license check runs locally and in CI; the worker reconciles unknown-outcome environments on confirmed absence. Not yet run on the cluster.
- A10's offline part (2026-10-25): each run records its tool mix, an offline `tool-mix` command measures recorded runs, one platform note per run steers shell-run Python to the kernel, and a lost kernel's result names what to recompute. The SNOVA measurement and a live lost-kernel test remain.

## Verified live on the cluster (test instance, 2026-10-24)

- Egress only through OpenShell: git from GitHub, pip through the cache, unlisted hosts and pypi.org denied and recorded, raw sockets to public, LAN and in-cluster addresses refused. MCP is not configured yet.
- Kata in effect with tier 3 recorded. Plain runc is not usable: OpenShell refuses to start there (D43).
- The manager cycle: bootstrap, reconnection to the same sandbox after its Pods are killed, explicit replace, control history intact.
- The Secret generator, the bundle and the OpenShell values, after the fixes the run found.

## Built but not verified live

- On the cluster: Longhorn backups to Garage, Garage artifacts, log collection, job sandboxes, MCP.
- The direct MCP path (agent to server through OpenShell and the middleware, D98): a prototype that passes offline only.
- Ports admitted on demand, and the workspace answering network and package requests, against a real gateway.
- Soft-cap check-ins and loop detection with a real model.
- The unit on babbage and the compiled lockdown policy.

## Decided but not built

- Run the babbage lab for the direct MCP path, then your decision to retire the MCP proxy or keep it (D98, A2).
- The workspace's review view for classifications, and proxy-side drift findings (A3).
- Cards for the direct MCP path's requests (A4).
- The web usage view and a real-model check of the check-ins (A8).
- Measure shell against kernel use and steer agents to the kernel (A10).
- First deployment and its three proofs (B3); durability extras, load balancer and Authentik (B4); dashboards, alerts and canaries (B5); job sandboxes (B6, D93).
- A second instance for another trust group (D94).

## Operator-side prerequisites

1. Talos 1.13.12 with Kata on the amd64 nodes: done and verified.
2. Cluster preparation: done for the test instance (namespaces, storage classes, RuntimeClass, Agent Sandbox, OpenShell); the [checklist](../docs/KUBERNETES-PREP.md) now carries what the run found. Still open: a Garage bucket and keys for artifacts and backups (backups go to the existing NFS target for now).
3. Images: the trusted images in use are `trusted-app` and `trusted-worker` at `30c78ad5`; the profile images `orchestrator:3cf3bd51`, `minimal:d3b1f1cf` and `crypto-base:d3b1f1cf` are from run 40081031769. Digests are in the private deployment record.

## Boundaries that stand

- Images stay private on ghcr. No push of `dev` or `main` by agents.
- SNOVA and its archive stay preserved; its stopped monitor is not recreated.
