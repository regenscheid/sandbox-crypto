# Review notes: D-infrastructure

Baseline: main at fa456bc. Reviewed in depth: all of `packages/contracts/src` (3,805 lines, 20 modules), `policy/approvals.py`, `policy/openshell.py` (`compile_sandbox`, `compile_mcp_proxy`), `policy_middleware/evaluator.py` and `cli.py`, `mcp_proxy/proxy.py` (escalation path) and `cli.py`, `event_store/store.py`, and the worker/orchestrator/workspace code that consumes these packages (`worker_client.py`, `manager.py` digests, `provisioner.py` permission requests, `permissions.py`, `provisioning_client.py`). Skimmed: the rest of `policy` (model, evaluate, operations, package_requests, store), `package-cache` (adapter contract, store, app error handling), `gitproto.py`, and the four docs plus `POLICY-MIDDLEWARE.md`, `PROVISION-EVENTS.md`, `WORKER.md` (upgrade notes), `IMPLEMENTATION-PLAN.md` ("Shared contracts"). Skipped: package-cache adapters' per-ecosystem parsing, mcp-proxy upstream/session/JSON-RPC internals, middleware auth/TLS. Ran the six packages' tests: 7,345 passed, 10 opt-in skips, 30 s.

## Summary

The infrastructure track (policy, middleware, package cache, MCP proxy, event store) is in good shape and has barely changed since 2026-10-11/12: layering is clean (no infra deployable imports another; only the documented workspace→orchestrator exception exists), suppressions are near zero, the tests are fast, and the security behavior fails closed. It should be frozen for M2 apart from small consolidations. The part that needs attention is `contracts`, the one package here still growing fast: 504 lines on 10-11 to 3,805 lines on 10-14 (47 commits). Most of the growth is in worker read-models and the delegation run transport. The plan's shared contracts for the M2 path (Anchor, Mutation, PermissionRequest) were never written. The changes that matter most:
1. **D-01 (P1):** define how contracts stay compatible and how requests are identified. Today `extra="forbid"` applies in both directions, the protocol version is never checked, and idempotency digests hash pydantic's `model_dump_json()`, so a new defaulted field changes stored identities. The fixes so far are per-field (`exclude_if`, `model_fields_set`).
2. **D-02 (P2):** give contracts one dialect, with shared primitives, named limits and one paging check, before the W4 features add more.
3. **D-03 (P2):** decide where Anchor, Mutation and PermissionRequest live before building the ledger, PDF anchors and GUI approvals.
4. **D-04 (P2):** split `approvals.py`, define the escalation state machine once (it is now written twice) with typed records, and add a GUI-reachable approval path.
5. **D-08 (P2):** the harness has no MCP client, so the MCP proxy is unreachable from the agent. Decide whether connectors are part of the M2 exit.

## Findings

### D-01: Contract compatibility and request identity are implicit and fragile
- Where: `contracts/model.py:12-15`; `contracts/worker.py:21` (`PROTOCOL_VERSION`), `:116`; `worker/manager.py:403,1403-1409,1460-1464,2987`; `worker/provisioner.py:286,715,1387`; `orchestrator/worker_client.py:258-262,284-289,815-817`; `contracts/provisioning.py:130-139`; `contracts/tests/test_provision_upgrades.py:14-19`; `contracts/run_cells.py:196-198`; `worker/store.py:635,684,1291,1647-1663`; `docs/WORKER.md:536,571`.
- Category: architecture
- Priority: P1
- Evidence:
  - Every `Contract` is `extra="forbid", frozen=True`, for requests and responses alike. WORKER.md's rule ("upgrade the orchestrators before the workers") covers only worker→client fields. It leaves out two cases:
    - Fields that travel the other way: orchestrator requests and the control→worker document catalog (`contracts/documents.py`). Those need the worker upgraded first.
    - The workspace, which also parses worker responses (`workspace/provisioning_client.py`).
  - In practice that means lockstep upgrades, which nothing enforces. `Capabilities.protocol` is sent (`manager.py:2987`) but never read by any client. A version skew surfaces as `WorkerAPIError(INTERNAL, "unexpected worker response")` (`worker_client.py:284-289`).
  - Idempotency keys are `sha256(request.model_dump_json())` (5 call sites in the worker). Pydantic's emitted JSON is therefore a persisted identity: adding a defaulted field changes the digest of every stored operation, and a replay of the same operation ID then conflicts. The workarounds are per-field:
    - `ProvisionRequest.action` uses `exclude_if`, guarded by a byte-exact "legacy" test.
    - `JobRequest` carries "use the policy default" as "`timeout_seconds` not set" (`model_fields_set`, `manager.py:1408,1462`). The client has to call `model_dump_json(exclude_unset=...)` and `model_copy(update={"kind": "job"})` to keep the discriminator (`worker_client.py:258-262`). Any round trip through `model_validate(model_dump())` silently turns the policy default into an explicit 86,400 s.
  - Delegation uses a second digest scheme (sorted-key JSON, `run_cells.py:196-198`).
  - The worker also stores contract JSON as its rows and re-validates them on read (`store.py`), so tightening a contract validator is also a data migration. The store has no `user_version`, only column sniffing.
