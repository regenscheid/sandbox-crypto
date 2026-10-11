# Backlog

Working checklist by work package ([implementation plan](../docs/IMPLEMENTATION-PLAN.md)). Tick items in the pull request that completes them.

## Track P: infrastructure

### Policy (`packages/policy`)

- [x] Policy file schema (`sandbox-science/v0alpha1`) with fail-closed validation
- [x] Safe YAML loading: duplicate keys, aliases and merge keys rejected; size and depth limits; SHA-256 revision digest
- [x] Request classification (git smart-HTTP; ambiguous requests denied) and MCP message classification
- [x] Hard floor, pool ceiling (hosts, MCP servers, resource caps) and project-policy evaluation
- [x] Monitors (egress threshold)
- [x] Example policy validated by tests; crypto-research-mcp tool outcomes pinned by a fixture
- [ ] Data-class limits in the pool ceiling (D50)
- [x] OpenShell v0.1.3 compiler (D69): admission endpoints and the policy middleware for research sandboxes, and the MCP proxy's `protocol: mcp` policy, with findings for everything not expressed exactly. Filesystem policy belongs to the environment layer (D48)
- [ ] Validate the compiled policies against a real OpenShell v0.1.3 gateway on babbage. Done for the research sandbox's policy (loaded and enforced, 2026-10-10); still to do: the MCP proxy's policy and the prover's boundary check
- [x] Name every public host the policy mentions beside the host-less endpoint, and compile `*.suffix` to `**.suffix`: a sandbox resolves only names an endpoint names (D69, amended after the babbage lab)
- [ ] On-demand admission: when OpenShell drafts a policy advisor proposal for a blocked host, the orchestrator approves it if the library could allow a request to that host, so `*` rules and the default reach unnamed hosts (D69)
- [x] Credential handling: OpenShell providers inject credentials after the policy middleware decides (D57, D69)
- [ ] Escalation grants: record approvals as scoped grants that the policy middleware and MCP proxy honour, and compile them into the MCP proxy's fence
- [x] Wildcard-host read rules: `*` cannot be an OpenShell endpoint, so the compiler admits public address ranges with a host-less endpoint and the policy middleware decides each request (D69)
- [ ] Policy revisions: GUI and file edits, re-authentication for widening (D54)
- [ ] `defaults.log`: decide whether allowed operations may go unlogged

### Other track P items

- [ ] Worker API schema and worker service
- [ ] Read-only-rootfs environment layout, lockfile capture and rebuild (D48)
- [x] Policy middleware service (`packages/policy-middleware`, D70): an OpenShell supervisor middleware (gRPC) that decides each request under the revision its sandbox is bound to, verifies extension tokens, classifies requests with the library (Content-Type, body presence, upgrades), checks git fetch bodies, denies WebSockets at preflight, returns reason codes (`approval_required` for escalations) and records escalations, alerts and monitor findings. Checked only against the vendored contract so far
- [ ] Orchestrator: write the middleware's policy store and sandbox bindings when it creates sandboxes and revises policies (the `bind` command stands in until then)
- [ ] Package mirror (D44), the only route to package indexes
- [x] On babbage: verify the middleware path. Done 2026-10-10 against a real gateway: registration, tokens, reads, escalations, WebSockets, git, bodies over the limit, failing closed, latency ([lab report](docs/agent-reports/babbage-openshell-lab-2026-10-10.md)). Provider-admitted traffic is still to check
- [ ] MCP proxy: forward only tool names the server advertises, exactly as advertised; bound argument sizes
- [ ] Event store with an append-only hash chain
- [ ] Adversarial fixture suite
- [ ] Isolation-tier probes (D43)
- [ ] Multi-arch base images: `minimal` and the `crypto-base` recipe (D46); babbage MicroVM gateway configuration (D63)
- [ ] Secrets through OpenShell providers (D57); MCP endpoint policies (D58); connector auth service and MCP proxy (D66, D67)

## W0: behavior specification and harness foundation

- [ ] Behavior specs with acceptance fixtures: tools, skills, plans and delegation, automatic review, Conda provisioning, artifacts
- [ ] Harness building-block decision with licenses recorded

## W1: shared references and one worker adapter

- [ ] `contracts` package (`ExecutionContext`, `ArtifactRef`, `Event`, ...)
- [ ] First slice: one shell command and two stateful Python cells through the worker API

## babbage

- [x] Install OpenShell v0.1.3: release binaries in `~/.local/opt/openshell-0.1.3/bin`, checked against the published SHA-256 checksums, GitHub's digests and the release workflow's build attestations
- [x] OpenShell's kernel qualification: it runs inside each sandbox (the guest kernel, for the VM driver) and passed; no host-side check needs user namespaces (lab, 2026-10-10)
- [x] Exercise the VM driver (experimental at v0.1.3) on KVM: gateway and sandboxes ran from a systemd user unit (lab, 2026-10-10)

## Repository

- [x] Development conventions, uv workspace, CI with license check
- [x] Plan snapshot tags
- [x] Fix doc drift: IMPLEMENTATION-PLAN's harness default contradicts D61; ROADMAP M3 says hosted targets were established in M1 (PR #4)
- [ ] Delete the paused `impl/p-policy-engine` branch and the merged `impl/p-policy` and `docs/plan-drift` branches (user's call)
