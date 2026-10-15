# Handoff

Updated 2026-10-14, main `959267c`. For the next agent: where things stand, how work is done, and what is next. This file is replaced, not appended to, and stays under 8 KB. Read it with [STATUS.md](STATUS.md), the [refactoring plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **The code:** M1 passed on babbage. Most of M2 is built (see [STATUS](STATUS.md#what-works-today)): chat with a live run transcript, the run service, a PDF viewer, user-grade review views and an MCP client. A real model answered one call through the owner's gateway. The ledger's store, links, tools and read routes are built; its GUI views are in review.
- **The plan:** Phases 0 to 3 are done apart from leftovers, and Phase 4 is under way. The plan's [progress section](refactor-plan/PLAN.md#progress) lists each finding with its PR, and [PLAN-CHANGES](PLAN-CHANGES.md) the decisions made on the way.
- **In flight:** the ledger's views #259 and #260 (lane G); `session open` #261, the first of lane W's stack for the local launcher (`sandbox-science up`). Merged since the last sync: the ledger backend (#252 to #255), the specs for file recording and project memory (#251, approved, not built) and the worker's deleted-file report (#258).
- **Lab state:** nothing is running. No lab has run since lab 36, so the worker changes since then have not run on OpenShell ([known issues](STATUS.md#known-issues)).
- **Secrets:** the owner's gateway and Access keys are files that agents never read ([conventions](CONVENTIONS.md#how-work-is-done)).
- **Heartbeat:** a conversation task, *Retry stalled orchestrator*, checks every five minutes and resumes a stalled session with the same model.

## What a new agent needs to know about the code now

- **Loop and service:** features plug in as turn hooks (`orchestrator/hooks.py`). `Harness` (`harness.py`) is what `run` and `serve` build; `service.py` starts runs for queued chat messages, and `chat.py` delivers them and writes run events. Connector tools come from `connectors.py`, and the ledger's tools from `orchestrator/ledger/` ([ledger](../docs/LEDGER.md)). A run record carries `role`, `parent` and `delegation`.
- **Control store:** `orchestrator/control/` holds `ControlStore` (`read()` and `write()`), the migration list (version 18) and the shared pieces (`Receipts`, `EventLog`, `LeasedNotices`, `FeedCursor`, `TextRevisions`). To change the schema, append a step to `MIGRATIONS`; never edit `baseline.sql` or a shipped step. Tables check their values when written, and a migration that a row fails leaves the store unchanged. Stacked PRs that add steps renumber on rebase.
- **Worker:** `manager.py` is a facade over `dispatcher`, `journal`, `recovery`, `connections`, `publication`, `kernels` and others; `build_worker` builds it. The executor's `SANDBOX_FILES` use only the standard library, and a test checks it. Any change to what crosses the worker API raises `PROTOCOL_VERSION` (now 5; [compatibility](../docs/COMPATIBILITY.md)).
- **Workspace:** routes are plain handlers with one error mapping. The browser shares `lib/http.ts`, `lib/poller.ts`, `lib/mutation.ts` and the list, detail, history and write pieces. Rerun `scripts/generate_api_types.py` when a response model changes.
- **Policy and MCP:** `policy/approvals` has one `resolve`. `sandbox-science-mcp-proxy compile-policy` compiles the proxy's fence. The example policy allows crypto-research-mcp's tools except those that act on the world, which ask first.

## How work is done now

- **Follow the plan and its [working rules](CONVENTIONS.md#working-rules),** and put them in every brief: production callers only, PRs of about 800 product lines at most, the [threat model](../docs/THREAT-MODEL.md) as the bar for defenses, characterization tests first, evidence out of git, no new subsystem in `loop.py`, `manager.py` or `create_app`.
- **Re-check each finding** against current `origin/main`. The notes were written against `6e9608a`, and much has moved.
- **Parallel lanes:** one agent per task, in its own worktree, with a written brief. Stacked PRs name their base.
- **Tracking files:** only the agent coordinating a wave edits STATUS, the handoff, PLAN-CHANGES and the plan's progress. Other PRs put their summary in the PR description and update the docs and BACKLOG items their change affects.
- **Review by risk:** a full adversarial review by a separate read-only agent for security paths, storage migrations, concurrency and new features; a light check for code moves, docs, test-only PRs and fixes that implement a reviewer's suggestion.
- **Merging:** the coordinating agent merges green PRs, as the owner asked, then fast-forwards the main checkout, runs `uv sync --locked` and removes clean merged worktrees.

## What is next

1. Land the open PRs above and start the launcher's next steps.
2. The ledger: decisions and the gate, evidence, the frontier, flags, statement math checks and the pack import; its views are #259 and #260.
3. Automatic file recording and project memory ([specs](../docs/specs/research-artifacts.md#files-from-executions-and-delegated-runs), [memory](../docs/specs/project-memory.md)): the worker now reports deleted files, so the harness's recorder, the artifact list and the project settings come next; the memory tool after.
4. A whole run with the real model. The owner then registers the MCP provider, and a first call goes through the proxy.
5. A babbage lab for the worker changes since lab 36, then the M2 exit lab.
6. After the exit lab: delegation and MCP for real research, then Phase 5, which includes moving the domain packages out of the orchestrator.

## Gotchas

- **The lab's `env.sh` breaks `gh`:** it points `XDG_CONFIG_HOME` at the lab. Use another shell.
- **Watching CI too early:** `gh pr checks N --watch` right after `gh pr create` can exit with "no checks reported". Wait a few seconds.
- **A stalled `test (fast)` job:** rerun it, and read the heartbeat lines in its log.
- **Repeat test runs:** `--count` needs `-o tmp_path_retention_policy=failed`.
- **The main checkout is live during a lab:** the lab's cache and middleware run from its editable install. Use worktrees.
- **AppArmor:** babbage restricts unprivileged user namespaces; Claude Desktop's shells are exempt, so qualification checks run through `systemd-run --user`.
- **The forward unit restarts forever:** stop it (`labs/lab_down.sh`) before a lab teardown. Lab scripts run only at the commit their report names.