- Proposal:
  1. Add `contracts/canonical.py` with `request_digest(model, domain)`: domain-separated SHA-256 of sorted-key JSON from `model_dump(mode="json", exclude_defaults=True)`. Use it at all digest sites. On lookup, also accept the old digest for one release.
  2. Change `JobRequest.timeout_seconds` to `float | None = None`, and delete the `exclude_unset`, `model_copy` and `model_fields_set` handling.
  3. State the compatibility policy in CONVENTIONS and the contracts docstring. For M2: lockstep releases, with the orchestrator and workspace checking `capabilities.protocol == PROTOCOL_VERSION` on first contact and reporting "version mismatch"; bump the version on any wire change. Before independent placement (M3): a tolerant `Response` base (`extra="ignore"`), strict requests, additive-only changes, new behavior gated by `features`.
  4. Give persisted worker rows a schema version, or separate row models from wire models.
- Effort: M
- Risk and verification: the digest change can break idempotent replay of operations stored before the upgrade, so add golden-digest fixtures for each hashed request type first. Covered by `test_provision_upgrades.py` and the worker idempotency/recovery tests. Add a test that a mismatched protocol version is reported clearly.
- Depends on / enables: enables every W4 feature that adds worker fields (PDF viewer, skills, provisioning GUI) and M3 placement. Overlaps D-02 and D-07.

### D-02: The contracts package grew 7.5× in three days, in two dialects, without shared primitives
- Where: `contracts/` (see the counts below); `contracts/worker.py:88-101` vs `:119-136`; `contracts/__init__.py`.
- Category: complexity
- Priority: P2
- Evidence:
  - **Growth.** 504 lines in 7 files (1fbc7b1, 10-11 01:04); 1,440 lines (10-12 00:48); 2,838 lines (10-13 02:47); 3,805 lines in 22 files (fa456bc).
  - **Two dialects.**
    - The original W1/W2 modules (worker, environments, revisions, events, context, artifacts, model; about 1,040 lines) have 5 validators. The modules added since (about 2,550 lines) have 59.
    - The strict base config is re-declared 7 times (`run_kernels.py:33,55,65`, `lockfile_diffs.py:23` `Bounded`, `provision_observations.py:43` `Observation`, `run_api.py:25` `RunAPIModel`, `run_cells.py:38`).
  - **Primitive types redefined per module.**
    - `Counter` is defined 5 times with two upper bounds: 2**62 (`provision_observations.py:35`) and 2**63-1 (`documents.py:29`, `document_exports.py:20`, `artifact_inventory.py:18`, `output_observations.py:21`).
    - SHA-256 is typed 4 ways (`artifacts.py:13`, `document_edits.py:12`, `artifact_inventory.py:21`, `output_observations.py:22`).
    - Timezone validators are hand-written 6 times (for example `provision_observations.py:48-53,103-108,205-210`), although pydantic's `AwareDatetime` is used elsewhere.
  - **Paging rules.** The offset/limit/next-offset invariant is implemented 8 times (`provision_observations.py:167,223,266`, `lockfile_diffs.py:142`, `revision_packages.py:66`, `run_api.py:121`, `output_observations.py:119,225`).
  - **Worker limits copied as literals.**
    - `provision_observations.py:97-101,114,190,214` hard-code 2000, 4000, 64, 128, 32, 32, 8 and 512. `lockfile_diffs.py:38,44-45` hard-code 8 MiB and 20,000.
    - The named constants for these already exist in `revisions.py:130,138`, `provisioning.py:41-43` and `worker/provisioner.py:107,113`.
    - `worker/provision_observations.py:312` checks `<= 32` a third time.
  - **Validators recompute server results.** `lockfile_diffs.py:107-120` recomputes `changed_fields`. `lockfile_diffs.py:62-64` re-derives the AGPL flag with a regex copied from `worker/resolution.py:79`. The worker is trusted (Z1), so these checks catch bugs rather than defend a boundary, and they make each semantic change a three-package lockstep change.
  - **Small duplications and redundant checks.** `ShellRequest` and `JobRequest` duplicate the script/argv validator. `provisioning.py:138` re-checks a `Literal` by hand. `ProvisionOperation._installer_scope` (`provisioning.py:351-371`) bakes in worker storage details (the 64 KiB cap, the media type and the `provisioning/<id>/<label>.json` path).
  - **Public surface and tests.** `__init__` re-exports 11 of 20 modules; the other 9 are imported by submodule path (81 submodule imports vs 76 package-level imports). `contracts/tests` covers 6 of the 20 modules; the rest are tested only through worker, orchestrator and workspace tests.
