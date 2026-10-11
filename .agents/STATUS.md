# Status

Updated: 2026-10-11.

**Phase:** 1 (babbage), milestone M1: its exit run passed on babbage ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)). M0's specs and decisions still await the user's review. **Focus:** toward M2: project and session state, shared data across environments, and the MCP connectors.

## Recent progress

- **Math repairs in the workspace** ([workspace](../docs/WORKSPACE.md#math-repairs)), adapted from the user's own `cscience-crypto`, which the user contributed under MIT. The [living documents](../docs/specs/living-documents.md) spec describes the same behavior (LD-41 to LD-48), and its math findings (LD-53 to LD-55) are to use the same repairs.
  - The renderer reads `\(…\)`, `\[…\]` and bare display environments as math, by rules that follow dollar math's. Code stays as written, and displayed math stays in its list item or quote; the original's conversion before parsing rewrote both. LD-41 drafts that conversion; which to keep is for the user.
  - What KaTeX rejects is repaired by rules where it can be, else by the reviewer model, else approximated in Unicode. Each is marked, with KaTeX's error and the LaTeX as written behind the mark; the text never changes.
  - `POST /api/v0/math-repair` asks only the configured reviewer alias, through the model gateway, within the original's limits, and at most 60 times an hour for each browser session. The gateway is the server's only outbound connection, and only when configured.
  - Tested with a scripted model and a fake gateway, and in a browser against the fake gateway. No real model or key yet.
  - An adversarial review found no way to run script and the key never reaching the page, and ten other issues, all fixed. The worst froze the page: every answer rendered the whole text again, and with no model on the server every waiting formula did (300 broken formulas, 37 s); a run of backslashes took quadratic time; and formulas with macro definitions kept KaTeX busy for seconds. Now the page keeps KaTeX's results, sends one request until the server has a model, drops the rest at once, renders again at most once a frame, and refuses definitions.
  - A second review confirmed those fixes and found eight smaller issues, all fixed. KaTeX's time had been a second for each text, so a page of 30 texts of the slowest formulas froze for 9 s. Now it is 250 ms for each go of the page's thread, shared by the texts rendered in it, with the rest rendered in later goes: that page's first view takes a third of a second. Escaped brackets around prose (`\[...\]`, `\[1, 2\]`, Pandoc's citations) stay text, which is the user's call (rule and trade-off in the workspace docs).
- **Decisions on living documents and the ledger** (2026-10-11; D65 amended, D82). Living documents are also Markdown files under `documents/` in every environment, kept at their current revisions; agents change them through the document tool or its `ss-doc` helper, and direct edits of the files are merged or kept as rejected patches. Formulas KaTeX rejects in an agent's text come back to that agent as `math` findings at the end of its turn, while the display shows the repair. The other main questions of both drafts are approved as drafted, and the approved research-artifacts and consistency-review specs carry dated amendments to match.
- **Shared data across a project's environments** (D79, [lab 16](docs/agent-reports/babbage-openshell-lab-16-2026-10-11.md)). The worker keeps a project's `shared/` area in step across its environments, before and after every call. A dedicated session now sees the shared results of the project's other sessions, and two environments changing one file leave a conflict copy, of which the agent is told. An adversarial review found that a reported digest was used as a host path, that naming another project's digest got its content, and that a sandbox made again looked as if all its files were deleted; the reworked sync checks what executors report, keeps each project's blobs apart and marks each environment's directory. On babbage, across two VMs, a call that changes nothing costs 0.03 s.
- **Specs for living documents and the project ledger** (W0, drafts to review with the user). [Living documents](../docs/specs/living-documents.md): block and math-span IDs kept across edits, agent patches that name their base and blocks and are rebased or refused, never overwriting a newer edit, comments that carry work requests to agents, and the user's CScience math handling: dollar delimiters on save, and LaTeX that KaTeX rejects repaired for display by rules, the reviewer model or a Unicode approximation. [Ledger](../docs/specs/ledger.md): typed entries with evidence types apart from status, a frontier compared within a category and its conditions, flags after assumption changes, and the external pack's board records. Every W4 feature now has a spec, apart from the GUI's PDF viewer.
- **The workspace GUI, first slice** (W4, D80; [workspace](../docs/WORKSPACE.md)). A Svelte app, built into static files, and an aiohttp server, `sandbox-science-workspace`, show the harness's projects, sessions and runs, read-only: the agent's messages, its tool calls with their arguments and results, and the LaTeX in them, rendered by KaTeX.
  - Login works like Jupyter's: a token printed once opens an HttpOnly, SameSite=Strict session. Changes need the server's own origin, a loopback server answers only to its own names, and the content policy admits no inline script.
  - One component shows all text: raw HTML stays text, links only for http, https and mailto, images never load, KaTeX in its untrusted mode. A formula KaTeX cannot parse shows as written, beside its error.
  - The browser's math rules are a port of mdit-py-plugins', which the server is to use; shared cases pin them, and the plugin passes them.
  - The harness now keeps every run's record with `--state-dir`.
  - Checked in a browser against records the harness wrote. CI has a `web` job, and checks the npm lockfile's licenses, sources and install scripts.
  - An adversarial review found no way to run script, and eight other issues, all fixed. The worst: a formula could draw fake code over the tool call below it, and some texts took minutes to render.
- **An image for the MCP proxy** ([lab 15](docs/agent-reports/babbage-openshell-lab-15-2026-10-11.md)). The `mcp-proxy` image runs the proxy as its sandbox's command, through a launcher that waits for its settings and restarts it. In OpenShell, the providers' placeholders reached it, and it came back after the sandbox stopped and started; the port forward has to be started again.
- **One policy store.** The middleware, the worker and the MCP proxy now share one store module in the policy library, and the worker no longer depends on the middleware package.
- **Admission ahead of use** (D69). The agent can name the sites its code will contact before running it (`admit_hosts`), and the worker admits the allowed ones in one policy reload, so the first connection need not fail and no transfer is cut. In [lab 14](docs/agent-reports/babbage-openshell-lab-14-2026-10-11.md), a host named this way was reachable on the first connection, and clients limited to projects held against a real gateway.
- **Per-project worker credentials** (D78, proposed). Besides the pool's own token, the worker accepts client tokens that act only for the projects the settings list.
  - For such a client, other projects' executions, artifacts and environments do not exist. It can use only environments the worker created for its projects, under policy revisions an operator granted to them.
  - Two adversarial reviews found three ways around the checks, all closed:
    - An environment deleted and created again for another project while a request waited could receive that request. IDs now stay with their first project.
    - A sandbox made by hand could be reached through a deleted environment of the same name. Those sandboxes are now the pool token's alone.
    - Sandbox names derived from IDs could be shared by a crafted ID, which then took over another project's sandbox. Names are now random, and the worker checks a sandbox's identity before running code in it.
  - Operation IDs are kept per project, and the harness's environment IDs end with a random tag.
- **The MCP proxy in its own sandbox** ([lab 13](docs/agent-reports/babbage-openshell-lab-13-2026-10-11.md)).
  - The proxy ran in an OpenShell VM sandbox for the first time. It was reached from the host and from an orchestrator sandbox, and called DeepWiki's public MCP server and a fake one on the host.
  - OpenShell put the provider's token into the proxy's requests at the bound server only. The proxy's version header and its replies to the server's requests passed the fence.
  - Inside a sandbox every name resolves to a stand-in address, so the proxy could not check where a server's name leads. The compiled fence now checks the real addresses.
  - The proxy's sandbox policy now covers its image's prefix, and the orchestrator's admits the proxy. The proxy reports the fence's refusals as `fence_denied`.
- **Idle suspension** (W3; [lab 11](docs/agent-reports/babbage-openshell-lab-11-2026-10-11.md)). The worker can stop the sandbox of an environment nobody is using, and start it again on the next use, which then takes about 7 s. Files, installed tools and the policy survive; kernels are reported lost. A computation in a kernel keeps its environment up.
- **Admission on demand** (D69, amended; [lab 10](docs/agent-reports/babbage-openshell-lab-10-2026-10-11.md)).
  - A sandbox now reaches hosts its policy allows but does not name. The first connection fails. The worker then reads OpenShell's draft for it, checks the host against the policy, and names it in a new compilation. On a real gateway the host was reachable 9 to 20 s later.
  - The compiled policy no longer has a host-less endpoint, which had kept OpenShell from drafting anything.
  - Each admission reloads the sandbox's policy, which interrupts transfers in progress.
- **Project records in the orchestrator** (W3). The harness records each project's profile, policy and sessions, and creates the default and dedicated environments through the worker. `run --state-dir` picks the session's environment, and `session close` stops its kernels.
- **The M1 exit run** ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)).
  - The harness ran in its own sandbox (A), which can reach only the worker API, and drove a research sandbox (B) that the worker had created from the `crypto-base` image, at tier 3 with the probe's evidence.
  - In B the agent:
    - fetched Regev's LWE paper;
    - read it with the PDF tools (37 pages and the outline; hits cited as `p15:23`);
    - published a page render and a crop of two equations, located from the line boxes;
    - ran Sage with LaTeX results;
    - got a useful Python error;
    - installed through the package cache, with `pypi.org` refused.
