# Proposed changes on the `review` branch

Date: 2026-10-10. Each change below follows from a finding in [critical-review.md](critical-review.md); finding IDs are given in brackets. Round 1 did not modify any confirmed decision (D01–D42). Round 2 amended D03, D05, D11, D12, D41 and D42 to record the user's decision to reimplement rather than adapt CScience. New recommendations are added as **D43–D54** (round 1) and **D55–D58** (round 2), marked "Proposed (review)". The exception is D45, which records the user's licensing requirement as Confirmed. The `.agents/docs/` archive and its INVENTORY.json are untouched, because they are dated snapshots.

Accept, amend or reject each item independently. Most edits are additive paragraphs, so reverting one is a small diff.

---

## New documents

### 1. `docs/THREAT-MODEL.md` (new) [C1, C3, M4, M5, M8]

The document contains:

- **Assets.** Credentials, embargoed results, datasets, the user's local machine, monitor integrity, approval authority.
- **Threat sources T1–T7.** Malicious packages, prompt injection, faulty agent code, deliberately studied exploit code, compromised control plane, GUI-targeted artifacts, covert exfiltration over allowed channels.
- **Trust zones Z0–Z4.** The orchestrator is classified as *semi-trusted*.
- **Isolation tiers 1–3.** Hardened container / gVisor or separate VM / microVM. Docker Desktop's shared-VM caveat and a "measurement" exception for timing and side-channel work are spelled out.
- **A control mapping** from each threat to primary controls and detection.
- **Residual risks** to accept explicitly.
- **Proposed data classes** (public, project-private, embargoed, restricted dataset) with routing rules.

*Why:* the plan has no adversary model, so its central open question ("VM boundary or container host?") had no basis for an answer.

### 2. `docs/LICENSING.md` (new) [M1, C4]

The document contains:

- **The user's requirement:** permissive dependencies, MIT target, avoid AGPL, GPL only where it does not affect the project's license.
- **Three tiers:**
  - platform code: permissive only, no GPL import or linking;
  - shipped base image: thin and permissive only;
  - runtime-installed research dependencies: Sage, fpylll, G6K, PARI and similar, installed from conda-forge into the environment volume, so the project never distributes them.

  An optional locally built warm image covers first-use latency without creating a distribution obligation.
- **A component table** with licenses verified from upstream license files on 2026-10-10. Flagged items:
  - **PyMuPDF, MinIO and Grafana are AGPL-3.0**: avoid.
  - Squid is GPL: prefer Envoy.
  - marker's model weights are not permissive.
  - The CScience upstream is proprietary.
  - `crypto-analysis-skills` has no LICENSE file.
  - Use conda-forge only, not Anaconda `defaults`.
- **Enforcement:** CI license scan for tier 1, SBOM gate for the shipped image, and license metadata recorded for runtime installs.

*Why:* licensing was not addressed, and several components the plan would naturally adopt conflict with an MIT goal. This also reflects the user's notes that many research dependencies can be installed at runtime.

---

## Edits to existing documents

### 3. `docs/DECISIONS.md` [all]

- Added D43–D54:

  | ID | Decision |
  | --- | --- |
  | D43 | Threat model and isolation tiers |
  | D44 | Egress before inspection |
  | D45 | MIT/permissive licensing (Confirmed) |
  | D46 | Thin base image plus runtime-installed GPL tools |
  | D47 | Semi-trusted orchestrator |
  | D48 | Read-only rootfs plus declared volumes |
  | D49 | Per-pool local policy ceiling |
  | D50 | Data classes |
  | D51 | W0 go/no-go criteria and a parallel MIT infrastructure track |
  | D52 | Reference fallback backend |
  | D53 | Staged placement certification and minimum useful release |
  | D54 | GUI artifact origin separation and approval hardening |

- "Decisions deliberately deferred": noted that the harness-independent infrastructure can proceed before the CScience spike.
- "Discussion order": added item 0, confirm D43–D54.

### 4. `docs/MONITORING.md` [C2, C3, M3, m8]