- Proposal:
  1. Put `StrictContract`, `Counter`, `Sha256`, `Revision`, `AwareTime` and an `OffsetPage` base with a single page check in `model.py`.
  2. Add a `limits.py` that both the contracts and the worker import.
  3. Share the script/argv validator.
  4. Add a CONVENTIONS rule: contracts hold shapes, per-field bounds and defining cross-field rules (such as exactly-one-of), not re-derivations of server computation.
  5. Move single-consumer models next to their only user, or under `contracts.internal`: `artifact_inventory` (orchestrator only), `output_observations` (worker only), `document_exports` and `document_edits` (orchestrator only).
  6. Add one test module per contract module.
- Effort: M
- Risk and verification: mostly a type-identical refactor. Unifying `Counter`'s two bounds changes accepted values, so review that case. Run the worker, orchestrator and workspace suites (contract validators are tested mainly there). pyright strict catches renamed aliases.
- Depends on / enables: D-01; makes the next W4 contract additions reviewable.

### D-03: The plan's shared contracts for the M2 path were never created
- Where: `docs/IMPLEMENTATION-PLAN.md:69-84`; `contracts/events.py`; `orchestrator/consistency/models.py:67`; `policy/approvals.py:219-241,402-454`.
- Category: architecture
- Priority: P2
- Evidence:
  - The plan lists ExecutionContext, ArtifactRef, Anchor, Mutation, Event (with a schema version), PermissionRequest and EnvironmentRecord.
  - Only ExecutionContext, ArtifactRef and execution-only events exist, and the events have no schema version.
  - There is no Anchor: only `EvidenceAnchor` in the consistency module. There is no Mutation. Permission requests are untyped dict payloads in the event store.
  - Meanwhile contracts gained about 1,100 lines of provisioning, lockfile and revision page models and 567 lines of delegated-run transport.
  - The open M2/W4 items need exactly the missing types: ledger entries, PDF source anchors, comments on PDFs and GUI-visible permission requests.
- Proposal: before starting the ledger and the PDF viewer, define Anchor (an artifact revision plus a typed location: block or math node, PDF physical page and region, cell) and Mutation (target, expected revision, operation ID, actor) once.
  - Put them in `contracts` if the worker produces them (PDF search and render results already carry page regions); otherwise put them in the orchestrator under the W4 exception.
  - Define a typed PermissionRequest view as part of D-04.
- Effort: S for the decision, M for the types.
- Risk and verification: new code with no migration. Existing document/comment anchors in the orchestrator would need mapping (area B).
- Depends on / enables: enables the ledger, the PDF viewer and GUI approvals; D-04.

