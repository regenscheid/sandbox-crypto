# Threat model and isolation tiers

Date: 2026-10-10. Proposed by the plan review ([critical review](../.agents/critical-review.md), finding C1), revised the same day with the user's priorities. Confirmed on 2026-10-14 as D43, adopted under the owner's instruction to proceed with the refactoring plan's recommendations. It is the design the controls follow, not a list of what is built; the built controls are described in [worker](WORKER.md), [policy middleware](POLICY-MIDDLEWARE.md), [package cache](PACKAGE-CACHE.md), [MCP connectors](MCP-CONNECTORS.md) and [workspace](WORKSPACE.md). It states what the isolation, secrets and monitoring controls in [architecture](ARCHITECTURE.md) and [monitoring](MONITORING.md) protect, and from whom.

## Primary concern: agentic workers overreaching

The user's main worry is **agents doing things they shouldn't**: acting beyond their task, scope or grants. That includes the agent loop's tool and MCP calls, and the code the agents write, download, install and run in workers. The agent is not assumed to be malicious. It is assumed to be capable, persistent, sometimes wrong, and willing to work around obstacles to finish a task. Controls should therefore make out-of-scope actions **impossible or visible** rather than rely on the agent's judgement.

Prompt injection from consumed papers is a lesser concern. It remains one way an agent can be steered out of scope, and the same controls cover it.

## Scope and assumptions

- Single trusted user (D15). Multi-tenant hosting is out of scope; adding it would require revisiting every tier below.
- Code inside a research sandbox is **untrusted by default**, whatever its origin: agent-written, downloaded or installed.
- The orchestrator runs trusted code but performs **agent-selected actions** (tool calls, MCP calls, model requests), so its outbound actions are governed like a worker's (D47).
- External services are mostly reached through MCP (D58). Workers that need API keys receive them through a secrets manager that keeps raw keys out of the sandbox (D57).

## Assets

| Asset | Where it lives | Primary harm |
| --- | --- | --- |
| API keys, MCP OAuth access/refresh tokens and client secrets | Secrets store (OpenShell gateway credential driver), connector auth service, model gateway | Direct use or disclosure outside the intended endpoint |
| Worker enrollment keys, engine/cluster credentials | Trusted worker/policy infrastructure | Full control of execution hosts |
| Project data and unpublished results | Project store, artifacts, model context | Destruction, unintended disclosure |
| Shared project state used by other sessions | Shared storage, ledger, documents | Silent overwrite or corruption by another agent |
| Policy, monitor configuration and event log | Policy infrastructure | Agent widening its own permissions or hiding activity |
| The user's machine (babbage, macOS) | Local pools | Effects outside the sandbox |
| Compute and paid-API budget | Pools, model gateway, MCP services | Runaway cost or resource exhaustion |
| Approval authority | Web GUI session | Approvals obtained through misleading prompts |

## Threat sources, by priority

| ID | Priority | Source | Examples |
| --- | --- | --- | --- |
| A1 | **Primary** | Agent overreach through tools and MCP | Calls a write/delete MCP tool when the task needed reads; uses a credential for a different purpose at the same endpoint; edits shared documents or ledger entries it was not asked to change |
| A2 | **Primary** | Agent overreach through code in workers | Reaches a non-granted host; tries to read the raw API key; writes outside its workspace; disables or probes the monitor or proxy; starts listeners or background processes that outlive the task |
| A3 | **Primary** | Agent self-escalation | Proposes or pressures for broader policy; writes approval prompts that understate the request; edits policy files it can reach |
| A4 | **Primary** | Runaway work | Fork bombs, unbounded loops, disk filling, large model or paid-API bills |
| T1 | Secondary | Malicious or compromised package installed by the agent | `setup.py`/post-link hook exfiltrates `$HOME`; typosquat |
| T4 | Secondary | Deliberately studied exploit code | Kernel or container-escape proof of concept run to reproduce a paper |
| T5 | Secondary | Compromised hosted control plane | Hosted orchestrator sends commands to a local worker |
| T2 | Lower | Prompt injection via consumed content | A paper or README instructs the agent to upload files or loosen policy |
| T6 | Lower | Malicious artifact aimed at the GUI | Script in an SVG/HTML/notebook output acting in the user's session |
| T7 | Lower | Covert exfiltration over allowed channels | Data in URL query strings, package names or DNS labels |

## Trust zones

| Zone | Components | Trust | Rule |
| --- | --- | --- | --- |
| Z0 Host / cluster control | Node OS, container engine, Kubernetes control plane | Fully trusted | Never shares a kernel with Z3 at tier ≥ 2 |
| Z1 Policy infrastructure | Worker service, OpenShell gateway/supervisor, secrets store, connector auth service, egress proxies, package cache, monitor, event store | Trusted | Holds enforcement and credential material; not agent-writable; reachable from Z3 only via its proxy/API ports |
| Z2 Control application | Orchestrator/harness, project store, model gateway | **Semi-trusted**: trusted state, agent-selected actions | Egress only to the model gateway and MCP endpoints under policy; holds placeholders or gateway-mediated credentials rather than raw keys where possible; no engine/cluster credentials |
| Z3 Research sandboxes | Project/session/batch/review containers, kernels, Conda | Untrusted | No direct internet; egress via Z1 proxies; credentials only as placeholders |
| Z4 Browser | GUI and artifact previews | User-trusted UI, untrusted content | Untrusted artifacts on a separate origin; routine approvals within the session, re-authentication for high-risk actions (D54) |

## Isolation tiers