- New section **"Egress architecture comes before inspection."** It explains why content inspection fails for high-entropy crypto data and adds a path table:
  - no direct internet from sandboxes;
  - package installs only via an allowlisted pull-through mirror;
  - a read-only fetch proxy that allows git `upload-pack` and denies `receive-pack`, keyed on service path;
  - proxy-side DNS;
  - escalation, never alert-only, for unlisted destinations;
  - orchestrator connector fetches under the same rules.
- Conda workflow step 3: installs go through the mirror; GPL tools are installed at runtime with license recording.
- The contextual monitoring layer adds independent eBPF telemetry (Tetragon/Falco).
- The test list becomes a maintained **adversarial fixture suite**, run against every backend and tier.

### 5. `docs/ARCHITECTURE.md` [C1, C3, C4, M2, M4, M5, m10]

- **CScience integration:** notes that the proprietary patched bundle cannot be in an MIT release, refers to the D51 go/no-go criteria, and proposes the **stdio-forwarding stub seam** as the first low-patch experiment. It adds untrusted-workspace handling rules for the orchestrator.
- **Placement:** record the isolation tier per execution. Explains why all-local Docker Desktop is tier 1 unless workers use `runsc` or a separate VM. Hosted workers use a sandboxed RuntimeClass on a separate node pool.
- **Storage:** proposed read-only rootfs plus declared writable volumes, so idle preservation = digest + volumes + lockfile, with a rebuild-from-lockfile recovery path. Also the thin permissive base image and runtime Sage install.
- **Permissions:** semi-trusted orchestrator egress limits; per-pool local policy ceiling; GUI artifact origin separation, KaTeX/PDF.js hardening, and re-authenticated approvals built from observed facts.

### 6. `docs/IMPLEMENTATION-PLAN.md` [C4, M2, M3, M6]

- **W0:** stdio-forwarding seam first, plus explicit **go/no-go criteria**:
  - patch-site budget;
  - survival of one upstream bump;
  - terms permit the intended deployment;
  - no proprietary code in MIT components.

  On failure, use CScience as a behavior spec and OpenScience Desktop (MIT) as the first reuse source.
- New **track P (parallel)**: worker API and service, rootfs/volume layout, egress proxy and mirror, hash-chained event store, adversarial suite, tier probes, implemented against both OpenShell and the fallback stack.
- **W2:** Sage installed at runtime via the mirror, spoken to only over the Jupyter protocol; measure install time and per-tier performance. The exit adds adversarial fixtures and recorded tiers.
- **W3:** capture simplified by the D48 layout.
- **W5:** staged placement certification (D53).
- Ownership table: orchestrator marked semi-trusted.

### 7. `docs/OPENSHELL-EVALUATION.md` [M3, C1]

- New section **"Maturity risk and reference fallback."** It covers the v0.1.3/pre-1.0 risk and the hosted and local reference stacks (all Apache-2.0/MIT). OpenShell itself is verified Apache-2.0.
- Open questions: added a proposed answer to the VM-boundary question, linked to the threat model, and a new question about MicroVM driver support for persistent environments and the D48 layout.

### 8. `docs/ROADMAP.md` [C4, M6]

- **M0:** confirm the threat model, licensing and go/no-go criteria first; start track P in parallel; the exit includes a go/no-go verdict.
- **M1 exit:** execution at a recorded isolation tier with mirror-only egress.
- **M4:** staged placement certification.
- New section **"Minimum useful release":** local tier 2, project container and kernels, runtime Sage, PDF page anchors, mirror-only egress with block/escalate, and the existing reviews. Real use then informs the ledger and concurrent-editing work.

### 9. `docs/PROJECTS-AND-SESSIONS.md` [M2]

One sentence: the D48 layout reduces "changed filesystem state" to the declared volumes.

### 10. `README.md` [m3, M1]

- New **Terminology** section: defines CScience (and its licensing consequence) and isolation tier, and flags that "Babbage Linux" needs a definition.
- "Read first" table: added the threat model and licensing documents.
- New **License** section stating the MIT intent.

