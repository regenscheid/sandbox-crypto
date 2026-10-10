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
- [ ] Validate the compiled policies against a real OpenShell v0.1.3 gateway on babbage (`openshell policy set --wait`, the prover's boundary check); so far they are checked only against the documented schema
- [x] Credential handling: OpenShell providers inject credentials after the policy middleware decides (D57, D69)
- [ ] Escalation grants: record approvals as scoped grants that the policy middleware and MCP proxy honour, and compile them into the MCP proxy's fence
- [x] Wildcard-host read rules: `*` cannot be an OpenShell endpoint and `upstreamProxy` only chains TLS tunnels, so generic HTTP goes through the project egress proxy
- [ ] Policy revisions: GUI and file edits, re-authentication for widening (D54)
- [ ] `defaults.log`: decide whether allowed operations may go unlogged

### Other track P items

- [ ] Worker API schema and worker service
- [ ] Read-only-rootfs environment layout, lockfile capture and rebuild (D48)
- [ ] Policy middleware service (`packages/policy-middleware`): an OpenShell supervisor middleware (gRPC, `EvaluateHttpRequest`) that maps the sandbox to its project policy, checks the policy digest from its config, classifies requests with the library (Content-Type, body presence, pkt-line bodies for git), returns allow or deny with `approval_required` for escalations, and records findings (D69)
- [ ] Package mirror (D44), the only route to package indexes
- [ ] On babbage: verify the middleware path (host-less public endpoint and DNS, bodies over the payload limit, WebSocket upgrades, provider-admitted traffic passing the middleware, latency)
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

- [ ] Install OpenShell v0.1.3 and run its kernel qualification outside agent shells
- [ ] Exercise the VM driver (experimental at v0.1.3) on KVM

## Repository

- [x] Development conventions, uv workspace, CI with license check
- [x] Plan snapshot tags
- [x] Fix doc drift: IMPLEMENTATION-PLAN's harness default contradicts D61; ROADMAP M3 says hosted targets were established in M1 (PR #4)
- [ ] Delete the paused `impl/p-policy-engine` branch and the merged `impl/p-policy` and `docs/plan-drift` branches (user's call)
