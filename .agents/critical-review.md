# Critical review of the Sandbox Science plan

Date: 2026-10-10. Reviewer: Claude Code. Scope: every document under `docs/`, `examples/`, the root `README.md`, and the `.agents/docs/` archive index and agent reports. This review covers the plan only. Nothing was built or run. License facts cited below come from the components' published license files, read on 2026-10-10; re-check them when pinning versions.

The project goal under review: a web-GUI research workbench for cryptography, similar to Claude Science. Projects must be able to download, install and execute code in isolated local or hosted containers, where execution and network isolation policies can be monitored and enforced. A requirement added during this review: the project should use permissive dependencies and ideally be MIT-licensable. GPL is acceptable only where it does not affect the project's license, and AGPL should be avoided.

Proposed edits that follow from this review are on the `review` branch and are described in [proposed-changes.md](proposed-changes.md).

---

## 1. Summary verdict

The plan is careful about evidence. It consistently separates what was inspected from what was validated. Its first engineering slice is well chosen: route ordinary shell and stateful Python through the retained tools before building anything else. Several hard details are already right: no silent fallback to running code in the orchestrator, no holding human approval inside a bounded middleware RPC, alert-and-continue being different from audit mode, and new session containers starting from the base image.

Its weaknesses sit where the stated goal is most demanding:

1. **There is no threat model.** The plan is mostly about isolation and monitoring, but it never says who the adversary is, which assets matter, or how strong the boundary must be. As a result, the isolation strength (plain containers, gVisor/Kata, or a microVM) is left as an open question, even though it is the core security decision.
2. **Monitoring relies on content inspection where it should rely on egress design.** In a cryptography workbench, the data that matters (keys, ciphertexts, random-looking research output) cannot be told apart from normal traffic by content heuristics. Default-deny egress, a package mirror and read-only fetch paths do most of the work. Inspection should be a secondary signal.
3. **The orchestrator is treated as trusted, but it is directly exposed to prompt injection.** It reads untrusted papers and web content, holds model and connector credentials, and can make outbound calls through connectors. Sandboxing the workers does not close that exfiltration path.
4. **The implementation foundation conflicts with the licensing goal and is fragile.** "CScience" is a set of patches applied to an extracted, minified, proprietary Claude Science 0.1.60 bundle. That cannot become MIT-licensed code. It is also costly to keep patched across upstream releases, and its terms may not permit modified hosted deployment. The plan measures "patch burden" but sets no go/no-go threshold and does not consider licensing at all.
5. **The scope is too large for one person.** The "initial, not later" scope includes hosted Kubernetes, local runtimes, eight placement combinations, snapshot/restore, a three-action monitor, concurrent document editing, a ledger/frontier, native Sage, PDF region anchors and external skill packs. Every document reinforces that none of these can be deferred.
6. **The documents are heavily duplicated.** `.agents/docs/current/` is a byte-for-byte mirror of `docs/`, and the same requirements are restated across 6–10 files. A consistency review was needed for this reason, and the duplication will keep causing drift.

Findings below are ordered by severity: **Critical** (threatens the core goal), **Major** (likely rework or a security gap), **Minor** (clarity or maintainability).

## 1a. Updates after the user's responses (2026-10-10)

The user answered several points during the review. The branch now reflects them:

