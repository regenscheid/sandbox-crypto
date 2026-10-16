# Handoff

Updated 2026-10-15, main `0195f834`. For the next coordinator: how work is done and what to watch for. This file is replaced, not appended to, and stays under about 6 KB. Read it with [STATUS.md](STATUS.md), the [plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **The first milestone for real use is met** in [lab 40](docs/agent-reports/babbage-openshell-lab-40-2026-10-15.md). [Lab 41](docs/agent-reports/babbage-openshell-lab-41-2026-10-15.md) checked what merged after it, and its bugs are fixed. No lab has run since, and nothing is running.
- **Work is paused** (the owner, 2026-10-15). The owner is deciding how to manage the context of long runs ([STATUS](STATUS.md#open-questions)). Until then start no new work, and let work in flight finish.
- **Two PRs are open.** #436, the figures spec, waits for the owner's decisions on its eight questions. #437, skills K6a, is a draft on hold by the owner's instruction: do not review or merge it until the owner decides.

## How work is done

- **Briefs:** one task per agent, in its own worktree on a branch from `origin/main`, with the plan's [working rules](CONVENTIONS.md#working-rules). A stacked PR names its base.
- **Review by risk** (the owner, to save tokens). A full adversarial review by a separate read-only agent for security paths (policy, approvals, MCP, sandbox confinement, auth), storage migrations, concurrency and new features. A light check, reading the diff and seeing every check green on its latest run, for code moves backed by tests, docs, test-only PRs and fixes a reviewer suggested.
- **Model routing:** Sonnet for low-risk implementation, tests, docs, light reviews, rebases, CI fixes and status syncs. Opus for security paths and their reviews, migrations, concurrency, designs and specs.
- **Test hygiene:** `pytest -n 4` at most, only the packages touched, one suite at a time: babbage is shared with labs. `--basetemp` goes under `~/.cache/pt/<task>/`, never `/tmp`, whose tmpfs filled twice. Delete scratch by absolute path with no glob, or Claude Code stops to ask the owner. `--count` needs `-o tmp_path_retention_policy=failed`.
- **Migration numbers:** the control store has one ordered list of steps, and main is at 43. Whoever merges first takes the next number; every other open PR with a migration renumbers and says so (#437 holds 44). Never edit `baseline.sql` or a shipped step.
- **Merging** green PRs, as the owner asked: `gh pr merge N --merge`, then delete the remote branch once the PR reads MERGED. `--delete-branch` on a base with a PR stacked on it closes the dependent, and a closed PR's base cannot change. So retarget each dependent first (`gh pr edit N --base main`), then merge the base, rebase the dependent, renumber its migration and wait for its checks. In a script, end each zsh step with `|| exit 1`. Then fast-forward the main checkout and run `uv sync --locked`.
- **Tracking files:** only the coordinator, or an agent whose task says so, edits STATUS, this file, PLAN-CHANGES and the plan's progress. Other PRs put their summary in the description and update the `docs/` pages and BACKLOG items they touch.
- **Never `cd` the coordinator's shell outside the repository.** A `cd` to another folder, such as the owner's research worktree, moves the Desktop session's working directory, and running subagents are told to work there. Use absolute paths.

## Prompt research

The owner's research on other products' prompts lives outside this repository, as does lane K's triage of it. The repository holds only our own words (D56): nothing from another product's prompts or skills is copied or paraphrased. Never import OpenScience's two skills derived from a Claude Science prompt collection ([licensing](../docs/LICENSING.md)); check NOTICE files before borrowing from it.

## Secrets

- The owner's keys are files in `~/.config/sandbox-science/secrets/`: `litellm-harness.key` (the model gateway) and `cloudflare.env` (crypto-research-mcp's Access token). Agents never open, read, list, copy or print them; a program gets a path only when its task says so. Tests use synthetic tokens (`test-secret-…`).
- **The `cloudflare.env` helper** (the owner, "for now"): `[mcp_proxy] provider_env_file` makes `up` run `sandbox_science.launcher.access_provider`, which reads the file in its own process and registers the OpenShell provider when the gateway lacks it. Only a lab's own config outside the repository names the real file; the coordinator gives its path when a lab runs. Labs never copy, archive or query the gateway's store.

## Labs

- **Tooling** is in [`.agents/labs/`](labs/README.md): `lab_up.sh`, `worker_up.sh`, `lab_down.sh` and `launcher_config.py`, which writes a lab's config outside the repo.
- **The weaker model is deliberate.** Labs use the gateway alias `sandbox-orchestrator` for every role, a weaker model than the owner runs day to day, to exercise how the platform handles model errors. Do not switch models to fix such a failure; treat it as a finding about the platform's checks.
- **Rules:** run from a worktree, not the main checkout. Qualification checks go through `systemd-run --user`, since Claude Desktop's shells are exempt from babbage's user-namespace restriction. Stop any forward unit before `lab_down.sh`. The lab's `env.sh` hides `gh`'s auth: use another shell. Raw evidence stays outside git.
- **CI:** `gh pr checks N --watch` right after `gh pr create` can exit at once; wait a few seconds. Rerun a stalled `test (fast)` job.

## The code, briefly

- **Loop and service:** features plug in as turn hooks (`hooks.py`). `Harness` is what `run` and `serve` build; `service.py` starts runs for chat messages and comments. Delegation is `delegation/`; skills are `skills/`, with the built-in pack in `skills/builtin/` at the repository root.
- **Control store:** `orchestrator/control/`, schema version 43.
- **Worker:** a change to an existing request or response raises `PROTOCOL_VERSION` (5); a new route does not ([compatibility](../docs/COMPATIBILITY.md)).
- **Workspace:** rerun `scripts/generate_api_types.py` when a response model changes. Agent text the user approves goes through `check_agent_text`.
- **Launcher:** `packages/launcher` is the `sandbox-science` command, which also imports the config's skill packs ([quickstart](../docs/QUICKSTART.md)).

## What is next

1. The owner's decision on context management, from a survey of libraries and a close study of how Claude Science and OpenScience manage context (under way). No live evaluation is planned. Nothing else starts before the decision.
2. Then the paused work: skills K6a (#437) onward, a lab with skills in runs to confirm labs 40 and 41's fixes, the figures spec (#436), [BACKLOG](BACKLOG.md) and Phase 5.
