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
- [x] On-demand admission (D69, amended after [lab 10](docs/agent-reports/babbage-openshell-lab-10-2026-10-11.md)): the worker reads OpenShell's drafts for refused connections. When the library could allow or escalate a request to the host, it names the host in a new compilation of the same revision, then rejects the draft with its decision. Admissions never change credential bindings. OpenShell drafts proposals only for connections its own rules refuse, so the compiler no longer emits a host-less endpoint
- [x] Admission: tell the agent when a host it tried has been admitted or refused: results carry notices since the session's previous result, which the harness shows the model
- [x] Admission: let the orchestrator ask the worker to admit hosts before code runs, so that fewer first connections fail and fewer reloads interrupt transfers. `POST /v1/environments/{id}/admissions`, the `admission` capability and the agent's `admit_hosts` tool. On babbage, an admitted host was reachable on the first connection ([lab 14](docs/agent-reports/babbage-openshell-lab-14-2026-10-11.md))
- [x] Give the worker the deployment's `admin_hosts`, so that its compiler and admission apply the same hard floor as the policy middleware
- [x] Credential handling: OpenShell providers inject credentials after the policy middleware decides (D57, D69); confirmed in lab 2
- [x] Provider credentials in research sandboxes (D57 amended; the user chose the arrangement after [lab 2](docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)): `credentials.providers[].hosts` names where each credential may go. The compiler binds it there with `credential_binding` on port 443, providers use profiles without endpoints, and the middleware treats only those hosts as credentialed. Tests check the compiled policies with a port of OpenShell's ambiguity check
- [x] On babbage: run the compiled credential bindings, including a bound host that rules also name (its other egress endpoint then keeps only port 80). Done 2026-10-11 ([lab 3](docs/agent-reports/babbage-openshell-lab-3-2026-10-11.md)); the plain-HTTP substitution bug was seen at runtime
- [x] Attach to each research sandbox exactly the providers `credentials.providers` lists, each with a profile without endpoints: the worker does it when it creates the sandbox (D73; the threat model keeps gateway credentials out of the orchestrator). To verify on babbage
- [x] Escalation grants in the policy middleware (D76): escalations are permission requests in the event store, answered with `approve` or `deny` (request or host scope, with an expiry); retries are recognized and not recorded again; escalation timeouts apply on retry
- [x] Permission requests in the MCP proxy (D76): escalated calls are recorded once and answered with `sandbox-science-mcp-proxy approve` or `deny` (call or tool scope)
- [ ] Compile approved tools into the MCP proxy sandbox's fence; approvals from the GUI with re-authentication (D54)
- [x] Wildcard-host read rules: `*` cannot be an OpenShell endpoint, so the compiler admitted public address ranges with a host-less endpoint and the policy middleware decided each request (D69). Replaced after lab 10 by admission on demand
- [ ] Policy revisions: GUI and file edits, re-authentication for widening (D54)
- [ ] `defaults.log`: decide whether allowed operations may go unlogged

### Other track P items