| Topic | User response | Effect on this review |
| --- | --- | --- |
| Licensing | Prefer permissive; ideally MIT; GPL only if it can't affect the project; avoid AGPL. Private, personal project. | M1 and `docs/LICENSING.md` (D45). |
| Runtime installs | Many research dependencies can be installed at runtime. | GPL tools (Sage, fpylll, G6K) are installed at runtime, not shipped (D46). |
| CScience code | Don't use Claude Science code; reimplement the parts needed. | **C4 is resolved by decision** (D11 confirmed, D03 amended, D05 superseded). The go/no-go criteria for patching are moot. They are replaced by behavior specs and reimplementation hygiene (D51, D56) and an MIT harness stack (D55). The plan no longer needs a "retained runtime" path, which also removes the risk of a forgotten local execution path. |
| babbage | Host name of the user's Ubuntu 26.04 machine. | m3 resolved. Added a check for OpenShell's Landlock ABI and Ubuntu's unprivileged-user-namespace restriction. |
| Threat model | Primary concern is agentic workers doing things they shouldn't. Prompt injection from consumed papers is less of a concern. | C1's threat model is reprioritized: agent overreach (A1–A4) is primary, and T2 (injection) is lower. C3 still stands, reframed: the orchestrator executes agent-selected MCP and model calls, so overreach through it needs the same governance as workers. |
| External services and secrets | Mostly via MCP. Workers that need API keys should get them from a secrets manager that limits direct access; OpenShell has features for this. | Confirmed against OpenShell v0.1.3 docs (details below). Added D57 (secrets via providers) and D58 (MCP endpoints with tool allowlists). |

**OpenShell secrets (as documented at v0.1.3):**

- *Providers* give sandboxes opaque placeholders. The proxy substitutes real values only when the network policy and the credential's endpoint binding both match, and fails closed otherwise.
- The gateway can mint short-lived tokens through OAuth2 and STS refresh.
- Credentials are stored encrypted in the gateway database, or via `kubernetes-secrets` or `vault` drivers.
- `protocol: mcp` endpoints support method- and tool-level allowlists.
- Gaps to design around:
  - placeholders are endpoint-scoped, not binary-scoped;
  - there is no rewriting for cookies, unsupported bodies or binary frames;
  - responses are not rewritten;
  - a placeholder still permits anything its token's scope permits.
- HashiCorp Vault is BUSL-1.1, so the licensing policy points to OpenBao (MPL-2.0) or Kubernetes Secrets.

Findings below are kept as originally written, so the reasoning stays visible. Read C4 and the stdio-seam suggestion as superseded.

---

## 2. Critical findings

### C1. No threat model, so the isolation strength is undecided

The documents say "isolation", "trusted monitor" and "untrusted parsers" many times, but nowhere state:

- **Assets:** model/provider API keys; connector credentials; Magma license; unpublished cryptanalysis and embargoed vulnerability findings; private datasets (real keys, captured traffic); the user's local machine in the hosted-control/local-compute profile; the monitor's own integrity.
- **Adversaries / failure sources:**
  - malicious or compromised packages, including Conda/pip build hooks, typosquats and compromised maintainers;
  - prompt injection from papers, web pages, repository READMEs or tool output that steers the agent;
  - buggy or destructive code generated by the agent;
  - a compromised hosted control plane that sends commands to a local worker;
  - exploit code the researcher deliberately downloads to study, which this domain will do routinely: published attack PoCs, side-channel tools.
- **Trust boundaries:** which components may share a kernel, which hold credentials, and which can widen policy.

Without this list, the plan cannot answer its own open question: "Does the environment need a VM isolation boundary, or is a dedicated container host sufficient?" (`docs/OPENSHELL-EVALUATION.md`, open questions). The core requirement is to *download, install and execute* arbitrary code. That puts it in the same class as CI runners and online judges, which generally do not rely on a shared-kernel runc container alone.

A concrete problem with the planned local profile: under Docker Desktop on macOS, the orchestrator (holding model keys), the OpenShell gateway (holding the engine socket), the monitor, and every research worker run in the **same Linux VM kernel**. A single kernel escape from a worker compromises everything the plan calls "trusted." The phrase "trusted monitor outside agent-controlled containers" is true at the container level but false at the kernel level.

**Recommendation:** add `docs/THREAT-MODEL.md` (drafted on the review branch). From it, derive isolation tiers. For hosted, run workers in a sandboxed RuntimeClass (gVisor or Kata) on a node pool separate from control services. For local, run workers in a separate VM from trusted services, or record the shared-kernel risk as accepted for a single-user deployment. Make the tier a recorded property of every execution.

### C2. Content inspection cannot carry the exfiltration-control goal in a cryptography workload

`docs/MONITORING.md` puts much of the defense on inline request/response inspection and "contextual behavior monitoring." This approach has known limits in general, and they are worse in this domain:

