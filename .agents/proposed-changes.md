# Proposed changes on the `review` branch

Date: 2026-10-10. Each change below follows from a finding in [critical-review.md](critical-review.md); finding IDs are given in brackets. Confirmed decisions (D01–D42) were not modified or overridden. New recommendations are added as **D43–D54**, marked "Proposed (review)". The exception is D45, which records the user's licensing requirement as Confirmed. The `.agents/docs/` archive and its INVENTORY.json are untouched, because they are dated snapshots.

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
- **[m4–m7]** Replace absolute `~/...` evidence paths with repo + commit references; add a glossary, non-goals and measurable targets; make the event log hash-chained (partly covered by track P).
- **Add a LICENSE file** (MIT) to this repository and to `crypto-analysis-skills` once the user confirms.