- Base profile images (D46, D77 proposed; [images](../docs/IMAGES.md)). `images/` holds the recipes for `minimal` and `crypto-base`, pinned throughout, with Conda lockfiles for both architectures. Both were built for linux/amd64 on babbage and passed their checks as plain containers: read-only root, uid 1000, and under a Landlock ruleset like OpenShell's. In crypto-base the executor offers Python and Sage, and SageMath, fpylll, G6K, lattice-estimator, `latexmk`, the PDF tools and the in-sandbox PDF helper work. Next: run them in an OpenShell lab; build arm64.
- MCP proxy, first version (`packages/mcp-proxy`, D75 proposed, [docs](../docs/MCP-CONNECTORS.md#the-mcp-proxy-as-built)). It decides every MCP message with the policy library under the project's revision, refuses escalations as pending requests, records alerts before they proceed and keeps one record per call. It applies lab 2's findings: exact advertised tool names, `MCP-Protocol-Version` after `initialize`, and placeholders only in headers. Tested against a fake Streamable HTTP server; not yet run in its OpenShell sandbox.
- W1 boundary end to end ([lab 7](docs/agent-reports/babbage-openshell-lab-7-2026-10-11.md)). The harness ran in its own OpenShell sandbox, whose compiled policy admits only the worker API, and drove tools in a research sandbox the worker created. Every tool ran there at tier 3, with the probe's evidence and Python state kept. Neither sandbox could reach what it shouldn't. A harness killed mid-cell reattached to the same execution.
- Package clients in sandboxes: with its `package_cache` setting, the worker has the executor write pip, uv, conda (mamba, micromamba), Cargo, Go and npm configuration that reaches the cache only, keeping files someone changed ([client configuration](../docs/PACKAGE-CACHE.md#client-configuration)). Real clients installed through a loopback cache with it; an OpenShell lab is still to come.
- Event store (`packages/event-store`, D74 proposed, [docs](../docs/EVENT-STORE.md)): hash-chained SQLite records, checkpoints and the `sandbox-science-events` command. The policy middleware records its escalations, alerts and monitor findings there instead of `events.jsonl`.
- Worker-managed environments (D73, proposed; [lab 6](docs/agent-reports/babbage-openshell-lab-6-2026-10-11.md)). The worker creates, revises and deletes research sandboxes: it compiles a stored policy revision, attaches exactly the listed providers and binds the middleware. An operator adds revisions with `sandbox-science-worker policy add`. Against a real gateway, a sandbox was ready in 11 s and a revision took 9 s. Isolation-tier probes and the backend conformance suite also landed (PR #22).
- Isolation-tier probes (D43): results report the tier in effect, the declared tier and the evidence, and evidence only lowers the declared tier. The backend conformance suite (D52) runs the execution-tools fixtures through the worker API against any backend; it still has to run against OpenShell in a lab.
- Sage (W2): the worker offers SageMath's own Jupyter kernel when an environment has it, and the harness gives the agent a `sage` tool (PR #20). Lab 5 on babbage ([report](docs/agent-reports/babbage-openshell-lab-5-2026-10-11.md)) ran a real SageMath 10.9 from conda-forge in OpenShell VM sandboxes. Preparsing, finite fields, polynomial rings, Gröbner bases, exact matrices, LaTeX, GAP, Maxima, PARI and fpylll all worked, as did interrupt, timeout, restart and kernel death. Two fixes came out of it: the compiled policy now allows pseudo-terminals and `/dev/shm`, and the executor puts the environment's tools on `PATH`. The VM costs 0–7% on single-threaded mathematics.
- Lab 4 on babbage ([report](docs/agent-reports/babbage-openshell-lab-4-2026-10-11.md)): the W1 first slice ran in an OpenShell VM sandbox. The harness drove the worker service, which drove the in-sandbox executor and Jupyter kernels. Every execution-tools fixture passed after working around two seccomp restrictions: a scheduler shim for libzmq, and per-process signals for process groups. Publication now names the attempt that wrote a file, from observed file lists.
- W1 first slice, locally: `packages/contracts`, the [worker service](../docs/WORKER.md) with its in-sandbox executor (`packages/worker`), and the harness's tool loop with a provider-neutral model interface (`packages/orchestrator`). The [execution tools spec](../docs/specs/tools.md) and its acceptance fixtures pass on the local backend.
- Lab 3 on babbage ([report](docs/agent-reports/babbage-openshell-lab-3-2026-10-11.md)): the compiled credential bindings load and work with an endpointless provider. OpenShell's substitution into plain HTTP on port 443 was seen at runtime (residual risk, [bug description](upstream/openshell-credential-substitution-over-plain-http.md)).
- Provider credentials (D57 amended, the user's choice after lab 2): the policy names each provider's hosts, and the compiler binds the credential there on HTTPS. Providers use profiles without endpoints, and the middleware treats only those hosts as credentialed.
- Lab 2 on babbage ([report](docs/agent-reports/babbage-openshell-lab-2-2026-10-10.md)).
  - In a sandbox, real pip, uv, micromamba, npm, go and cargo installed through the policy middleware and the package cache, with toolchains from conda-forge.
  - Provider credentials and the MCP proxy's compiled policy were checked against the gateway.
  - Cargo needs `http.multiplexing = false` over plain HTTP (documented).
  - Lockfiles written in a sandbox name the cache (backlog).
  - Providers whose profiles name hosts conflict with the compiled policy (decision pending).
  - The v0.1.3 prover cannot check policies with middleware or MCP endpoints.
- Package cache (D71), PRs #10 and #11: the policy side for PyPI, conda, Cargo, Go and npm (reviewed adversarially; the review's critical finding is fixed) and the pull-through cache service. Real clients install through it on loopback. Go 1.27.2 is installed on babbage for these tests.
- Policy library (`packages/policy`): merged in PRs #3 and #5, after five adversarial review rounds.
- OpenShell compiler (D69), PR #6: admission endpoints plus the policy middleware for research sandboxes, and the MCP proxy's policy, with rendered examples in `examples/openshell/`. OpenShell carries the traffic; there is no separate egress proxy.
- Policy middleware service (`packages/policy-middleware`, D70), PR #7: the OpenShell supervisor middleware that decides each request with the library. Verified against a real OpenShell v0.1.3 gateway with the VM driver on babbage ([lab report](docs/agent-reports/babbage-openshell-lab-2026-10-10.md)); the lab was torn down afterwards.
- `allow_credentialed`: a read-only rule can let credentialed requests time out to an alert, endpoint by endpoint (requested by the user).
- The lab showed that a sandbox resolves only host names its policy names. The compiler now names every public host the policy mentions; on-demand admission will cover the rest (D69, amended with the user).
- OpenShell v0.1.3 installed on babbage (`~/.local/opt/openshell-0.1.3/bin`), checked against the published checksums, GitHub's digests and the release workflow's build attestations.
- Plan drift fixed in PR #4; conventions, uv workspace and CI (lint, types, tests, license check, generated-code check); plan snapshot tags pushed.

## Next

- M2 (W3): the Conda provisioning spec (installs queued, recorded with lockfiles and probed). The read-only root filesystem is deferred (D48).
- M2 (W4): the research workspace (living documents, ledger and board, comments, automatic consistency review), which the M2 exit run needs.
- MCP connectors: the connector auth service for crypto-research-mcp (it needs the user's OAuth consent), compiling approved tools into the proxy's fence, and exporting the proxy's records to the monitor.
- Worker: environment IDs per project, and limits per client.

## Blockers and open questions

Decided by the user on 2026-10-11: D68–D78 confirmed; shared data across a project's environments by worker-synced copies (D79); the read-only root filesystem deferred (D48); the Conda provisioning, research-artifacts and consistency-review specs approved as drafted, with the artifacts spec's shared-file rule amended for D79; the workspace GUI as a Svelte web app (D80); delegated agents are runs in their session's environment, not sessions, with an environment of their own as a later opt-in (D81); living documents as files in every environment with merged edits (D65 amended); math findings for the agents that wrote the math (D82); and the other main questions of the living-documents and ledger drafts as drafted, with RA-1, RA-24, CR-10 and CR-28 amended.

Decisions to confirm: PSF-2.0 and MIT-0 in tier 1.

Questions with a recommendation, waiting for the user:
- **Event store checkpoints.** The user timer that logs each checkpoint to the journal is in `examples/systemd` ([event store](../docs/EVENT-STORE.md#checkpoints-in-the-journal)); installing it on babbage is the user's call.
- **Images.**
  - No registry on babbage, since OpenShell reads the local Docker store; a self-hosted Zot or Distribution on the cluster.
  - Keep G6K at x86-64-v3 (AVX2).
  - Accept the GPL runtime libraries in `minimal` and add an SBOM before it is ever published.
  - Keep crypto-base's compilers, R and OpenJDK.
  - Let projects carry the IACR LaTeX classes.
- **MCP proxy.**
  - Build MCP 2026-07-28 only when a server needs it.
  - Keep binary results withheld until artifact references exist.
  - Keep `max_argument_bytes` in the policy.
  - Decide whether worker code may use `resources/read`, `prompts/get` and `completion/complete`, which `worker_tools` does not restrict.
- **The W0 specs** still in draft (tools, skills, plans and delegation, PDF sources, living documents, and the project ledger, board and frontier), each with its open questions; the last two record the user's decisions on their main questions, and the spec index's question 15 asks about stray files under `documents/` and edits from overlapping calls; the Conda provisioning, research-artifacts and consistency-review specs are approved. The consistency review's trigger defaults wait for the user's own CScience settings.
- **A real model.** The first slice with a real model needs the user's LiteLLM gateway and a virtual key.
- **Upstream.** Whether to file the OpenShell and libzmq descriptions in `.agents/upstream/`.
- **Workspace GUI.**
  - The GUI shows conversation text in its own origin, under its Markdown rules and a policy without inline script, while D54 puts previews of agent-written artifacts on a separate origin. Recommended: keep conversation text where it is, and give rich artifacts (HTML, SVG, notebook output, PDFs) the separate origin when previews come.
  - The workspace server reads the harness's state directory through the orchestrator's store, an exception to the rule that deployable members never import each other. Recommended: accept it until W5 gives the control application an API.

Known facts to keep in mind:
- OpenShell's VM driver README calls the driver experimental at v0.1.3, while the support matrix lists it as supported. babbage's tier 3 depends on it (D43); it worked in every lab.
- babbage enforces Ubuntu's AppArmor restriction on unprivileged user namespaces. Shells started from Claude Desktop run under its `claude-desktop` profile and are exempt, so qualification checks must run from a plain terminal or `systemd-run --user`.
- crypto-analysis-skills: confirm the copyright holder before merging its MIT LICENSE pull request.