### 11. `examples/deployment-profiles.json` and `examples/run-request.json` [C1, C2, M8, m11]

- Each profile gains `isolation_tier_default` and `egress` fields. The remaining combinations are labelled "design-compatible, not separately certified initially."
- The run request gains `policy.isolation_tier_minimum` and `policy.egress_profile`. `data_class` changes from the undefined `public-fixture` to `public`, from the proposed D50 classes.
- Both files remain valid JSON and illustrative only.

---

## Recommended but not applied

These need the user's judgement, or would restructure the documents more than a review branch should:

- **[m1] Replace `.agents/docs/current/` and `history/` snapshots with git tags.** Deleting archived material is the user's call.
- **[m2] Deduplicate requirements so other documents reference D-numbers rather than restating them.** This is a broad rewrite best done after D43–D54 settle.
- **[M6] Reduce the "initial gate" scope further,** e.g. move concurrent document reconciliation or the frontier after the minimum useful release. Several of these are confirmed decisions (D23–D26, D33). Only staging (D53) is proposed.
- **[M7] A long-running job class** (multi-day sieving with application checkpointing and preemption tolerance), quotas, and a GPU isolation-tier note. Worth a short section once the minimum release is defined.
- **[m4–m7]** Replace absolute `/home/andy/...` evidence paths with repo + commit references; add a glossary, non-goals and measurable targets; make the event log hash-chained (partly covered by track P).
- **Add a LICENSE file** (MIT) to this repository and to `crypto-analysis-skills` once the user confirms.

---

# Round 2 (2026-10-10): changes after the user's responses

The user clarified five things:

- babbage is the host name of their Ubuntu 26.04 machine.
- The primary threat is agentic workers doing things they shouldn't; prompt injection from consumed papers is less of a concern.
- External services will mostly be MCP, and worker API keys should come from a secrets manager. OpenShell has features for this.
- The project should **not** use Claude Science code; the needed parts are reimplemented.
- Many research dependencies can be installed at runtime. This was already folded into round 1 as D46.

## 12. Reimplementation instead of adaptation [C4 → resolved]

- **DECISIONS.md**
  - **D03** amended: CScience is the *behavior* reference, not a code base.
  - **D05** marked superseded.
  - **D11** Confirmed: an independent harness; do not retain or patch Claude Science.
  - **D12** reopened for the new control application: SQLite first behind a repository interface, PostgreSQL only for multi-replica control.
  - **D41** amended: use the Jupyter protocol for Python/R/Sage from the start, since there is no retained JSON-lines protocol.
  - **D42** amended to drop "retain the supported store".
  - **D51** rewritten: behavior specs plus a parallel infrastructure track. The patching go/no-go criteria are gone.
  - New **D55**: an MIT harness stack (Anthropic client SDKs or LiteLLM, MCP SDK, own research-loop code; OpenCode/OpenScience Desktop as reuse candidates). The Claude Agent SDK is excluded because it drives the proprietary Claude Code CLI.
  - New **D56**: reimplementation hygiene. Do not copy code, prompts, skill text, SQL migrations or assets from the bundle.
  - The "deferred decisions" and "discussion order" text were updated to match.
- **IMPLEMENTATION-PLAN.md**
  - The first slice now runs a minimal new harness with no local execution path.
  - **W0** is rewritten as "Behavior specification and harness foundation", with a list of behaviors to specify and acceptance fixtures.
  - **W1** uses the Jupyter protocol.
  - W4 review behavior is implemented to spec, with an independently written rubric.
  - The decision table is updated.
