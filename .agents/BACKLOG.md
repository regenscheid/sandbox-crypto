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
- [x] Validate the compiled policies against a real OpenShell v0.1.3 gateway on babbage: the research sandbox's policy (lab 1), and the MCP proxy's policy, loaded with its provider and its rules exercised ([lab 2](docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)). The v0.1.3 prover cannot check either, since it reports policies with middleware or MCP endpoints as unsupported
- [x] Name every public host the policy mentions beside the host-less endpoint, and compile `*.suffix` to `**.suffix`: a sandbox resolves only names an endpoint names (D69, amended after the babbage lab)
- [ ] On-demand admission: when OpenShell drafts a policy advisor proposal for a blocked host, the orchestrator approves it if the library could allow a request to that host, so `*` rules and the default reach unnamed hosts (D69). The v0.1.3 prover cannot check these policies, so the orchestrator also checks that a proposal adds no credentialed reach
- [x] Credential handling: OpenShell providers inject credentials after the policy middleware decides (D57, D69); confirmed in lab 2
- [x] Provider credentials in research sandboxes (D57 amended; the user chose the arrangement after [lab 2](docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)): `credentials.providers[].hosts` names where each credential may go. The compiler binds it there with `credential_binding` on port 443, providers use profiles without endpoints, and the middleware treats only those hosts as credentialed. Tests check the compiled policies with a port of OpenShell's ambiguity check
- [x] On babbage: run the compiled credential bindings, including a bound host that rules also name (its other egress endpoint then keeps only port 80). Done 2026-10-11 ([lab 3](docs/agent-reports/babbage-openshell-lab-3-2026-10-11.md)); the plain-HTTP substitution bug was seen at runtime
- [ ] Orchestrator: attach to each research sandbox exactly the providers `credentials.providers` lists, each with a profile without endpoints. The middleware counts only those hosts as credentialed, and OpenShell would bind any other provider to its profile's endpoints
- [ ] Escalation grants: record approvals as scoped grants that the policy middleware and MCP proxy honour, and compile them into the MCP proxy's fence
- [x] Wildcard-host read rules: `*` cannot be an OpenShell endpoint, so the compiler admits public address ranges with a host-less endpoint and the policy middleware decides each request (D69)
- [ ] Policy revisions: GUI and file edits, re-authentication for widening (D54)
- [ ] `defaults.log`: decide whether allowed operations may go unlogged

### Other track P items