Each execution records the tier it actually ran at. If a pool cannot provide the tier its policy requires, the work is refused. It never falls back to a weaker tier. The worker checks the tier a backend declares with a probe each time it starts an executor, and evidence can only lower it; no probe from inside a sandbox can prove tier 3 ([worker](WORKER.md#isolation-tiers)).

| Tier | Mechanism | Shares a kernel with Z1/Z2? | Intended use |
| --- | --- | --- | --- |
| 1 | Hardened container (runc, rootless where possible, seccomp default, no capabilities, read-only rootfs) | Yes | Development and trusted fixtures only, with the risk accepted explicitly |
| 2 | Sandboxed runtime (gVisor `runsc`) or a separate VM for all workers | No | Default on the mixed-arch Kubernetes test cluster (gVisor supports amd64 and arm64); macOS where available |
| 3 | Per-sandbox microVM (Kata, Firecracker, OpenShell MicroVM driver) | No | **Default on babbage** (OpenShell MicroVM driver over KVM); cluster nodes that support it; required for T4 workloads |

Docker Desktop and Colima on macOS run every container in one Linux VM. Unless workers run under `runsc`, or in a VM separate from Z1/Z2, a local all-in-Docker deployment is **tier 1**. On babbage, OpenShell needs Landlock ABI ≥ 3 and active kernel qualification. Ubuntu's AppArmor restriction on unprivileged user namespaces affects rootless runtimes, so check it for each candidate.

Performance-sensitive work (timing and side-channel measurements, `perf_event_open`) is usually incompatible with tiers 2–3. It needs an explicit "measurement" policy that documents weaker isolation and is never the default.

## Control mapping

| Threat | Primary controls | Detection |
| --- | --- | --- |
| A1 | All MCP traffic through the MCP proxy (D67) with per-project tool allowlists, re-enforced by `protocol: mcp`; least-privilege, read-only tokens; L7 method/path rules; revision-checked mutations on shared documents/ledger with attribution | MCP and L7 events per tool call; ledger/document history |
| A2 | Default-deny egress via proxies; credentials only as endpoint-bound placeholders (fail-closed); read-only rootfs and declared volumes (deferred, D48); Landlock/filesystem policy; monitor and policy outside the sandbox | `credential_endpoint_mismatch` findings; denied-connection events; Tetragon/Falco process/file events |
| A3 | Agent text never authorizes anything; policy advisor with auto-approval off; prover flags new credentialed reach (at v0.1.3 it cannot check policies that use middleware or MCP endpoints, so for those the compiler and orchestrator check); approval prompts built from observed facts; re-authentication for policy widening and credential changes (D54); local policy ceiling per pool | Pending-decision log; prover findings |
| A4 | CPU/memory/PID/disk/wall-time limits from the policy file; per-project quotas; LiteLLM virtual-key spend limits (D61); for MCP services, the services' own limits and billing: cost control is their job, and escalation is for effects, not cost (owner, 2026-10-14) | Quota and spend alerts |
| T1 | Package egress only through the package cache (D71); lockfiles; rebuild-from-lockfile | Package cache logs; install-phase correlation |
| T4 | Tier 3 isolation; no Z1/Z2 credentials in the sandbox; everything an executor reports is checked, including for the shared-data sync (D79): its digests are never paths, content is kept only if it hashes to its digest, each project has its own blob store, an environment is synced for one project only, and each step has time and byte budgets ([worker](WORKER.md#shared-data-across-environments)) | Runtime security events |
| T5 | Per-pool local policy ceiling; local enrollment needs local user action | Local audit log of remote commands |
| T2 | Same as A1–A3; source content is data, not instructions | Content guard flags instruction-like text |
| T6 | Separate artifact origin, sandboxed iframes, CSP, KaTeX `trust: false`, PDF.js scripting off | CSP violation reports |
| T7 | Default-deny egress; read-only fetch proxy; proxy-side DNS; URL length and rate limits | Proxy logs |

## Residual risks to accept explicitly

- A placeholder still lets an agent do whatever the token permits at its bound endpoint. Token scope and tool/method rules are the real limit.
- OpenShell v0.1.3 resolves static placeholders per endpoint, not per binary, so any process in the sandbox can use an attached credential. In a shared project container this includes other workbooks' processes, so session attribution and session-scoped grants there are not trustworthy without a verified process-to-execution mapping ([MCP connectors](MCP-CONNECTORS.md#mcp-proxy)).
- Sessions that share an environment run as one user in one sandbox. Research code in one session can read the others' files, including their kernels' connection files, and drive their kernels. Only a dedicated session environment separates sessions ([projects and sessions](PROJECTS-AND-SESSIONS.md)).
- Low-bandwidth covert channels through allowed fetches cannot be eliminated, only bounded and logged.
- The shared-data sync (D79) carries what one environment writes in `shared/` into the project's other environments. Research code that controls one environment of a project can therefore put files, within the project's limits (32 MiB each, 2 GiB and 10,000 files in all), into its other environments, as a writable shared directory would. It cannot reach another project's data or the worker's own files.
- The event store's hash chain is not keyed. Whoever can write the store can rewrite its records consistently or remove the newest; only checkpoints kept out of their reach show it ([event store](EVENT-STORE.md)).
- Tier 1 local deployments share a kernel with trusted services.
- Model providers receive project content. Data classes (D50) decide which classes may go to which endpoints.

## Data classes (proposed)

| Class | May go to | May not go to |
| --- | --- | --- |
| Public | Any configured model/MCP service/pool | — |
| Project-private (default) | Configured model providers; user-owned pools and storage | Unapproved third-party endpoints |
| Embargoed | Explicitly approved model endpoints; designated pools | Hosted pools/storage not designated for it; general MCP services |
| Restricted dataset | Designated pool only; computation goes to the data | Model context (summaries only, by policy); any other pool |
