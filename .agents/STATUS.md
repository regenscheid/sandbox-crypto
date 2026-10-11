# Status

Updated: 2026-10-11.

**Phase:** 1 (babbage), milestone M0. **Focus:** infrastructure track P: the policy library, its OpenShell compiler and the policy middleware.

## Recent progress

- W1 boundary end to end ([lab 7](docs/agent-reports/babbage-openshell-lab-7-2026-10-11.md)). The harness ran in its own OpenShell sandbox, whose compiled policy admits only the worker API, and drove tools in a research sandbox the worker created. Every tool ran there at tier 3, with the probe's evidence and Python state kept. Neither sandbox could reach what it shouldn't. A harness killed mid-cell reattached to the same execution.
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

- Orchestrator: the middleware's policy store and bindings, escalation grants, and on-demand admission of hosts.
- W0 behavior specifications.

## Blockers and open questions

- Confirm D68 (policy evaluation details), the details of D69 (its direction, OpenShell plus a policy middleware, is confirmed), D70 (the middleware service) and PSF-2.0 and MIT-0 in tier 1.
- OpenShell's VM driver README calls the driver experimental at v0.1.3, while the support matrix lists it as supported. babbage's planned tier 3 isolation depends on it (D43). It worked throughout the lab.
- babbage enforces Ubuntu's AppArmor restriction on unprivileged user namespaces. Shells started from Claude Desktop run under its `claude-desktop` profile and are exempt, so qualification checks must run from a plain terminal or `systemd-run --user`.
- crypto-analysis-skills: confirm the copyright holder before merging its MIT LICENSE pull request.