- [x] Worker API schema and worker service, first version ([worker](../docs/WORKER.md)): idempotent executions with numbered, replayable events, cancellation, watchdogs, kernels through the Jupyter protocol, files and publication; local (tier 0, tests only) and OpenShell backends
- [ ] Worker service: an executor daemon in the sandbox that survives worker restarts; spilling large rich outputs to files; data grants and session containers
- [x] Worker service: per-project credentials, so that the `project_id` a request names is access control and not only a consistency check (adversarial review of 2026-10-11, "no project scoping"). Clients limited to projects (D78): their tokens are named by SHA-256 in the settings, other projects' executions, artifacts and environments are `not_found`, they use only environments the worker created for their projects and revisions granted to them (`policy add --project`), environment IDs stay with their first project, and operation IDs are kept per project ([worker](../docs/WORKER.md#clients-limited-to-projects)). Reviewed adversarially twice; the reviews' three high findings (a check that no longer held when the request ran, sandboxes made by hand, sandbox names another ID could share) and the medium one (other projects' revisions) are fixed
- [ ] Worker service: environment IDs per project. They are shared by the pool and stay with the first project that creates them, so a client limited to one project can take an ID another project would use. The harness's IDs carry a random tag, which other orchestrators cannot predict
- [x] Worker service: a limit per client on environments (`max_environments`, 8 by default), so that no client fills the pool's host with VMs
- [ ] Worker service: limits per client on executions and requests (review of D78)
- [x] On babbage: the executor's hardening under OpenShell's seccomp filter ([lab 8](docs/agent-reports/babbage-openshell-lab-8-2026-10-11.md)). Both `prctl` calls are allowed and leftover processes end. A process that is not dumpable loses its sockets there, so the executor stays dumpable under OpenShell and `/proc/<executor>/fd` stays open to research code in the same environment
- [x] Worker service: close a session's kernels (`POST /v1/sessions/close`); per-session kernels otherwise accumulate in a long-lived environment (lab 8)
- [x] Worker service: stop kernels unused for longer than `kernel_idle_timeout` (off by default)
- [x] Idle suspension (W3): with `suspend_after`, the worker stops the sandbox of an environment with no executions, no requests and no kernel CPU use, and starts it again on the next use. Files, installed tools, the policy and admitted hosts survive; kernels are reported lost ([lab 11](docs/agent-reports/babbage-openshell-lab-11-2026-10-11.md))
- [x] The orchestrator keeps project records and creates environments itself (W3): `sandbox-science-harness project create|show|revise`, `run --state-dir [--dedicated]` and `session close`. Sessions share the project's default environment, each with its own kernels and workspace, or get a dedicated one from the same base profile ([projects and sessions](../docs/PROJECTS-AND-SESSIONS.md#as-built-the-harnesss-project-records))
- [ ] The orchestrator closes sessions when they end, without an explicit command (needs the session lifecycle of the control application, W4)
- [ ] Delegations that opt into an environment of their own, made from the project's base image, for work that needs isolation, such as an independent check (D81; agreed with the user on 2026-10-11 as a later feature). By default delegated runs stay in their session's environment, with kernels of their own (PD-14); the worker keys kernels by session today, so per-run kernels need the execution context to name the run
- [x] Environment revisions: count package changes in a shared environment and report the revision each execution started and ended under (W3). Changes are recorded, not yet serialized
- [ ] Read-only-rootfs environment layout, lockfile capture and rebuild (D48): deferred by the user on 2026-10-11; sandboxes keep a writable root filesystem for now
- [x] Shared data across a project's environments (D79): the worker copies changed files in `shared/` between the project's environments before and after each tool call, keeping a conflict copy when two changed the same file ([worker](../docs/WORKER.md#shared-data-across-environments)); hardened after its review and run on babbage ([lab 16](docs/agent-reports/babbage-openshell-lab-16-2026-10-11.md))
- [x] Conda provisioning, first slice: package commands from shell calls and kernels take turns through wrappers and a hook, new executions wait while packages link, and every change is a revision with a lockfile (CP-4, CP-11, CP-12, CP-16, CP-18); reviewed three times and run on babbage ([lab 17](docs/agent-reports/babbage-openshell-lab-17-2026-10-11.md))
- [ ] Conda provisioning, the provisioning tool, as the approved spec drafts it: requests, catalog, decisions through the package cache, dry runs, permission requests, the AGPL refusal, probes, rollback, limits and failure records (CP-1 to CP-3, CP-5, CP-7 to CP-10, CP-13 to CP-15, CP-17, CP-20 to CP-26); a kernel's own start revision and the visible queue (CP-11, CP-12)
- [ ] Conda provisioning: never drop a lockfile that a recorded result's lineage names. The user accepted the history's caps on 2026-10-11 (each environment keeps its newest 500 revisions, and lockfiles for its newest 50 within 64 MiB) with this rule to come; it needs results to record their lineage ([research artifacts](../docs/specs/research-artifacts.md), [Conda provisioning](../docs/specs/conda-provisioning.md) CP-16 and CP-20)
- [x] Policy middleware service (`packages/policy-middleware`, D70): an OpenShell supervisor middleware (gRPC) that decides each request under the revision its sandbox is bound to, verifies extension tokens, classifies requests with the library (Content-Type, body presence, upgrades), checks git fetch bodies, denies WebSockets at preflight, returns reason codes (`approval_required` for escalations) and records escalations, alerts and monitor findings. Checked only against the vendored contract so far
- [x] Write the middleware's policy store and sandbox bindings when sandboxes are created and revised: the worker's environments (D73), with `sandbox-science-worker policy add` as the operator's way in. To verify on babbage
- [ ] Policy revisions from the GUI: an approval path into the pool's policy store, with re-authentication for widening (D54)
- [x] Package policy (D71): `packages` covers PyPI, conda, Cargo, Go and npm; the library classifies requests to the package cache; the compiler puts the cache behind the policy middleware; registries are on the floor for generic HTTP
- [x] Package cache service (`packages/package-cache`, D71): pull-through, size-capped, verifying, for the five ecosystems ([package cache](../docs/PACKAGE-CACHE.md)). Real pip, uv, mamba, cargo, go and npm install through it on loopback (`test_clients_integration.py`)
- [x] On babbage: the package cache through OpenShell, with the compiled policy and real clients in a sandbox. Done 2026-10-10: micromamba, pip, uv, npm, go and cargo installed through the middleware and the cache, toolchains from conda-forge ([lab report](docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)). Cargo needs `http.multiplexing = false` while the cache is plain HTTP
- [ ] Package cache: lockfiles written in a sandbox name the cache (npm's `resolved` links, `uv.lock`), so they fail elsewhere. Choose between handing out upstream links where the client maps them to the configured registry (npm does, pnpm does not) and rewriting lockfiles when work leaves the sandbox (lab 2)
- [ ] Package cache: opam and Hackage adapters (EasyCrypt, Jasmin, ProVerif, Cryptol, Tamarin)
- [ ] Package cache: a cooling-off period for new releases, keeping lockfile files from eviction, TLS between OpenShell and the cache
- [x] Package cache: per-sandbox budgets of upstream fetches. The policy middleware writes the sandbox's ID in a header on every cache request it allows, replacing anything the sandbox sent, and the cache keeps a token bucket per sandbox besides the shared one ([package cache](../docs/PACKAGE-CACHE.md#logging-and-limits)). On babbage, OpenShell 0.1.3 applies the header and replaces a forged one ([lab 17](docs/agent-reports/babbage-openshell-lab-17-2026-10-11.md))
- [x] Describe OpenShell v0.1.3's seccomp restrictions on thread scheduling and process-group signals, and libzmq's abort on EPERM, in `.agents/upstream/` (lab 4; filing is the user's call)
- [x] Describe the OpenShell v0.1.3 bugs we found, in `.agents/upstream/`: credentials substituted into plain HTTP and sent unencrypted; a host with two trailing dots selects no middleware. The user chose to keep the descriptions here; filing them upstream remains the user's call
- [x] Environment layer: write the package clients' configuration (pip, conda, Cargo, Go, npm) into each sandbox. The OpenShell backend's `package_cache` setting; the executor writes the files and sets the variables, keeping files someone changed ([client configuration](../docs/PACKAGE-CACHE.md#client-configuration)). Real pip, uv, conda, mamba, cargo, go and npm ran with it against a loopback cache (`test_package_clients_integration.py`)
- [ ] On babbage: the worker's package client configuration in an OpenShell sandbox, with clients run without registry options through the middleware and the cache. pip and micromamba done ([lab 17](docs/agent-reports/babbage-openshell-lab-17-2026-10-11.md)); uv, Cargo, Go and npm, which neither image carries, once installed from conda-forge; opam and cabal once the cache serves them
- [x] On babbage: verify the middleware path. Done 2026-10-10 against a real gateway: registration, tokens, reads, escalations, WebSockets, git, bodies over the limit, failing closed, latency ([lab report](docs/agent-reports/babbage-openshell-lab-2026-10-10.md)). Provider-admitted traffic checked in lab 2: the middleware decides it, and the floor holds
- [x] MCP proxy: forward only tool names the server advertises, exactly as advertised; bound argument sizes; send `MCP-Protocol-Version` on every request after `initialize`, with credential placeholders only in headers (lab 2). Done in the MCP proxy's first version (`packages/mcp-proxy`, D75), tested against a fake server
- [x] MCP proxy in its OpenShell sandbox on babbage ([lab 13](docs/agent-reports/babbage-openshell-lab-13-2026-10-11.md)): OpenShell substituted the provider's token in its headers, and its version header and replies to server requests passed the fence. Names resolve to synthetic addresses there, so the compiled endpoints now carry the address checks (`allowed_ips`). The fence's refusals are reported as `fence_denied`
- [x] MCP proxy image: the proxy runs as its sandbox's command, through a launcher that restarts it, and comes back after a stop and start ([lab 15](docs/agent-reports/babbage-openshell-lab-15-2026-10-11.md))
- [ ] MCP proxy: export its event store's records from its VM to the monitor, and give approvals a way in other than `openshell sandbox exec`. A user unit that restarts `openshell forward service` is in `examples/systemd`; lab 17 checked it across a stop and start
- [x] The orchestrator's sandbox policy admits the MCP proxy: `orchestrator.mcp_proxy` in the targets file (lab 13)
- [ ] Connector auth service (D66): OAuth with DCR for crypto-research-mcp, refresh, and `403 insufficient_scope` as an escalation; then an `SS_INTEGRATION=1` test of the MCP proxy against crypto-research-mcp
- [ ] MCP proxy: binary results as artifact references (withheld for now); rate and spend limits per project and server
- [ ] MCP proxy: MCP 2026-07-28 (no sessions, metadata in every request, `Mcp-Method` and `Mcp-Name` headers, server requests embedded in results), and the policy's protocol method list for it
- [ ] MCP proxy: pass server notifications (progress, logging, list changes) to callers, and resume interrupted event streams
- [ ] MCP proxy callers: identity from the policy middleware, which knows the sandbox, instead of per-sandbox tokens; `process`-level attribution (D67)
- [x] One policy store library for the policy middleware, the worker and the MCP proxy: `sandbox_science.policy.store`, with sandbox bindings for the middleware and project bindings for the proxy. The worker no longer depends on the middleware package
- [x] Event store with an append-only hash chain (`packages/event-store`, D74): SQLite with durable, group commits; SHA-256 chain; `sandbox-science-events verify`, `checkpoint` and `export`. The policy middleware records there instead of `events.jsonl`
- [x] Event store checkpoints: a systemd user timer template logs a checkpoint every five minutes to the journal, whose files the user's processes cannot rewrite, and `verify` reads a journal export of them ([event store](../docs/EVENT-STORE.md#checkpoints-in-the-journal)). Sending checkpoints off the host remains
- [ ] Event store growth: rotate old records (D74). Repeated escalations are no longer recorded again (D76)
- [ ] Adversarial fixture suite
- [x] Isolation-tier probes (D43): the worker probes each executor it starts and reports the tier in effect beside the declared one; evidence only lowers it, and the OpenShell backend must declare its tier ([worker](../docs/WORKER.md#isolation-tiers))
  - [x] Backend conformance suite (D52): the execution-tools fixtures through the worker API (`packages/worker/tests/test_conformance.py`), against the local backend in CI or a running worker named by `SS_CONFORMANCE_*` ([worker](../docs/WORKER.md#conformance-suite))
  - [ ] Run the suite, and with it the probe, against an OpenShell sandbox on babbage (lab)
- [ ] Multi-arch base images: `minimal` and the `crypto-base` recipe (D46); babbage MicroVM gateway configuration (D63)
  - [x] Recipes for both profiles in `images/` (D77, [images](../docs/IMAGES.md)): pinned sources, Conda lockfiles for linux-64 and linux-aarch64, build and test scripts. Built for linux/amd64 on babbage and checked as plain containers: read-only root, uid 1000, and under a Landlock ruleset like OpenShell's
  - [ ] arm64 images: build and test on an arm64 host; check that the Ubuntu snapshot service covers the ports archive; a registry for multi-platform images (D63)
  - [ ] Run the images in an OpenShell lab: the worker's profiles, the targets file's `sandbox.filesystem` ([images](../docs/IMAGES.md#openshell-filesystem-paths)) and the conformance suite
  - [ ] A private registry for crypto-base (open question), and a standard-format SBOM before `minimal` is published
- [ ] Secrets through OpenShell providers (D57); MCP endpoint policies (D58); connector auth service and MCP proxy (D66, D67)

## W0: behavior specification and harness foundation

- [ ] Behavior specs with acceptance fixtures: Conda provisioning, research artifacts and automatic review approved by the user (2026-10-11); tools ([drafted](../docs/specs/tools.md)), skills, plans and delegation, PDF sources, living documents ([drafted](../docs/specs/living-documents.md)) and the project ledger, board and frontier ([drafted](../docs/specs/ledger.md)) to review with the user; the two drafts record the user's decisions of 2026-10-11 on their main questions
- [x] Harness building-block decision with licenses recorded: D72, proposed, to confirm with the user

## W1: shared references and one worker adapter

- [x] `contracts` package (`ExecutionContext`, `ArtifactRef`, `Event`, the worker API types)
- [x] First slice: one shell command and two stateful Python cells through the worker API, driven by the harness's tool loop with a scripted model. Done locally and on babbage in an OpenShell VM sandbox ([lab 4](docs/agent-reports/babbage-openshell-lab-4-2026-10-11.md)), after working around two OpenShell seccomp restrictions (scheduler shim, process-group signals)
- [x] The control application in one container and execution in another: the harness in its own OpenShell sandbox under the compiled orchestrator policy (worker API only), the tools in a sandbox the worker created; location, state, the boundary in both directions and reconnection checked ([lab 7](docs/agent-reports/babbage-openshell-lab-7-2026-10-11.md))
- [x] An orchestrator image, so the harness's sandbox needs no uploaded wheelhouse (D46): the `orchestrator` profile in `images/` ([images](../docs/IMAGES.md#the-orchestrator-image)). The harness is installed from wheels built from the checkout, over a lockfile with aiohttp and pydantic at `uv.lock`'s minor versions; the prefix is read-only and there are no kernels. Built for linux/amd64 on babbage and checked as a plain container and under a Landlock ruleset like the orchestrator policy's, including a scripted run against a stub worker
  - [x] Run the harness from the image in an OpenShell lab, under the compiled orchestrator policy with the targets file's `orchestrator.filesystem` ([lab 12](docs/agent-reports/babbage-openshell-lab-12-2026-10-11.md))
- [ ] The first slice with a real model through LiteLLM (needs the user's model gateway and a virtual key)

## W2: certify babbage execution with math and source capabilities

- [x] Sage through SageMath's own Jupyter kernel (`sagemath` kernelspec): the worker offers it when the environment has it, and the harness gives the agent a `sage` tool. Tested with a stand-in kernelspec
- [x] Real SageMath from conda-forge in an OpenShell sandbox ([lab 5](docs/agent-reports/babbage-openshell-lab-5-2026-10-11.md)): preparsing, finite fields, polynomial rings, Gröbner bases, exact matrices, LaTeX, GAP, Maxima, PARI and fpylll, interrupt, timeout, restart and kernel death. SageMath 10.9 installs in about 30 s through the cache; the VM costs 0–7% on single-threaded mathematics. Needed two fixes: the compiled policy now allows pseudo-terminals and `/dev/shm`, and the executor puts the environment's tools on `PATH`
- [x] `crypto-base` profile with Sage present at session start (D46): in [lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md) its executor offered `python` and `sage` at once, and Sage ran with LaTeX results
- [x] PDF extraction and rendering in the worker (pdf_oxide, D59): the in-sandbox helper, the [spec](../docs/specs/pdf-sources.md), the worker's `PdfRequest` and the harness's `pdf_*` tools
- [x] PDF evidence on babbage ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)): Regev's LWE paper through the PDF tools in a crypto-base sandbox; page labels, outline, line anchors, and page and equation crops published as artifacts. Text extraction garbles mathematics, and the renders do not

## W4: integrate the initial research workspace

- [x] GUI stack (D80, confirmed by the user): Svelte 5 with TypeScript built by Vite (`web/workspace`), an aiohttp server (`packages/workspace`), markdown-it and KaTeX in the browser
- [x] First slice ([workspace](../docs/WORKSPACE.md)): a read-only API v0 and views of projects, their sessions and environments, and each session's runs (messages, tool calls with arguments and results); Jupyter-like login, origin and host checks, a strict content policy; one Markdown component for all text; a `web` CI job and the npm lockfile check (licenses, registry, integrity, install scripts)
- [x] The harness keeps each run's record with `--state-dir`, the same that `--transcript` writes
- [ ] Living documents (D23, D65; [spec drafted](../docs/specs/living-documents.md)): parsing on the server with markdown-it-py and mdit-py-plugins, passing the shared math cases (`web/workspace/src/lib/dollarmath.fixtures.json`), with the 200-line limit on `$$` blocks added to the plugin's block rule; block and math IDs; revision-checked edits; comments and anchors
- [ ] Document files, mirror and merge (D65 amended; [living documents](../docs/specs/living-documents.md), LD-49 to LD-52): the worker keeps each environment's `documents/` at the documents' current revisions at every sync point, outside the `shared/` sync; it reports direct edits of the files, which the control application merges into the current revision or keeps as rejected patches; and the `ss-doc` helper's calls travel from the executor through the worker in the running call's event stream
- [ ] Math findings (D82; [living documents](../docs/specs/living-documents.md), LD-53 to LD-55; [consistency review](../docs/specs/consistency-review.md), CR-36 to CR-38), with the review machinery: KaTeX run on the server outside the orchestrator's process (Node or an embedded engine, to choose) on every save of durable text; `math` findings with the display's repair as their suggested fix, handed over in the turn's closing round; confirmation by the check itself
- [ ] Tables: markdown-it 15 and markdown-it-py 4.2 split a table differently when rare Unicode whitespace starts its rows (4 of 120,000 random texts); pin both parsers' versions and check again when the server parses Markdown
- [ ] Ledger and board (D24; [spec drafted](../docs/specs/ledger.md)), and review findings (the consistency review spec)
- [ ] Approvals in the GUI, with re-authentication for high-risk ones (D54); policy revisions from the GUI
- [ ] Previews of rich artifacts (HTML, SVG, notebook output, PDFs) on a separate origin in sandboxed frames (D54); the PDF viewer
- [ ] Live updates while a run is in progress; the environments' live state from the worker
- [ ] Records of runs that end in an error
- [ ] Ship the bundled npm packages' license notices with the built app before it is distributed: the build drops license comments
- [ ] TLS, sessions that survive a restart, and the GUI on another machine than the harness (W5)

## babbage

- [x] Install OpenShell v0.1.3: release binaries in `~/.local/opt/openshell-0.1.3/bin`, checked against the published SHA-256 checksums, GitHub's digests and the release workflow's build attestations
- [x] OpenShell's kernel qualification: it runs inside each sandbox (the guest kernel, for the VM driver) and passed; no host-side check needs user namespaces (lab, 2026-10-10)
- [x] Exercise the VM driver (experimental at v0.1.3) on KVM: gateway and sandboxes ran from a systemd user unit (lab, 2026-10-10)

## Repository

- [x] Development conventions, uv workspace, CI with license check
- [x] Plan snapshot tags
- [x] Fix doc drift: IMPLEMENTATION-PLAN's harness default contradicts D61; ROADMAP M3 says hosted targets were established in M1 (PR #4)
- [ ] Delete the paused `impl/p-policy-engine` branch and the merged `impl/p-policy` and `docs/plan-drift` branches (user's call)