### D-04: `approvals.py` mixes the protocol, file-set grants and operator rendering; the escalation flow is duplicated; there is no GUI path
- Where: `policy/approvals.py` (keys and scopes 45-189; payloads 219-241; CLI rendering 244-313; `Approvals` fold 364-557; packages-only branches 418-428, 447-454, 496-506, 521-557); `policy_middleware/evaluator.py:408-497`; `mcp_proxy/proxy.py:662-739`; `policy_middleware/cli.py:139-177` vs `mcp_proxy/cli.py:120-148`.
- Category: duplication
- Priority: P2
- Evidence:
  - **Mixed concerns in the fold.** The table-driven key/scope design is good. But the `packages` kind adds four side maps (`_files`, `_withdrawn`, `_until`, `_context`) and kind-specific `if`s in `_take`, `describe`, `pending_lines`, `packages_requests` and `granted`.
  - **Duplicated state machine.** The answer→pending→timeout logic is written twice, about 80 lines each: synchronously in the middleware and asynchronously in the proxy. "Record, or deny with `audit_unavailable`" appears 4 times in the middleware alone.
  - **Duplicated CLI helpers.** `_read_records`, `_pending` and `_answer` are near-copies in both CLIs, and the proxy lacks `pending --request`.
  - **No source check.** `RecordLike` has no `source` (`approvals.py:96-108`), so an approval appended by any writer is honoured. That is acceptable under the threat model (all writers are Z1), but nothing will tell a future GUI approval writer apart from a buggy worker.
  - **No typed records.** Payloads are dicts parsed defensively, with no PermissionRequest type a GUI could list.
  - **Operator-only answers.** Answers are possible only through the CLI on the pool host. The MCP proxy's store is inside its VM, reachable only through `openshell sandbox exec` (MCP-CONNECTORS.md:208).
  - **Do grants belong here?** Yes: the worker's forecast and withdrawal and the middleware's enforcement must evaluate the file-set grant identically, so a shared library is the right home, but as a module of its own.
- Proposal:
  1. Make `policy/approvals/` a package with:
     - `keys.py`;
     - `records.py`: pydantic payload models with source rules (approvals and denials only from `operator`, withdrawals only from `worker`);
     - `ledger.py` (the fold);
     - `grants.py` (file sets);
     - `render.py`;
     - one `resolve(key, now)` returning Denied, Approved, NeedsRecord, Waiting, TimedOutAlert or TimedOutDeny, used by both the middleware and the proxy.
  2. Share one CLI helper between the two command lines.
  3. For W4 chat: list pending requests and accept answers through the worker API (the worker already holds the store, `permissions.py`), with re-authentication in the GUI (D54).
  4. Decide whether the proxy's requests join that store.
- Effort: M
- Risk and verification: security-relevant. Before refactoring, add a cross-component test in which the worker records a request, the operator approves it and the middleware grants exactly that file set. Existing coverage: `policy/tests/test_approvals.py`, `policy-middleware/tests/test_approvals.py` (414 lines) and the worker permission tests.
- Depends on / enables: D-03; enables GUI approvals and "waiting for approval" in chat.

### D-05: `compile_sandbox` is long but linear
- Where: `policy/openshell.py:710-1048`; `_bound_hosts` 1121; `_wildcard_hosts` 597; `admission_refusal` 675.
- Category: complexity
- Priority: P3
- Evidence:
  - 339 lines, 34 branch nodes, two closures and 15 `Finding` constructions. Helpers mutate a `findings` out-parameter.
  - It runs as sequential phases: ports (731-742), named hosts (744-803), wildcards (804-833), on-demand hosts (834-868), services (870-901), policies (903-949), credentials (950-982), and the middleware plus constant notes (983-1031; unconditional prose at 889, 996, 1005, 1016).
  - `admission_refusal` repeats the port and open-internet reasoning.
  - Unchanged since 10-11. Covered by 82 tests, including a randomized property test and a golden check of `examples/openshell` (`test_openshell.py:1395-1405`).
- Proposal: compute a `_HostPlan` once, shared by `compile_sandbox` and `admission_refusal`; split out `_service_policies()` and `_credential_policy()`; move constant notes to a module tuple; return findings instead of mutating them. The aim is a top-level function of about 60 lines.
- Effort: S–M
- Risk and verification: low, because the golden YAML catches output changes. Do it only when the function is next touched (for example M3 driver differences). It is not on the M2 path.
- Depends on / enables: none.