- [x] Worker API schema and worker service, first version ([worker](../docs/WORKER.md)): idempotent executions with numbered, replayable events, cancellation, watchdogs, kernels through the Jupyter protocol, files and publication; local (tier 0, tests only) and OpenShell backends
- [ ] Worker service: an executor daemon in the sandbox that survives worker restarts; spilling large rich outputs to files; data grants and session containers
- [ ] Read-only-rootfs environment layout, lockfile capture and rebuild (D48)
- [x] Policy middleware service (`packages/policy-middleware`, D70): an OpenShell supervisor middleware (gRPC) that decides each request under the revision its sandbox is bound to, verifies extension tokens, classifies requests with the library (Content-Type, body presence, upgrades), checks git fetch bodies, denies WebSockets at preflight, returns reason codes (`approval_required` for escalations) and records escalations, alerts and monitor findings. Checked only against the vendored contract so far
- [ ] Orchestrator: write the middleware's policy store and sandbox bindings when it creates sandboxes and revises policies (the `bind` command stands in until then)
- [x] Package policy (D71): `packages` covers PyPI, conda, Cargo, Go and npm; the library classifies requests to the package cache; the compiler puts the cache behind the policy middleware; registries are on the floor for generic HTTP
- [x] Package cache service (`packages/package-cache`, D71): pull-through, size-capped, verifying, for the five ecosystems ([package cache](../docs/PACKAGE-CACHE.md)). Real pip, uv, mamba, cargo, go and npm install through it on loopback (`test_clients_integration.py`)
- [x] On babbage: the package cache through OpenShell, with the compiled policy and real clients in a sandbox. Done 2026-10-10: micromamba, pip, uv, npm, go and cargo installed through the middleware and the cache, toolchains from conda-forge ([lab report](docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)). Cargo needs `http.multiplexing = false` while the cache is plain HTTP
- [ ] Package cache: lockfiles written in a sandbox name the cache (npm's `resolved` links, `uv.lock`), so they fail elsewhere. Choose between handing out upstream links where the client maps them to the configured registry (npm does, pnpm does not) and rewriting lockfiles when work leaves the sandbox (lab 2)
- [ ] Package cache: opam and Hackage adapters (EasyCrypt, Jasmin, ProVerif, Cryptol, Tamarin)
- [ ] Package cache: a cooling-off period for new releases, keeping lockfile files from eviction, TLS between OpenShell and the cache
- [ ] Package cache: per-sandbox rate limits. The policy middleware writes a sandbox header on cache requests (overwriting anything the sandbox sent), which the cache uses to give each sandbox its own budget; the cooling-off period needs the same mechanism
- [x] Describe OpenShell v0.1.3's seccomp restrictions on thread scheduling and process-group signals, and libzmq's abort on EPERM, in `.agents/upstream/` (lab 4; filing is the user's call)
- [x] Describe the OpenShell v0.1.3 bugs we found, in `.agents/upstream/`: credentials substituted into plain HTTP and sent unencrypted; a host with two trailing dots selects no middleware. The user chose to keep the descriptions here; filing them upstream remains the user's call
- [ ] Environment layer: write the package clients' configuration (pip, conda, Cargo, Go, npm) into each sandbox
- [x] On babbage: verify the middleware path. Done 2026-10-10 against a real gateway: registration, tokens, reads, escalations, WebSockets, git, bodies over the limit, failing closed, latency ([lab report](docs/agent-reports/babbage-openshell-lab-2026-10-10.md)). Provider-admitted traffic checked in lab 2: the middleware decides it, and the floor holds
- [ ] MCP proxy: forward only tool names the server advertises, exactly as advertised; bound argument sizes; send `MCP-Protocol-Version` on every request after `initialize`, with credential placeholders only in headers (lab 2)
- [x] Event store with an append-only hash chain (`packages/event-store`, D72): SQLite with durable, group commits; SHA-256 chain; `sandbox-science-events verify`, `checkpoint` and `export`. The policy middleware records there instead of `events.jsonl`
- [ ] Event store checkpoints: take them regularly and keep them where the store's writers cannot change them (orchestrator or a timer)
- [ ] Adversarial fixture suite
- [ ] Isolation-tier probes (D43)
- [ ] Multi-arch base images: `minimal` and the `crypto-base` recipe (D46); babbage MicroVM gateway configuration (D63)
- [ ] Secrets through OpenShell providers (D57); MCP endpoint policies (D58); connector auth service and MCP proxy (D66, D67)

## W0: behavior specification and harness foundation

- [ ] Behavior specs with acceptance fixtures: tools ([drafted](../docs/specs/tools.md)), skills, plans and delegation, automatic review, Conda provisioning, artifacts; all to review with the user
- [ ] Harness building-block decision with licenses recorded

## W1: shared references and one worker adapter

- [x] `contracts` package (`ExecutionContext`, `ArtifactRef`, `Event`, the worker API types)
- [x] First slice: one shell command and two stateful Python cells through the worker API, driven by the harness's tool loop with a scripted model. Done locally and on babbage in an OpenShell VM sandbox ([lab 4](docs/agent-reports/babbage-openshell-lab-4-2026-10-11.md)), after working around two OpenShell seccomp restrictions (scheduler shim, process-group signals)
- [ ] The first slice with a real model through LiteLLM (needs the user's model gateway and a virtual key)

## babbage

- [x] Install OpenShell v0.1.3: release binaries in `~/.local/opt/openshell-0.1.3/bin`, checked against the published SHA-256 checksums, GitHub's digests and the release workflow's build attestations
- [x] OpenShell's kernel qualification: it runs inside each sandbox (the guest kernel, for the VM driver) and passed; no host-side check needs user namespaces (lab, 2026-10-10)
- [x] Exercise the VM driver (experimental at v0.1.3) on KVM: gateway and sandboxes ran from a systemd user unit (lab, 2026-10-10)

## Repository

- [x] Development conventions, uv workspace, CI with license check
- [x] Plan snapshot tags
- [x] Fix doc drift: IMPLEMENTATION-PLAN's harness default contradicts D61; ROADMAP M3 says hosted targets were established in M1 (PR #4)
- [ ] Delete the paused `impl/p-policy-engine` branch and the merged `impl/p-policy` and `docs/plan-drift` branches (user's call)
