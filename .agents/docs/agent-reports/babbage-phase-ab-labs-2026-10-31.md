<!-- SPDX-License-Identifier: MIT -->
# Babbage labs: the Phase A/B items that need a real gateway and a real model (2026-10-31)

Run on babbage on 2026-10-31, from 22:20 to 23:30 UTC, against OpenShell 0.1.5 (VM driver), from a fixed detached worktree of `k8s-sync` at `ba6cc038` (lab scripts on the local branch `labs/phase-ab-2026-10-31`). Images were built locally from that tree and never pushed: `sandbox-science/orchestrator:06a5e78a` (built for this run) and `sandbox-science/minimal:48d206ba` (already present, same recipe revision). The workspace UI was built from the same tree. Four disposable roots ran, each with its own gateway, middleware, cache and worker on random loopback ports, and each was torn down completely.

- `direct_mcp.py`, variants `locked` and `inspection`: labs 1 and 3, against the owner's MCP portal on the direct path, with no model.
- `a4_approvals.py extras` and `lockdown`: lab 4 and the network half of lab 1, with no model.
- `model_lab.py`, new: labs 2, 5, 6, 7 and 8, and the host launcher's half of lab 1. It runs the whole product (`sandbox-science up`) with `[mcp_direct]` on the owner's portal and the gateway's `sandbox-orchestrator` alias for every role. That alias answered as `gpt-6-luna`.

**Model spend: USD 0.555** reported by the gateway, over 393 calls (main USD 0.28, delegated USD 0.10, reviewer USD 0.15, all priced), against the lane's USD 40. The project's local daily limit was set to USD 15 as a stop. Nothing is left running.

## Results