### D-06: Three grammars for the package-cache URL layout; conda and pypi lack the cross-check
- Where: `policy/package_requests.py` (the middleware's view); `package_cache/adapters/*.py` (the cache's); `worker/resolution.py:262-314` (builds grant paths); `policy/approvals.py:543`; `package-cache/tests/test_adapter_{cargo,go,hackage,npm,opam}.py` (`test_policy_classifies_what_the_adapter_serves`).
- Category: tests
- Priority: P2
- Evidence:
  - A file-set grant matches by exact path string. The worker-built path, the path OpenShell canonicalizes, and the cache route must therefore agree byte for byte.
  - Cross-check tests exist for five ecosystems, but not for the two that provisioning uses (`test_adapter_conda.py` and `test_adapter_pypi.py` do not import the policy classifier).
  - A mismatch fails closed (the download is denied), so the risk is functional, not a security hole.
- Proposal:
  1. Add "policy classifies what the adapter serves" tests for conda and pypi.
  2. Add a test that `resolution.py`'s grant paths are routes the cache serves.
  3. Later, consider generating both grammars from one route table.
- Effort: S
- Risk and verification: tests only.
- Depends on / enables: provisioning reliability in the M2 run.

### D-07: Shared vocabulary is copied across packages
- Where:
  - **Reason codes:** `policy_middleware/evaluator.py:58-96` and `mcp_proxy/reasons.py` (9 shared codes plus the layer→code mapping). The worker matches the string `"approval_required"` (`provisioner.py:1458,1829`; `resolution.py:543`), and `loop.py:52` mentions it in prompt text.
  - **PEP 503 name normalization:** `revision_packages.py:45`, `lockfile_diffs.py:102`, `package_requests.py:139`, `operations.py:241`, `worker/resolution.py:195`, `package_cache/adapters/pypi.py:69`.
  - **Duplicate-key-rejecting JSON parsers:** 14 `object_pairs_hook` implementations in 7 packages (for example `contracts/run_api.py:157`, `workspace/provisioning_client.py:121`, `mcp_proxy/jsonrpc.py:54`).
  - **Canonical JSON:** 33 ad hoc `sort_keys=True` encodings, 8 of them in contracts, with differing `ensure_ascii`.
  - **ID patterns:** `policy/store.py:30-36` copies contracts' `Identifier` and `PolicyRevision` patterns.
- Category: duplication
- Priority: P3
- Evidence: as listed under Where. Changing one copy of a reason code silently breaks the worker's string match.
- Proposal: add `policy.reasons` (codes and layer mapping), `policy.names.pep503()`, and `contracts.jsonio.strict_loads(raw, cap)` plus `contracts.canonical` (D-01). `event-store` keeps its own standard-library copy.
- Effort: S–M
- Risk and verification: low. Grep-able, and the existing tests cover the behavior.
- Depends on / enables: D-01.

### D-08: The MCP path is not connected to the harness, and its deployment exists only in lab scripts
- Where: `orchestrator/src` and `workspace/src` (no MCP client); `policy/openshell.py:1070` (`compile_orchestrator`) and `:1196` (`compile_mcp_proxy`), called only by `scripts/render_openshell_examples.py` and `.agents/labs/compile_proxy_policy.py` (with an unrestricted ceiling); `docs/MCP-CONNECTORS.md:206-218`; `docs/ROADMAP.md:59`.
- Category: architecture
- Priority: P2
- Evidence:
  - The 2,800-line proxy is reachable only by worker code inside sandboxes; the agent cannot call a connector.
  - Per MCP-CONNECTORS.md's "Not built yet": no connector-auth service; approved tools are not compiled into the proxy fence, so an escalated tool cannot take effect; no test against crypto-research-mcp, which needs OAuth.
  - The roadmap's tier 1 target includes crypto-research-mcp, and the plan says the M2 run needs connector authorization.
- Proposal: decide whether connectors are part of the M2 exit.
  - If they are, the minimal path is:
    1. Harness MCP tools over Streamable HTTP to the proxy.
    2. A token placed into the provider by hand.
    3. Move the lab compile script into a product command (worker CLI or proxy CLI).
    4. Compile approved tools into the fence.
  - If not, record it as M3 work and freeze mcp-proxy.
