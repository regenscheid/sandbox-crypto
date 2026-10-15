# Handoff

Updated 2026-10-15, main `b8487377`. For the next coordinator: where things stand, how work is done, and what is next. This file is replaced, not appended to, and stays under about 6 KB. Read it with [STATUS.md](STATUS.md), the [refactoring plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **M2's exit checkpoint was met** in [lab 39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md), with two open conditions: B5 (the connector's 72 tools cost about 17,000 tokens in every call) and B6 (numbers the model copies are not checked). [PLAN-CHANGES](PLAN-CHANGES.md) records the decisions made on the way.
- **In flight:** the ledger's frontier stack, #304, #310, #321 and #323 in that order, under fixes for a review that found three high defects, and #326 off main (lane S, with B6 queued after); B5's on-demand tool loading, #332, #334 and #335 stacked in that order (lane O).
- **Next:** the first milestone for real use, plan items 7 and 9: delegation and MCP connectors. The owner chose the "check this claim" delegation as the first delegation piece. Phase 5 follows, with the domain packages moving out of the orchestrator.
- **Lab state:** nothing is running; lab 39 was torn down completely.

## How work is done

- **Briefs:** one task per agent, in its own worktree, with the plan's [working rules](CONVENTIONS.md#working-rules) in every brief. Re-check each finding against `origin/main`. Stacked PRs name their base.
- **Review by risk** (the owner, to save tokens). A full adversarial review by a separate read-only agent for security paths (policy, approvals, MCP, sandbox confinement, auth), storage migrations, concurrency and new features. A light check, where you read the diff and see every check green on its latest run, for code moves backed by tests, docs, test-only PRs and fixes that implement a reviewer's suggestion.
- **Model routing:** Sonnet for low-risk implementation, tests, docs, light reviews, rebases, CI fixes and status syncs. Opus for security paths and their reviews, storage migrations, concurrency, designs and specs, and anything a Sonnet result leaves in doubt. 
- **Test hygiene:** `pytest -n 4` at most, only the packages touched, one suite at a time: babbage is shared with labs. `--basetemp` goes under `/home/andy/.cache/pt/<task>/`, never `/tmp`, whose tmpfs filled twice and then broke the Bash tool. Delete scratch by absolute path, with no glob. `--count` needs `-o tmp_path_retention_policy=failed`.
- **Merging** green PRs, as the owner asked: `gh pr merge N --merge`, then delete the remote branch once the PR reads MERGED. Never use `--delete-branch` on a base with a PR stacked on it: it closes the dependent, and a closed PR's base cannot change. Merge a stack from the bottom: retarget the next PR (`gh pr edit N --base main`), rebase it, renumber its migration, wait for its checks. Then fast-forward the main checkout, run `uv sync --locked` and remove clean merged worktrees. In zsh scripts end each step with `|| exit 1`.
- **Tracking files:** only the coordinator edits STATUS, this file, PLAN-CHANGES and the plan's progress. Other PRs put their summary in the description and update the `docs/` pages and BACKLOG items they touch.

## Secrets

- The owner's keys are files in `~/.config/sandbox-science/secrets/`: `litellm-harness.key` (the model gateway) and `cloudflare.env` (crypto-research-mcp's Access token). Agents never open, read, list, copy or print them; a program gets a path only when its task says so. Tests use synthetic tokens (`test-secret-…`).
- **The `cloudflare.env` helper** (the owner, 2026-10-15, "for now"): `[mcp_proxy] provider_env_file` makes `up` run `sandbox_science.launcher.access_provider`, which reads the file in a process of its own and registers the OpenShell provider when the gateway lacks it. Only a lab's own config outside the repository names the real file; the coordinator gives its path when a lab runs. The token then sits in the lab gateway's encrypted store, whose database and key labs never copy, archive or query.

## Labs

- **Tooling** is in [`.agents/labs/`](labs/README.md): `lab_up.sh`, `worker_up.sh`, `lab_down.sh`, and `launcher_config.py`, which writes a lab's launcher config outside the repository (`--mcp-proxy`, `--provider-env-file`).
- **Rules:** run from a worktree, not the main checkout, whose install serves the lab. Qualification checks go through `systemd-run --user`, since Claude Desktop's shells are exempt from babbage's user-namespace restriction. Stop the forward unit and run `lab_down.sh` for a complete teardown. The lab's `env.sh` hides `gh`'s auth: use another shell. Raw evidence stays outside git.
- **CI:** `gh pr checks N --watch` right after `gh pr create` can exit at once; wait a few seconds. Rerun a stalled `test (fast)` job.

## The code, briefly

- **Loop and service:** features plug in as turn hooks (`hooks.py`). `Harness` is what `run` and `serve` build; `service.py` starts runs for chat messages and comments. Tools: `connectors.py`, `ledger/`, `memory_tools.py` ([ledger](../docs/LEDGER.md), [memory](../docs/MEMORY.md)).
- **Control store:** `orchestrator/control/`, schema version 25. Append a step to `MIGRATIONS`; never edit `baseline.sql` or a shipped step.
- **Worker:** `manager.py` is a facade. A change to an existing request or response raises `PROTOCOL_VERSION` (5); a new route does not ([compatibility](../docs/COMPATIBILITY.md)).
- **Workspace:** rerun `scripts/generate_api_types.py` when a response model changes. Agent text the user approves goes through `check_agent_text` and `ApprovalText`.
- **Launcher:** `packages/launcher`, the `sandbox-science` command ([quickstart](../docs/QUICKSTART.md)); it runs the other members as commands.
- **Policy and MCP:** the example policy allows crypto-research-mcp's tools except those that act on the world; `portal_*` stays denied.

## What is next

1. Land the frontier stack once its fixes pass review, then #326, and lane O's B5 stack (#332, #334, #335).
2. B5, then B6, so the connector path fits a day and checks its numbers.
3. Delegation, starting with "check this claim", and MCP for real research ([plan](refactor-plan/PLAN.md), Phase 4 items 7 and 9).
4. Phase 5, and the after-M2 items in [BACKLOG](BACKLOG.md).
