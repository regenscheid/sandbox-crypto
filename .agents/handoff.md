# Handoff

Updated 2026-10-17 on `codex/research-foundations`, base `57e29536`. Replace this file; keep it under 8 KB. Read with [STATUS.md](STATUS.md), the [plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **Research foundations (D47/D87):** signed local implementation, unmerged/unpushed; see the [acceptance evidence](docs/agent-reports/foundation-acceptance-2026-10-17.md). Schema 65 supports durable tree Resume within original grants, child settlement, paged history and reconciliation. Model loops run in a separate OpenShell sandbox; replacement preserves UI/state/worker and never restarts paid work automatically. Memory bounds and installed-tool restrictions are implemented. The disposable lab passed and was fully torn down; personal services are untouched.
- **Context management is built (steps a to g); its acceptance is not met.** Lab 44 was round 1 on GPT models ([STATUS](STATUS.md#phase-and-focus)). The owner's checklist in its report is pending. Round 2 needs the Claude arms, 600,000 arms that fold twice, the control repeated and the state block's placement (CM-25) measured first. Lanes: CM-A (archive, history tools, session view, the cite-handle and gateway fixes), CM-B (the ledger's fields and provenance, cache key), CM-D (the adapters, figure charts), G (the GUI), K (skills, images).
- **Figures** are built through F5 and F9 to F11. Left: F6, F7, F8, F11b, F12, F13 and the browser lab ([BACKLOG](BACKLOG.md#repository)). **Skills:** K7a and K7b are built; generated binding restrictions are implemented; remaining skill work stays in BACKLOG.
- **Waiting for the owner:** lab 44's checklist, and Q12 to 14 of the skills spec, still the coordinator's reversible acceptance. The state block's placement is a design question for round 2, not for the owner yet.

## How work is done

- **Briefs:** one task per worktree from `origin/main`; stacked PRs name their base. Follow the [working rules](CONVENTIONS.md#working-rules).
- **Review by risk:** independent read-only review for security, storage, concurrency and new features; diff/latest-check review for tested code moves, docs and test-only fixes. Current session instructions govern whether delegation is available.
- **Model routing:** this session uses the owner-requested GPT-6.1-Sol subagents for independent reviews. Current session instructions take precedence over historical routing conventions.
- **Test hygiene:** one pytest suite, at most `-n 4`; unset `SS_LIVE_GATEWAY` and `SS_INTEGRATION` for ordinary checks. Logs use `~/.cache/pt/`. Foundation fixtures use `/var/tmp/sandbox-science-test-cache/` because `/home/andy` is itself a Git checkout and security tests reject fake CLIs below it. Other fixtures use the home cache. Delete scratch by exact absolute path.
- **Migration numbers:** this branch is at 65, its base at 57. Recheck main before integration; whoever merges first takes the next number, and later migrations renumber. Never edit `baseline.sql` or a shipped step.
- **Integration:** this session has no push or merge authorization. Preserve the primary checkout; use signed commits in the managed worktree. Recheck migration numbers and lockstep image/protocol compatibility before future integration.
- **Tracking files:** only the coordinator, or an agent whose task says so, edits STATUS, this file, PLAN-CHANGES and the plan's progress. Other PRs put their summary in the description and update the `docs/` pages and BACKLOG items they touch.
- **Never `cd` the coordinator's shell outside the repository.** It moves the Desktop session's working directory, and running subagents are told to work there. Use absolute paths.

## Prompt research

The owner's research on other products' prompts lives outside this repository. The repository holds only our own words (D56): nothing from another product's prompts or skills is copied or paraphrased. Never import OpenScience's two skills derived from a Claude Science prompt collection ([licensing](../docs/LICENSING.md)); check NOTICE files before borrowing from it.

## Secrets

- The owner's keys are files in `~/.config/sandbox-science/secrets/`: `litellm-harness.key` (the model gateway) and `cloudflare.env` (crypto-research-mcp's Access token). Agents never open, read, list, copy or print them; a program gets a path only when its task says so. Tests use synthetic tokens (`test-secret-…`).
- **The `cloudflare.env` helper** (the owner, "for now"): `[mcp_proxy] provider_env_file` makes `up` run `sandbox_science.launcher.access_provider`, which reads the file in its own process and registers the OpenShell provider when the gateway lacks it. Only a lab's own config outside the repository names the real file. Labs never copy, archive or query the gateway's store.

## Labs

- **Tooling** is in [`.agents/labs/`](labs/README.md): `lab_up.sh`, `worker_up.sh`, `lab_down.sh` and `launcher_config.py`, which writes a lab's config outside the repo.
- **The weaker model is deliberate.** Labs use the gateway alias `sandbox-orchestrator` for every role, a weaker model than the owner runs day to day, to exercise how the platform handles model errors. Do not switch models to fix such a failure: it is a finding about the platform's checks.
- **Rules:** run from a worktree. Qualification checks go through `systemd-run --user`. Stop any forward unit before `lab_down.sh`. The lab's `env.sh` hides `gh`'s auth: use another shell. Raw evidence stays outside git.
- **CI:** `gh pr checks N --watch` right after `gh pr create` can exit at once; wait a few seconds. Rerun a stalled `test (fast)` job.

## The code, briefly

- **Loop and service:** features plug in as turn hooks (`hooks.py`). `Harness` is what `run` and `serve` build; `service.py` starts runs for chat messages and comments. Delegation is `delegation/`; skills are `skills/`, with the built-in pack in `skills/builtin/` at the repository root.
- **Control store:** `orchestrator/control/`, schema version 65 on this branch; private runtime protocol and worker protocol are separately pinned.
- **Context:** `history/` holds the session archive, the history tools, the session view, the read register and its descriptors; `responses.py` and `messages.py` are the GPT and Claude clients (`model.gateway_model` picks one per alias), `folds.py`, `overview.py`, `bound.py` and `markers.py` hold the folds, and `digest.py` the state block's digest; `consistency/archived.py` ties reviews to the archive. Figures: `view_image` in the orchestrator, the frame and charts in `web/workspace/src/figures`.
- **Ledger rule** (the owner): weakening a settled state needs no authority; strengthening needs the user or a passing gate. The main agent reopens an obstructed question by itself, with a reason.
- **Worker:** a change to an existing request or response raises `PROTOCOL_VERSION` (8); a new route does not ([compatibility](../docs/COMPATIBILITY.md)).
- **Workspace:** rerun `scripts/generate_api_types.py` when a response model changes. Agent text the user approves goes through `check_agent_text`.
- **Launcher:** `packages/launcher` is the `sandbox-science` command, which also imports the config's skill packs ([quickstart](../docs/QUICKSTART.md)).

## What is next

1. Review/integrate the signed foundation branch and its acceptance report. Upgrade components together; image and control source revisions must match. There is no host-loop fallback.
2. Real-model scientific continuity, Claude folding and provider cache measurements still need separately budgeted acceptance. Production context order is unchanged after the offline comparison.
3. Return to UI and remaining product work after acceptance. The authenticated recovery API supplies paged unresolved identities and explicit operator decisions; the workspace currently exposes Resume plus blocked reasons, not a full reconciliation editor.