- Effort: M–L
- Risk and verification: new code. The proxy's own tests (276) and labs 13, 15 and 17 cover its side.
- Depends on / enables: the paper-acquisition part of "paper → Sage" if papers come from connectors; D-04 (MCP approvals).

### D-09: Smaller scaling and concurrency issues in the approval path
- Where: `policy/approvals.py:371-385`; `policy_middleware/evaluator.py:432-437,472-482`; `policy_middleware/events.py:24-30`; `policy_middleware/service.py:236-237`; `policy_middleware/cli.py:139-142`; `mcp_proxy/proxy.py:679-681`; `mcp_proxy/cli.py:120-123`.
- Category: concurrency
- Priority: P3
- Evidence:
  - **Unbounded fold.** The `Approvals` fold is unbounded, and the store has no retention. The middleware folds the whole store from sequence 0 inside the first escalated evaluation after a restart, whose OpenShell budget is 500 ms, and `_read_records` materializes a list.
  - **Blocking read in the proxy.** The proxy reads SQLite synchronously on its event loop.
  - **Duplicate-request race.** Two concurrent identical escalations (gRPC thread pool) can both see `pending is None` and record two requests. The second then stays "unanswered" in `pending` indefinitely.
- Proposal:
  1. Prime the approvals at startup, outside any request.
  2. Read only approval-relevant kinds (`WHERE kind IN …`).
  3. Wrap the proxy's read in `asyncio.to_thread`.
  4. Take a per-fingerprint lock around the pending→record step.
  5. Design store retention before M3.
- Effort: S
- Risk and verification: add a concurrent-escalation test and a large-store startup test.
- Depends on / enables: D-04 (folds into its `resolve`).

### D-10: The delegated-run contracts are unwired and defensively over-built
- Where: `contracts/run_api.py` (207 lines), `run_cells.py` (221), `run_kernels.py` (139); `run_cells.py:130-202` (`run_cell_bytes`, 73 lines, 25 branches); `run_api.py:162-207`; `orchestrator/worker_client.py:442-610`; `worker/app.py:329-331`; `contracts/document_edits.py`.
- Category: over-engineering
- Priority: P3
- Evidence:
  - These 567 lines (15% of contracts) serve client methods used only by `child_controller` and `child_tools`, which no production module imports. The worker routes are registered but enabled only for `run_controller` clients.
  - The serializers reach into pydantic internals (`object.__getattribute__(value, "__dict__")`, `__pydantic_extra__`) to guard against in-process construction that bypasses validation. The threat model does not cover that: orchestrator code is trusted, and its Z2 risk is agent-selected actions.
  - `document_edits.py` is consumed only by `documents/worker_source.py`, which nothing imports.