- Research output is mostly high-entropy: keys, ciphertexts, hashes, random lattices. Entropy or pattern-based data-loss prevention cannot separate legitimate output from exfiltrated secrets. The plan notes that "keywords alone must not block legitimate cryptanalysis," but does not draw the larger conclusion: content heuristics will be noisy and easy to evade here.
- TLS inspection needs a CA injected into the sandbox. Code can pin certificates, bring its own TLS stack, or use protocols the middleware does not inspect. The plan itself lists WebSocket binary, `tls: skip`, and compressed or partial bodies as uncovered.
- Data can be encoded into *allowed* channels: URL query strings to a permitted API, package names requested from a pull-through mirror, DNS lookups, git request bodies, timing.
- DNS gets one mention in a test row ("DNS/redirect behavior") and has no policy.

**Recommendation:** reorder the layers. Rely on **egress architecture** first and use inspection second:

1. Execution sandboxes have **no direct internet route**. Their only egress is to trusted proxies, and DNS is resolved by the proxy, not the sandbox.
2. Package installs go through a **trusted pull-through mirror** restricted to allowlisted channels (conda-forge, PyPI), read-only, with cache misses logged. Use permissive tools such as devpi (MIT), quetz (BSD-3), or a caching Envoy/nginx proxy. This preserves automatic Conda provisioning, because Conda still resolves and installs inside the sandbox, while it removes arbitrary egress.
3. "Download code" goes through a **read-only fetch proxy**. For git smart-HTTP, allow the `git-upload-pack` service (clone/fetch) and deny `git-receive-pack`. This rule must be keyed on the service path, because fetch also uses POST. Allow GET on release archives and paper hosts. Deny API write methods, gists and paste sites by default.
4. Any destination outside the allowlist goes through permission escalation. It may never be **alert-only by default**.
5. Inspection and contextual monitoring stay, as detection and forensics, with coverage gaps shown in the UI.

This makes the monitor's job tractable. "Did this sandbox reach anything other than the mirror, the fetch proxy and its granted endpoints?" has a clear answer. "Was this payload malicious?" does not.

### C3. The orchestrator is an unsandboxed agent with credentials and network access

The plan sandboxes the workers thoroughly, but the orchestrator:

- reads untrusted content, including PDFs, web pages, MCP responses and tool output, directly into model context;
- holds model and connector credentials;
- calls research connectors such as web fetch, paper search and Firecrawl-style crawlers. A URL fetch with data in the query string is an exfiltration channel that never touches a worker sandbox;
- may still contain host-side execution: the plan's "classify existing `repl` host APIs individually" points at this risk.

`docs/MONITORING.md` covers orchestrator traffic in one sentence ("route those through an appropriate trusted gateway"), and the architecture diagram shows the connector broker as trusted.

**Recommendation:** treat the orchestrator as **semi-trusted**. It is trusted to hold durable state, but its *outbound actions are agent-influenced*. Concretely:

- give the orchestrator container egress only to the model gateway and the connector broker;
- put connector fetches under the same allowlist/escalation policy as workers, preferably by running fetch-type connectors inside a worker sandbox;
- never mount an engine or Kubernetes credential in the orchestrator;
- log every orchestrator-initiated outbound request in the same event store as worker traffic.

### C4. The CScience foundation conflicts with MIT licensing and has no exit criteria — resolved by D11 (reimplement)

The reuse assessment states that the runtime is "a pinned upstream distribution with compatibility patches, not an independently owned orchestration source tree." `src/patches.mjs` patches a minified upstream bundle (`claude-science.js`). Implications the plan does not address:

- **License:** the patched runtime is proprietary upstream code. An MIT-licensed project cannot include it. At most, the patch scripts can be MIT, and they are useless without the upstream bundle. The plan checks per-file terms for *skills*, but not for the runtime it proposes to patch, containerize and deploy to hosted Kubernetes. Hosted deployment of a modified runtime needs its own terms check.
- **Maintainability:** moving every execution path behind a remote worker API is a deep change. Doing it by patching minified code means each upstream release can break it silently. "Record the patch surface and upstream-version sensitivity" is a measurement, not a decision rule.
- **Sequencing:** the worker service, sandbox backend, egress proxies, mirror and monitor do not depend on which harness is chosen. Yet the plan places all of them behind the harness feasibility spike.