- **ROADMAP.md**: M0 becomes "Specify behavior and choose harness components"; M1 and M5 wording updated; the patch-count requirement removed.
- **ARCHITECTURE.md**: "CScience integration before a new harness" is replaced by **"Reimplemented harness"**. The harness has no local execution path, and the orchestrator never mounts worker workspaces. The storage paragraphs now describe one embedded store.
- **README.md, PRODUCT.md, EXECUTION-PLAN.md, FEATURE-PLAN.md**: the direction text now says reimplementation. OpenScience Desktop (MIT) is marked as the first code-reuse candidate.
- **CONSISTENCY-REVIEW.md, REUSE-ASSESSMENT.md, SOURCE-STUDY.md**: a dated D11 note at the top says to read "retain/adapt" as behavior evidence. The analyses themselves are left intact.
- **OPENSHELL-EVALUATION.md**: "What the workbench must supply" and the spike text now refer to the new harness.
- **LICENSING.md**: the Claude Science row is now "Not used". New rows cover:
  - the user's own `cscience-crypto` wrapper code (reusable where it embeds no upstream code; `patches.mjs` excluded);
  - Anthropic client SDKs, the MCP SDK, OpenCode, Pydantic AI (all MIT) and LiteLLM (MIT core);
  - the Claude Agent SDK (not MIT-clean).

## 13. Threat model reprioritized around agent overreach [C1, C3]

- **THREAT-MODEL.md** rewritten.
  - Primary threats are now **A1–A4**: overreach through tools/MCP, overreach through code in workers, self-escalation, and runaway work.
  - Malicious packages, studied exploit code and a compromised control plane are secondary.
  - Prompt injection, GUI-targeted artifacts and covert channels are lower priority.
  - The assets list adds shared project state and compute/API budget.
  - The control mapping adds MCP tool allowlists, placeholder credentials, the policy advisor with auto-approval off, and spend limits.
  - New babbage notes and new residual risks: a placeholder still permits everything its token's scope permits, and resolution is per endpoint, not per binary.
- **D43** revised to name agent overreach as the primary concern.
- **D47** revised: orchestrator egress goes to the model gateway and policy-governed MCP endpoints, and running the orchestrator as an OpenShell sandbox with providers is to be evaluated.

## 14. Secrets manager and MCP [new]

The OpenShell v0.1.3 documentation (`docs/how-it-works/providers/overview.mdx`, `profiles.mdx`, `policies/schema.mdx`, `kubernetes/setup.mdx`) was read for these changes.

- **D57**: worker credentials come from OpenShell providers.
  - Sandboxes hold only placeholders. The proxy resolves real values only for endpoints the credential is bound to, and fails closed on mismatch.
  - The gateway refreshes short-lived tokens.
  - Storage is the gateway's encrypted database locally; `kubernetes-secrets`, or OpenBao through the vault driver, when hosted. HashiCorp Vault is BUSL-1.1, so it is avoided.
- **D58**: external services are `protocol: mcp` endpoints with per-project method/tool allowlists, provider credentials and least-privilege, read-only tokens.
- **MONITORING.md**: new section **"Credentials and MCP access"**. It covers how injection works, storage drivers, the documented limits (endpoint- not binary-scoped; no cookie, binary or response rewriting; token scope is the real limit), MCP endpoint policy, and the policy advisor/prover with auto-approval off. The egress table row for granted endpoints now points to it.
- **OPENSHELL-EVALUATION.md**: five new spike checks: provider secrets, credential scope, MCP policy, policy advisor/prover, and babbage kernel qualification.
- **IMPLEMENTATION-PLAN.md** track P: providers and MCP policies, plus a placeholder/mismatch fixture.
- **ARCHITECTURE.md**: the permissions section references D57/D58.
- **LICENSING.md**: HashiCorp Vault (BUSL, avoid), OpenBao (MPL-2.0), External Secrets Operator (Apache-2.0), SOPS (MPL-2.0).

## 15. babbage [m3 → resolved]

- "Babbage Linux" is replaced by "babbage (Ubuntu 26.04)" across `docs/` and `examples/`.
- The README Terminology section defines it and notes the Landlock and user-namespace checks.

## 16. Critique updated

- `critical-review.md` gains section **1a** recording the user's responses and their effect.
- C4 is marked resolved by D11, m3 is marked resolved, and the next steps now reference D43–D58.