- Proposal: follow the owner's decision on delegation (area A's lead).
  - If delegation is parked, move these modules to `contracts.internal` (out of the package's public `__init__`), or delete them together with the client.
  - If they are kept, replace the reflection with `model_dump(mode="json")` plus the shared canonical helper (D-01).
- Effort: S
- Risk and verification: the worker run API tests (`test_run_api.py`, `test_run_dispatch.py`) and `orchestrator/test_run_client.py`.
- Depends on / enables: A's delegation findings; D-01.

### D-11: The docs drift from the code
- Where: `docs/EVENT-STORE.md:3,5,13,"Not yet done"`; `docs/POLICY.md:3`; `docs/MCP-CONNECTORS.md:66`; `mcp_proxy/reasons.py` (`proxy_busy`); `docs/WORKER.md:536,571`.
- Category: docs drift
- Priority: P2
- Evidence:
  - **Stale decision status.** EVENT-STORE.md calls D74 "proposed", POLICY.md calls D68 "proposed" and MCP-CONNECTORS.md calls D75 "proposed". All three are Confirmed in DECISIONS.md.
  - **Stale writer list.** EVENT-STORE.md says only the middleware writes to the store and the worker is "to follow". The worker already writes permission requests, withdrawals and `provision.*` events into the middleware's store.
  - **Incomplete record kinds.** The kinds table omits `approval`, `denial`, `approved` and `provision.*`, and no registry of kind, source and owning doc exists.
  - **Undocumented reason code.** `proxy_busy` does not appear in MCP-CONNECTORS.md.
  - **Compatibility rule.** The rule is stated only in WORKER.md, and omits the workspace and the request direction (D-01).
- Proposal:
  1. Fix the three status lines.
  2. Add a "Record kinds" table to EVENT-STORE.md.
  3. Document `proxy_busy`.
  4. Move the compatibility policy into CONVENTIONS.
- Effort: S
- Risk and verification: documentation only.
- Depends on / enables: D-01.

### D-12: The worker and the middleware are coupled through shared files on one host
- Where: `worker/settings.py:26,117-123`; `worker/permissions.py:18-19`; `policy/store.py:1-15`; `docs/EVENT-STORE.md` ("Several hosts").
- Category: coupling
- Priority: P3
- Evidence:
  - The worker writes the middleware's policy store and its `events.db`. That is by design (D73, D74), and the coupling goes through libraries, so the import rule holds.
  - It does mean the worker must share a filesystem with the gateway-side middleware. That is fine for babbage, but blocks hosted placement, where the middleware runs inside the OpenShell gateway.
- Proposal: no change for M2. Before M3, put these stores behind a small store service or the worker API (which D-04's GUI path would start).
- Effort: L (M3)
- Risk and verification: not applicable for M2.
- Depends on / enables: D-04, M3.

### D-13: A few package-cache tests rely on timing
- Where: `package-cache/tests/test_app.py:457,514,966,999`; `test_upstream.py:816,878`.
- Category: tests
- Priority: P3
- Evidence:
  - Fixed `sleep(0.1–0.2)` calls followed by assertions of positive progress (for example `:514`: "the other two have downloaded" and then `upstream.count == 1` for all four). These can flake under CI load.
  - Sleeps that assert absence (`test_upstream.py:1032`) are safe.
  - Otherwise the infra tests are healthy: 7,345 tests in 30 s, and 23 sleeps across six packages.
- Proposal: replace positive-state sleeps with the existing `until(...)` helper.
- Effort: S
- Risk and verification: test-only change.
- Depends on / enables: none.

## Keep

- **Layering.** The measured import graph shows no infra deployable importing another. `contracts`, `policy` and `event-store` import no deployable. The only crossing is the documented workspace→orchestrator exception. Cross-package tests (the worker's forecast vs the middleware decision) are valuable.
- **The policy library's design:** classification, then evaluation, then a `Decision`, with the hard floor, the pool ceiling and most-restrictive-wins. A cached content-addressed policy store. Golden rendered OpenShell examples. Table-driven approval keys and scopes.
- **The event store:** small, standard library only, WAL with `synchronous=FULL`, group commit, a verifiable chain with off-host checkpoints, and measurements in the doc.
- **The package cache's adapter contract:** pure `route()` functions, with the core doing all I/O, plus crash-safe blob/index ordering and the adapter↔policy cross-check tests (extend them, D-06).
- **Fail-closed recording.** Alerts and approvals proceed only once recorded (`audit_unavailable`). Broad excepts in infra are few and deliberate (deny, or clean up and re-raise). Infra has zero `reportPrivateUsage` disables and few suppressions.
- **`Capabilities.features`,** the right mechanism for evolving the worker API (D-01 builds on it).
- POLICY-MIDDLEWARE.md and PACKAGE-CACHE.md match the code (settings keys and reason codes checked).
- **Recommendation:** freeze policy, middleware, package cache, event store and MCP proxy for M2, except for D-04, D-06 and D-09 where W4 needs them.

## Questions for the owner

1. Must the orchestrator, workspace and worker be upgradable independently before M3? If not, declare lockstep releases now and check the protocol version (D-01).
2. Are MCP connectors (crypto-research-mcp) part of the M2 exit? That decides whether a harness MCP client and OAuth come before the exit run (D-08).
3. For the M2 exit run, may permission requests be answered with the CLI on babbage, or must chat show them and let you answer in the GUI (D-04)?
4. Should delegated-run transport be parked (moved to internal or deleted) until the plans-and-delegation spec is confirmed (D-10)?
5. Should Anchor and Mutation live in `contracts` (if worker PDF results produce anchors) or in the orchestrator (D-03)?