**Recommendations:**

- Add explicit **go/no-go criteria** for W0/W1. Examples: the execution seam can be intercepted at ≤ N patch sites; the patches survive one upstream minor-version bump with no manual rework; terms permit the intended deployment; no proprietary code is required in MIT-licensed components.
- Evaluate a **low-patch seam first**: replace the subprocesses the harness spawns (the JSON-lines `kernel_worker.py`, the shell) with thin stdio-forwarding stubs that relay to the worker over an authenticated channel, plus a synchronized workspace. This may need very few patches to the bundle.
- Run a **parallel, harness-independent MIT track**: worker service, worker API, egress proxies, mirror, monitor and environment manager. This work survives either outcome of D11.
- Use CScience as a **behavior specification and fixture source** (review triggers, finding lifecycle, plan/delegation semantics). Be prepared to implement the harness independently. The newer OpenScience Desktop upstream is MIT-licensed (verified), which makes it a cleaner source of reusable code than the patched runtime.

---

## 3. Major findings

### M1. No license policy, and several named components conflict with the goal

The plan has no license policy, and several candidates it names, or would naturally reach for, need attention:

| Component (plan reference) | License (verified 2026-10-10) | Implication for an MIT project |
| --- | --- | --- |
| SageMath (D26, "integral") | GPLv2+ (distributions effectively GPLv3 through components) | Install **at runtime** into the worker environment (conda-forge); do not bake it into the shipped image. Platform code must not import `sage.*`. Talk to Sage's own Jupyter kernel over the Jupyter protocol (jupyter_client is BSD-3). |
| fpylll (M5, W6) | GPLv2+ | Runtime-installed worker tool only. Underlying fplll is LGPL. |
| G6K (lattice sieving; likely later) | GPLv2 | Runtime-installed worker tool only. |
| lattice-estimator (parameter analysis) | LGPLv3+ | Runtime-installed worker tool. Fine. |
| FLINT | LGPL | Fine. |
| **PyMuPDF** (common choice for PDF extraction/rendering, which the plan makes central) | **AGPL-3.0** | **Avoid.** The user already uses **pdf_oxide** (MIT OR Apache-2.0), now the primary candidate (D59). pypdfium2 (Apache-2.0), pdfplumber (MIT) and PDF.js (Apache-2.0) remain alternatives. Check whether current CScience PDF provisioning installs PyMuPDF; any reimplemented provisioning must not. |
| Poppler utilities (common for PDF rendering) | GPL (not re-verified in this pass) | Acceptable as a runtime-installed separate program inside workers only. |
| **MinIO** (natural pick for "private object storage" in the all-remote profile) | **AGPL-3.0** | **Avoid.** Use cloud object storage, SeaweedFS (Apache-2.0), or plain filesystem storage. |
| **Grafana** (natural pick for a monitoring UI) | **AGPL-3.0** | **Avoid.** Build monitor views into the GUI, or use Prometheus/OpenSearch (Apache-2.0). |
| Squid (natural pick for a forward proxy) | GPL | Acceptable as a separate service, but Envoy (Apache-2.0) or mitmproxy (MIT) fit better. |
| marker (PDF→Markdown) | Code Apache-2.0; **model weights under a modified OpenRAIL-M with revenue limits** | Weights are not permissive. Flag if adopted. |
| OpenShell | Apache-2.0 | Fine. |
| gVisor, Kata, Tetragon, Falco, Envoy, Podman, Lima, agent-sandbox | Apache-2.0 | Fine. |
| Colima, mitmproxy, KaTeX, devpi, pdfplumber | MIT | Fine. |
| Jupyter client/ipykernel, mamba/micromamba, quetz, miniforge installer | BSD-3 | Fine. Packages installed *by* miniforge carry their own licenses. |
| OpenScience Desktop upstream (`ai4s-research/open-science`) | MIT | Good reuse candidate. |
| Claude Science runtime (CScience base) | Proprietary | See C4. |
| `regenscheid/crypto-analysis-skills` | **No LICENSE file found** at `main` | Add one, MIT if desired. Unlicensed code is all-rights-reserved by default, even for the author's own other projects. |

