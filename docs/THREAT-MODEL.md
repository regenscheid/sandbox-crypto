# Threat model and isolation tiers

Date: 2026-10-10. Proposed by the plan review ([critical review](../.agents/critical-review.md), finding C1). This draft is for confirmation, not a validated design. It states what the isolation and monitoring controls in [architecture](ARCHITECTURE.md) and [monitoring](MONITORING.md) protect, and from whom.

## Scope and assumptions

- Single trusted user (D15). Multi-tenant hosting is out of scope; adding it would require revisiting every tier below.
- The user deliberately downloads, installs and runs third-party and agent-generated code, including published attack code. Code inside a research sandbox is **untrusted by default**, whatever its origin.
- Model outputs are influenced by untrusted content (papers, web pages, repository files, tool output). Any component whose actions an agent selects is **agent-influenced**, even if it runs trusted code.

## Assets

| Asset | Where it lives | Primary harm |
| --- | --- | --- |
| Model/provider API keys, connector credentials | Model gateway, connector broker | Theft, billing abuse, impersonation |
| Worker enrollment keys, engine/cluster credentials | Trusted worker/policy infrastructure | Full control of execution hosts |
| Unpublished results, embargoed vulnerability findings | Project store, artifacts, model context | Premature disclosure |
| Sensitive datasets (real keys, captured traffic, licensed data) | Project storage, enrolled pool-local data | Exfiltration, license breach |
| Licensed tool entitlements (e.g. Magma) | Specific pools | License breach |
| The user's local machine (hosted-control/local-compute) | Local pool | Remote code execution beyond the sandbox |
| Monitor policy, event log and evidence | Monitor/event store | Undetected misuse, falsified history |
| Approval authority | Web GUI session | Self-approved exfiltration |

## Threat sources

| ID | Source | Example |
| --- | --- | --- |
| T1 | Malicious or compromised package | `setup.py`/post-link hook exfiltrates `$HOME`; typosquat on conda-forge/PyPI |
| T2 | Prompt injection via content | A PDF or README instructs the agent to upload project files or loosen policy |
| T3 | Faulty agent-generated code | Deletes shared data, fork-bombs, fills storage |
| T4 | Deliberately studied exploit code | Kernel or container-escape proof of concept run "to reproduce a paper" |
| T5 | Compromised control plane | Hosted orchestrator sends commands to a local worker |
| T6 | Malicious artifact aimed at the GUI | SVG/HTML/notebook output with script that acts in the user's session |
| T7 | Covert exfiltration over allowed channels | Data in URL query strings, package names, DNS labels, git request bodies |

## Trust zones

| Zone | Components | Trust | Rule |
| --- | --- | --- | --- |
| Z0 Host / cluster control | Node OS, container engine, Kubernetes control plane | Fully trusted | Never shares a kernel with Z3 at tier ≥ 2 |
| Z1 Policy infrastructure | Worker service, OpenShell gateway/supervisor, egress proxies, package mirror, monitor, event store | Trusted | Holds enforcement credentials; not agent-writable; not reachable from Z3 except via its proxy/API ports |
| Z2 Control application | Orchestrator/harness, project store, model gateway, connector broker | **Semi-trusted**: trusted state, agent-influenced actions | Egress limited to the model gateway and connector broker; connector fetches subject to the same egress policy as workers; no engine/cluster credentials |
| Z3 Research sandboxes | Project/session/batch/review containers, kernels, Conda | Untrusted | No direct internet; egress only via Z1 proxies; no credentials beyond scoped endpoint tokens |
| Z4 Browser | GUI and artifact previews | User-trusted UI, untrusted content | Untrusted artifacts rendered on a separate origin; approvals require fresh authentication |

## Isolation tiers

Each execution records the tier it actually ran at. If a pool cannot provide the tier its policy requires, the work is refused. It never falls back to a weaker tier.

| Tier | Mechanism | Shares a kernel with Z1/Z2? | Intended use |
| --- | --- | --- | --- |
| 1 | Hardened container (runc, rootless where possible, seccomp default, no capabilities, read-only rootfs) | Yes | Local development and trusted fixtures only, with the risk accepted explicitly |
| 2 | Sandboxed runtime (gVisor `runsc`) or a separate VM for all workers | No (gVisor intercepts syscalls; a VM has its own kernel) | Default for local profiles where available |
| 3 | Per-sandbox microVM (Kata, Firecracker, OpenShell MicroVM driver) | No | Default for hosted Kubernetes; required for T4 workloads |

Docker Desktop and Colima on macOS run every container in one Linux VM. Unless workers run under `runsc`, or in a VM separate from Z1/Z2, a local all-in-Docker deployment is **tier 1**.

Performance-sensitive work (timing and side-channel measurements, `perf_event_open`) is usually incompatible with tiers 2–3. It needs an explicit "measurement" policy that documents weaker isolation and is never the default.

## Control mapping

| Threat | Primary controls | Detection |
| --- | --- | --- |
| T1 | Mirror-only package egress; read-only rootfs plus declared writable volumes; lockfiles; rebuild-from-manifest | Mirror miss log; Tetragon/Falco process/file events; monitor install-phase correlation |
| T2 | Agent text never authorizes anything; scoped grants; Z2 egress limits; approval prompts built from observed facts | Contextual monitor; content guard flags instruction-like source text |
| T3 | Resource limits, quotas, session workspace separation, versioned publication | Execution events, quota alerts |
| T4 | Tier 3 isolation; no Z1/Z2 credentials in the sandbox | Runtime security events |
| T5 | Per-pool local policy ceiling; remote control can only narrow it; local enrollment needs local user action | Local audit log of remote commands |
| T6 | Separate artifact origin, sandboxed iframes, CSP, KaTeX `trust: false`, PDF.js scripting off | CSP violation reports |
| T7 | Default-deny egress; read-only fetch proxy (git `upload-pack` only); proxy-side DNS; URL length and rate limits | Mirror/proxy logs; contextual monitor; volume anomalies |

## Residual risks to accept explicitly

- Low-bandwidth covert channels through allowed fetches (T7) cannot be eliminated. They can only be bounded and logged.
- Content inspection cannot tell exfiltrated key material from legitimate cryptographic output.
- Tier 1 local deployments share a kernel with trusted services.
- Model providers receive project content. Data classes ([decisions](DECISIONS.md) D50) decide which classes may go to which endpoints.

## Data classes (proposed)

| Class | May go to | May not go to |
| --- | --- | --- |
| Public | Any configured model/connector/pool | — |
| Project-private (default) | Configured model providers; user-owned pools and storage | Unapproved third-party endpoints |
| Embargoed | Explicitly approved model endpoints; designated pools | Hosted pools/storage not designated for it; general connectors |
| Restricted dataset | Designated pool only; computation goes to the data | Model context (summaries only, by policy); any other pool |