| # | Lab | Result | Evidence (one line) |
| --- | --- | --- | --- |
| 1a | A3 `mcp: locked` on the direct path | PASS | `e-print-mcp_corpus_stats` passed (rule `mcp.effects.read`). After a mid-session revision left it out, the stale fence still admitted it and the middleware refused it (`policy_denied`, rule `lockdown.mcp`). |
| 1b | A3 `network: allowlist` (research code, project `a4lock`) | PASS | GET example.com returned 200. GET example.org was refused at the connection, and the worker refused to admit it on demand ("the policy reaches only the hosts it names"). No `lockdown.network` record: the refusal is at the compiled OpenShell layer. |
| 1c | Revision-aware fence, runtime manager path | PASS | `GET /v1/runtime-classifications` (binding-only client) answered revision `null` before the change and `39fae2ad8cb1…` after it. `compile_egress` printed the note naming it. After the runtime sandbox was replaced, the tool was gone from the listing and refused by OpenShell in 0.01 s, with no middleware record. |
| 1d | Revision-aware fence, host launcher path | PASS | At `up` the launcher logged "admitted by the checked-in classification, without warnings". After a mid-session revision and `replace-orchestrator` it logged "admitted by the project's accepted classification revision 39fae2ad8cb1". |
| 1e | Fence fallback log line | PASS | With the worker unreachable, compile logged "…could not be read for the fence (the worker did not answer …: Connection refused); the fence admits tools by the checked-in classification until the runtime is next replaced…", then "…admitted by the checked-in classification, with a warning above". |
| 2a | A check gets only `read` connector tools | PASS | The check's `mcp_tools` queries "firecrawl monitor" and "eprint corpus" offered only read tools (`firecrawl_monitor_check`, `_checks`, `_get`, `_list`, `search_eprint`, `corpus_stats`). The task child's query loaded `firecrawl_monitor_delete` and `firecrawl_monitor_run` (write). |
| 2b | A child's outward call that asks first, under a user-edited plan | PASS | The child's `firecrawl_monitor_run` passed the fence and was held: request 79, rule `mcp.effects.write`, ceiling project. The card carried the child's delegation and brief (DA-9). |
| 2c | The same call once a session grant covers it | PASS | After request 79 was answered `session`, a new task's call, made after the user's plan edit, was refused by the fence: `plan_changed: the user changed the session's plan since you started; you make no new effects outside the session`. |
| 2d | Draft plan; tool allowed unasked; rebind | partly BLOCKED | Under a draft plan the main run cannot delegate at all (`plan is not active; activation is not USER approval`), so no child exists under a draft. The tool allowed by name was held by drift (below), not by the fence. No rebind case was run. |
| 3a | `mcp-call` / `mcp-answer` on an event-stream server | PASS | Three web-fetch calls through the portal gave 3 `mcp-call` records with arguments and 3 `mcp-answer` records, all `text/event-stream`, mode `stream`, complete. |
| 3b | Alert on a streamed answer | PASS | Default `ignore-instructions` found at offset 277; the answer passed; an `mcp-answer-alert` record was written. |
| 3c | Block on a streamed answer | PASS | Decision `cut`, `answer_blocked_lab_block`; the client got a `server_error` naming it; alert action `block`. |
| 3d | JSON-escaped payloads matched | PASS | A pattern with literal quotes matched at offset 263, while the raw answer carries `\"ignore-me\"`. The portal sends non-ASCII as raw UTF-8, not `\uXXXX`, so the escape exercised is `\"`, not `ignore`. |
| 4a | Conda channel as the escalated source | PASS | Provisioning from conda-forge raised card #3 (rule `packages.conda`, 13 files). A session grant (ecosystem conda, channel conda-forge) answered a later conda request through the relay in 4.8 s. |
| 4b | Git requests | PASS | `git ls-remote` of a public repository passed without a card. `git push` asked (card #28, rule `git-push`), was denied, and its retry got 403 `approval_denied`. Another sandbox's push asked again. |
| 4c | Grant expiry (`hours`) | PASS with a defect | `hours=0.02` gave a grant expiring 72 s later, after which another sandbox asked again. The answered sandbox kept passing reads 87 s after the answer: the middleware's host answer lasts an hour (defect 1). |
| 4d | Revocation's effect on the middleware | limitation confirmed (defect 2) | After a revoke, other sandboxes asked again, but the already-answered sandbox kept passing until the one-hour host answer ran out (approval #38). |
| 4e | Re-authentication for approvals (D54) | BLOCKED, not built | No re-authentication code in the workspace server or web; `docs/WORKSPACE.md` lists it as not built. |
| 5 | B12 parent review with a real model | PASS | A was accepted (`parent`, note "A is fine"). Feedback to B reopened it: result 2 edited `work/<B>/notes/beta.md` to revision 2, then B was accepted. C was left alone and accepted at the parent run's end (`parent_ended`). |
| 6a | A6: `write_file` outside the write area | PASS | B's `write_file notes/beta.md` was refused with `outside_write_area: … write only in work/dlg-724d…/ and in shared/`. The model's next call wrote `work/dlg-724d…/notes/beta.md`. |
| 6b | A6: a Resume continuation edits its earlier file | PASS with a defect | A child wrote `work/dlg-b3a1…/delta.md`, then slept 240 s; the runtime was deleted and replaced. After the user's Resume, a continuation delegation edited `work/dlg-b3a1…/delta.md`, the first delegation's write area. The automatic Resume before it failed (defect 6). |
| 7 | A8 check-ins with a real model | PASS with defects | 7 check-ins: 6 on turns, 1 on tokens. 5 were answered by the parent (`extend`), 1 automatically (the root's tokens, 122,482 of 100,000), and 1 by the user (the root's 40-turn limit, extended to 80 through `…/recovery/resources/decide`). The session Usage tab and the project Usage page show them (screenshots). Defects 4 and 5. |
| 8 | A10 kernel-first session | PASS | `tool-mix` on the session: 1 run, 10 cells (python 10), 0 shell calls. Over all four sessions: 58 cells against 2 shell calls (the 2026-10-25 SNOVA baseline was 53 cells against 1,155 shell calls). The model's ten terms and its answer (n = 10) check out by hand. |

## Defects

1. **A short grant outlives its expiry at the middleware.**
   - **Where:** `packages/workspace/src/sandbox_science/workspace/approvals.py`. `ledger_answer` sends every network session or project answer to the middleware as scope `host` with `HOST_HOURS = 1.0`, whatever `Decision.hours` says, and `_relay_one` sends the same.
   - **Symptom:** a grant with `hours=0.02` (`expires_at` 22:27:08) let the answered sandbox's reads pass at +87 s. Its middleware approval #31 expires 23:25:56.
   - **Suggested fix:** send `min(HOST_HOURS, remaining hours of the grant)`, both when the user answers and in the relay.
2. **Revocation does not reach the middleware.**
   - **Where:** `revoke` updates the control store only.
   - **Symptom:** a sandbox that already had the relay's answer keeps passing for up to an hour (approval #38 until 23:27:42). The module docstring says an answer "lasts minutes", which disagrees with `HOST_HOURS`.
   - **Suggested fix:** on revoke, have the worker withdraw (expire) the live host approvals the grant produced for the project, or shorten `HOST_HOURS`; and correct the docstring.
3. **The git-push card hides that it is a push (minor, UI).** It shows `GET /a4lab/none.git/info/refs` without the `?service=git-receive-pack` query; only the rule name `git-push` says it is a push. Suggested fix: show the service in the card's summary.
4. **A child's soft turn cap is also its run's hard turn limit.**
   - **Where:** `packages/orchestrator/src/sandbox_science/orchestrator/delegation/hook.py:678` and `:1025` pass `limits.task_turns` as the child run's `max_turns`.
   - **Symptom (with `--task-turns 6`):** a child at its cap checks in (`resource-aeaac4…`, "6 of 6"), and the parent extends it (total 306). But the run has no turns left: it ends `limit_reached` / `turn_limit`, the decision is marked `superseded`, and the work goes on only through automatic Resume of the `turn_limit` attempt (`resume-cccdaa…`: "2 unfinished branches: turn_limit"). The spec says "a child's only turn limit is `turn_ceiling`" (adaptive-research-resources.md). With a weak model this became 20 delegations (8 ended `turn_limit`) for one twelve-cell task.
   - **Suggested fix:** give the child run `turn_ceiling` (or its cap plus pending steps) as `max_turns`, so that only the check-in acts at the soft cap; or let an applied `extend` raise the live run's `turns_left`.
5. **Sibling continuations in one shared environment fail on the worker's kernel-observation lane.**
   - **Where:** `packages/worker/src/sandbox_science/worker/kernels.py:409` refuses a second concurrent observation of an environment with `UNAVAILABLE` "kernel observation capacity is busy". The post-tool hook (`kernel_check.py:106`) and context preparation catch it; an observation on a continuation's start path did not. Candidates are `continuity/kernels.py:220`, `:243` and `:355`, which call `observe_kernels` without a retry.
   - **Symptom:** the attempt was saved `blocked` with `transient:worker_loss: WorkerAPIError: kernel observation capacity is busy`. Six automatic Resumes in a row were blocked for that reason ("5 runs of session checkin failed in a row") before the tree completed.
   - **Suggested fix:** retry a busy answer with a short backoff on those paths (the workspace already treats it as transient), or queue observations per environment in the worker instead of refusing.
6. **An automatic Resume after a runtime replacement fails its children as `failed`.**
   - **Where:** `run_inputs.py:208` raises `ControlError(CONFLICT, "recovery prompt/tool/skill pins changed")` when a continuation's pins differ and the Resume is automatic.
   - **Symptom:** in the confined runtime this surfaces as `TransportError: control capability failed` with ERROR tracebacks. The delegation ends `failed`, "the child's harness phase failed" (`dlg-b9a6…`), and the Resume record says only "2 unfinished branches: error". The user's Resume then continued correctly ("the user's Resume accepted the change"). The pins most likely changed because a classification revision was accepted before the replacement, which changes the connector tool set; this was not verified.
   - **Suggested fix:** give the pin change its own stop reason (for example `pins_changed`) that leaves the work waiting for the user's Resume, with that reason in the Resume record, and no `failed` delegation or traceback.

Not product defects:

- **The portal changed since the 2026-10-26 pins.** `e-print-mcp_eprint_fulltext` changed and `e-print-mcp_search_eprint_fulltext` was added. Every write call to the server was held as `tool-list-drift` (request 37) until the listing was re-accepted, so the detection works; the checked-in pins need a refresh.
- **The fixture's replacement retried once too few.** `replace-orchestrator` refused twice while a child's 240 s shell held the old control connection, then succeeded once it drained, as the README describes.
- **The model took steps on its own.** After a plan was left open, the platform's plan continuation started another root run, which delegated tasks of its own. The model also refused to retry a denied tool, which is reasonable.

## Setup notes

- **The portal and its credentials.** The portal's URL was read from the personal instance's policy at run time and never written to the repository or the evidence. The Cloudflare Access file and the model key were passed by path only. The personal instance was not touched.
- **Classification revisions** were accepted by writing to the policy store (`PolicyStore.accept_classification`), as the existing fixture does, not through the workspace's draft route (CL-3). The portal's drift was re-accepted through the worker's drift route, as the workspace's review does.
- **The plan edits** in lab 2 went through the workspace's `POST …/plan/changes`, as the user's.

## Evidence

All of it is outside git. Each directory has a `SHA256.json`; the SHA-256 of that manifest file is given here.

| Lab | Directory | Manifest SHA-256 |
| --- | --- | --- |
| A3 locked + fence | `/var/tmp/ss-direct-mcp-evidence-285d6975/` | `c20f56813902fa8b16fe957a5c55707ff2c4c74d2e5c65bd332c7d967ec187f4` |
| Inspection | `/var/tmp/ss-direct-mcp-evidence-96eaa781/` | `dbc02c2fb5869692a585fef28aed6ab43c47396563508ef8e7ee6104ae3c0778` |
| A4 extras + allowlist | `/var/tmp/ss-a4lab-evidence-ddc9ca0d/` | `950503c8dea59820d0eb533a8500e93b7d8872aa2dec70d01358768b46f68bd5` |
| Real-model lab | `/var/tmp/ss-modellab-evidence-e9032889/` | `c3a5b7ac86fbbebbb59c889a0c6b4a3c03a44621a008957a17d844e9c93965d0` |

The real-model lab's `screenshots/` directory, added after export, holds three images from the built-in browser pane:
- `session-usage-checkins.jpg` (`d9c5b292…`): the session Usage tab's check-ins;
- `session-usage-agents.jpg` (`da5b2726…`): its agents' caps;
- `project-usage.jpg` (`75b87f68…`): the project Usage page.

Every fixture's teardown deleted its sandboxes, stopped its units, checked that ports, processes and driver directories were gone, and removed its root.