Also relevant:

- Anaconda's `defaults` channel has commercial terms of service; it was not re-verified in this pass because the policy page was unreachable. Defaulting to **conda-forge only** with miniforge/micromamba avoids the question.
- Docker Desktop has subscription terms for larger organizations. This does not matter for a personal project, but Colima, Lima or Podman avoid it.
- For a private personal deployment, GPL/AGPL obligations mostly arise on distribution or network service to others, so running MinIO privately would be legal. The recommendations above aim to keep the project *publishable* under MIT without later substitutions.

**Recommendation:** add `docs/LICENSING.md` (drafted on the review branch) with three tiers:

1. **Platform code** (GUI, orchestrator, worker service, proxies, monitor, adapters): MIT/BSD/Apache/ISC/PostgreSQL dependencies only. No GPL linking or importing. No AGPL anywhere.
2. **Shipped base worker image**: kept thin and permissive-only (micromamba, Python, jupyter_client/ipykernel, probe scripts). No GPL research tools baked in.
3. **Research dependencies installed at runtime**: Sage, fpylll, G6K, PARI, lattice-estimator and similar are installed **at runtime** from conda-forge, through the package mirror, into the project's environment volume. This is the existing automatic-Conda behavior, so it adds no new mechanism. The project then does not distribute these binaries. GPL obligations stay with the upstream packages the user installs, and the project stays MIT-clean. Avoid AGPL even here unless a specific tool is unavoidable. Record license metadata from each package's `info/licenses` in the environment record.

Installing at runtime also fits M2: the Conda prefix is a declared volume, so installed tools persist across idle periods without baking images. The cost is first-use latency, because Sage on conda-forge is several GB. Mitigate it with the mirror cache, a persisted environment volume, and an optional **locally built** (never distributed) "warm" profile image. A user who builds that image for personal use creates no distribution obligation.

Add an SBOM and license scan (syft, ScanCode; both Apache-2.0) to builds of the shipped image, so GPL/AGPL content cannot slip into tier 2.

### M2. The persistent mutable container is both a security liability and a snapshot problem

D07/D36 require a persistent project container and "retain the container or capture changed filesystem state." Capturing an arbitrary writable layer is fragile: it requires `docker commit`, overlay diffs or CRIU, and the plan admits it must "account for every allowed writable location." It is also a security problem. A malicious install hook that writes into `/usr/lib` or `~/.bashrc` persists indefinitely and survives "restores."

**Recommendation:** use a **read-only root filesystem** plus a small set of declared writable volumes: Conda env prefix, `$HOME`, project shared, session workspace, `/tmp` as tmpfs. Idle preservation then becomes "base image digest + volumes + lockfile," which is tractable and verifiable. Persistence is confined to known locations, and a full rebuild from manifest can discard tampering. The trade-off is that `apt install` does not work at runtime; system packages need a base-image change request. That is acceptable, and it improves reproducibility. Add a "rebuild environment from lockfile" action and use it as the quarantine recovery path.

### M3. OpenShell is a v0.1.3 project released one day before this plan, and the adapter has only one implementation

The plan correctly wraps OpenShell behind an adapter, but designs that adapter against a single backend. It has documented 0.0.x→0.1.0 breaking changes and documentation drift (supervisor binary vs. image). Abstractions with one implementation tend to take on that implementation's assumptions.

**Recommendation:** name a **reference fallback stack** and make the worker API satisfiable by both:

- **Hosted:** Kubernetes + gVisor/Kata RuntimeClass + Cilium or a default-deny NetworkPolicy + Envoy egress proxy + Tetragon/Falco for process/file/network telemetry.
- **Local:** rootless Podman or Docker with `runsc`, an internal-only network, and the same proxy.

Tetragon/Falco also give a monitor signal *independent* of OpenShell middleware. That is defense in depth, and it removes the plan's reliance on a single vendor's supervisor for both enforcement and observation.

### M4. Hosted control with local compute gives the hosted service remote code execution on the user's machine

Profile `hosted-control-local-compute` has the local worker dial out and accept commands. If the hosted orchestrator is compromised, it can run arbitrary code inside local sandboxes and request whatever grants it likes. The plan says trusted worker/policy infrastructure "owns policy enforcement," but never states that the **local pool's policy ceiling cannot be raised remotely**.

**Recommendation:** make it explicit that each pool has a locally configured maximum policy (egress destinations, mounts, resources, data classes). Remote control can only narrow it. Local enrollment requires local user action. Raising the ceiling requires local confirmation.

### M5. The web GUI is the approval surface and also renders untrusted content

The GUI renders PDFs, SVG/HTML figures, notebook outputs, Markdown/LaTeX from papers and agent-written documents. It is also where permission escalations are approved. Cross-site scripting in an artifact preview could therefore approve its own exfiltration request. The plan mentions "isolate rich artifact previews" in one sentence.

**Recommendation:**

- Serve untrusted artifacts from a **separate origin** in sandboxed iframes with a strict CSP.
- Configure KaTeX with `trust: false` and macro limits.
- Use PDF.js with scripting disabled.
- Require fresh authentication or a second factor, and CSRF protection, for permission approvals.
- Build approval prompts from **monitor-observed facts** (destination, method, bytes, data classes, requesting process), not from the agent's justification text.

### M6. The scope and the gates are too large for a single-user project

Count of "initial gate, not later" items in `docs/ROADMAP.md` M1–M3:

- hosted Kubernetes and at least one local runtime;
- native Sage;
- PDF region anchors;
- Conda under a monitor with three actions;
- persistent project and session containers;
- idle snapshot/restore;
- concurrent document reconciliation;
- ledger/board/frontier;
- external skill registration;
- automatic review;

and then all eight placement combinations in M4. For one person, this is plausibly a multi-year scope before first useful use.

**Recommendation (without overriding confirmed decisions):** keep D02's design constraint ("no profile may rely on another peer's localhost or absolute paths") but **certify placements in stages**: all-local → all-hosted → hosted-control/local-compute. Treat the other five combinations as configuration variations tested opportunistically. Define a **minimum useful release** that a researcher would actually use:

- local profile;
- project container plus kernels;
- Sage;
- PDF viewer with page anchors (regions later);
- mirror-only egress;
- block/escalate monitor actions;
- the existing review behavior.

Real use would then inform the ledger/frontier and concurrent-editing design.

### M7. Long-running and performance-sensitive cryptography workloads are under-specified

Gaps:

- **Long-running jobs.** Lattice sieving, index calculus and large searches run for days. They need application-level checkpoint/resume, tolerance of node preemption, and a placement that is not wiped by idle policies. The example `run-request.json` uses a 120 s limit, and there is no long-job class.
- **Timing and side-channel experiments.** These need CPU pinning, isolated cores and sometimes `perf_event_open`, which seccomp profiles and gVisor block. Either declare them out of scope for sandboxed execution or define a "measurement" tier with documented weaker isolation.
- **GPU.** This is deferred, which is fine, but GPU sieving and hashing are mainstream. Record that the isolation tier for GPU (Kata/VM passthrough vs. shared driver) is an open security question, not just a packaging task.
- **Resource accounting.** There are no per-project quotas or cost limits for hosted Kubernetes, and persistent per-project containers hold resources while idle.

### M8. Data classification and responsible disclosure are missing

Research may produce embargoed vulnerability findings or use sensitive datasets. These flow to third-party model providers, hosted control planes and object storage. The plan has a `data_class` field in `run-request.json` but defines no classes and no rules about where each class may go (which model endpoints, which pools, which storage).

**Recommendation:** define 3–4 data classes, e.g. public / project-private / embargoed / restricted-dataset. For each, state the permitted model endpoints, pools and storage, and enforce this in the orchestrator's model and connector routing as well as in workers.

---

## 4. Minor findings

| # | Finding | Recommendation |
| --- | --- | --- |
| m1 | `.agents/docs/current/` duplicates `docs/` byte-for-byte; `history/` snapshots duplicate what git already stores. INVENTORY.json hashes must be maintained by hand. | Replace snapshots with git tags (`plan-01-initial`, `plan-02-deployment`, `plan-03-current`). Keep only agent reports in `.agents/docs/`. |
| m2 | The same requirements (no orchestrator fallback, reproduction is an extension, base-image sessions, three monitor actions) are restated in 6–10 documents. | Make `DECISIONS.md` the single source and reference D-numbers elsewhere. Shrink restatements to links. |
| m3 | "CScience" is never defined in the root README. "Babbage Linux" is never defined anywhere. | **Resolved:** both defined in the README Terminology section (babbage = the user's Ubuntu 26.04 machine). |
| m4 | Evidence references point at absolute local paths (`~/Git_Projects/...`) that no other reader can verify. | Reference repository + commit + path, and note which sources are local-only. |
| m5 | No glossary for project/session/workbook/environment/kernel/attempt, although the plan depends on these distinctions. | Add a short glossary (the table in PROJECTS-AND-SESSIONS is close). |
| m6 | No success metrics or non-goals. | Add non-goals (multi-tenant SaaS, live migration, GPU, hardware labs) and a few measurable targets (cold start, warm cell latency, install time through the mirror). |
| m7 | The audit/event store has no integrity property. | Use an append-only, hash-chained event log so monitor evidence is tamper-evident to an agent that compromises the control store. This is cheap. |
| m8 | Adversarial tests appear as a one-off list in MONITORING.md. | Turn them into a maintained **adversarial fixture suite**: setup.py exfiltration, typosquat, DNS tunnel, prompt-injection PDF, proxy-bypass attempt, symlink escape. Run it in CI against every backend. |
| m9 | The OpenShell gateway with a Docker driver holds the engine socket, which is root-equivalent on the host or VM. | Prefer rootless Podman locally. Document the gateway as a host-root-equivalent trusted component. |
| m10 | A shared writable workspace between worker and orchestrator (the likeliest low-patch seam) makes the orchestrator parse untrusted files. | Orchestrator file access must not follow symlinks, must enforce size and type limits, and must never render or parse rich formats in-process. |
| m11 | `examples/deployment-profiles.json` lists "remaining_combinations" without explaining why they matter. | Either justify each or mark them "design-compatible, not certified." |

---

## 5. What the plan gets right (keep these)

- The first slice (shell + two stateful Python cells via the *retained tools*, with interruption and reconnect) is the right thing to test first.
- "An unavailable sandbox never silently causes code execution in the orchestrator container."
- Escalations are held outside the middleware RPC and resumed only through a scoped decision, with no blind replay of mutating requests.
- Alert-and-continue is distinguished from endpoint audit mode and from middleware `on_error`.
- Logical environment IDs are separate from concrete container IDs, and monitor attribution recognizes that sandbox ID ≠ session.
- A shared mutable Conda prefix is never shared across "independent" environments.
- Evidence types are separated (proof / exact computation / finite test / numerical / source claim / synthetic). Reviewer agreement is not treated as proof.
- Published frontier results do not need re-verification, while project results keep their own gates.
- The plan is consistently honest about what was inspected versus validated.

---

## 6. Suggested next steps (ordered)

1. Adopt `THREAT-MODEL.md` and `LICENSING.md` (drafted, revised) and settle the proposed decisions D43–D58 in `DECISIONS.md`.
2. Write the behavior specs for the CScience features to reimplement, and choose the harness components (W0, D51/D55/D56).
3. Start the infrastructure track: worker API, worker service, egress proxy + package mirror, OpenShell providers + MCP endpoint policies, read-only rootfs environment layout, adversarial fixture suite.
4. Pick isolation tier defaults per profile and measure Sage/fpylll performance under gVisor and Kata.
5. Define the minimum useful release and stage the placement matrix.
