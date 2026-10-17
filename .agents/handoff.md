# Handoff

Updated 2026-10-17 on `codex/research-ui`, foundation base `631dc246`. Replace; under 8 KB. Read with [STATUS.md](STATUS.md), the [plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **Active authorized work:** real arrcodex SNOVA acceptance, $200 total ceiling. The disposable lab is active; do not end the task with it left running. Preserve research artifacts before complete teardown. Keep unrelated UI 29183 and Local Codex 5175 untouched. No push/merge authorization.
- **Immediate next step:** configurable per-role reasoning effort and immutable profile pins are being implemented with an explicit audited Resume override. Actual prior responses use medium; owner wants max and an in-depth Discover research plan aimed at novel results. The live parent has been told to checkpoint/wait for the upgrade, with the deep-plan request queued. Verify actual returned effort before claiming deployment.
- **Checkouts:** development is the managed research-foundations worktree on `codex/research-ui`; the live launcher imports a separate frozen acceptance-runtime worktree at `bd738a9a`, image `acee67a0`. Never run a live service from actively edited sources. Current schema 68, worker 9, executor 3, private runtime protocol 2; upgrade together.
- **New acceptance:** actual 100 MiB OpenShell upload/hash; 512 MiB child artifact retained in host-side filesystem blobs and downloaded after worker deletion ([report](docs/agent-reports/large-artifacts-2026-10-17.md)); root Resume recovered one append without duplication. One unknown paid call remains explicitly reserved at $4 plus advertised token upper bounds. Parent has two actual folds; child folding still needs live evidence.
- **Research depth:** initial plan remained active, but a child marked its toy baseline completed too early. MAIN10/planning2/stock guidance (`f96d8d15`, 189 tests/review clear) requires evidence-based iteration for explicitly extended research; not yet deployed. Investigate/DISCOVER was loaded/declared, but supporting references initially were not read. Do not claim a novel attack from toy invariants or an agent's unverified report.
- **Adaptive allowances:** [spec](../docs/specs/adaptive-research-resources.md) records renewable time/token/turn/storage grants with parent escalation, hard outer ceilings and durable safe-point recovery. Planned, not implemented.
- **UI:** signed local sidebar/chat/research workspace, 100 MiB uploads, half-window drag pane, document filters/tabs. Synthetic preview remains for owner review. Context inspector prototype is reviewed; integration pending. Earlier foundation [acceptance](docs/agent-reports/foundation-acceptance-2026-10-17.md) and [STATUS](STATUS.md) retain historical scope.

## How work is done

- **Briefs:** one task per worktree from `origin/main`; stacked PRs name their base. Follow the [working rules](CONVENTIONS.md#working-rules).
- **Review by risk:** independent read-only review for security, storage, concurrency and new features; diff/latest-check review for tested code moves, docs and test-only fixes. Current session instructions govern whether delegation is available.
- **Model routing:** this session uses the owner-requested GPT-6.1-Sol subagents for independent reviews. Current instructions take precedence.
- **Test hygiene:** one pytest suite, at most `-n 4`; unset `SS_LIVE_GATEWAY` and `SS_INTEGRATION` for ordinary checks. Logs use `~/.cache/pt/`. Foundation fixtures use `/var/tmp/sandbox-science-test-cache/` because `/home/andy` is itself a Git checkout and security tests reject fake CLIs below it. Other fixtures use the home cache. Delete scratch by exact absolute path.
- **Migration numbers:** this branch is at 68, its foundation base at 65. Recheck main before integration; whoever merges first takes the next number, and later migrations renumber. Never edit `baseline.sql` or a shipped step.
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
- Historical labs deliberately used the weaker `sandbox-orchestrator` alias. This trial explicitly uses owner-authorized arrcodex models; the latest request is max effort with configurable roles.
- **Rules:** run from a worktree. Qualification checks go through `systemd-run --user`. Stop any forward unit before `lab_down.sh`. The lab's `env.sh` hides `gh`'s auth: use another shell. Raw evidence stays outside git.
- **CI:** `gh pr checks N --watch` right after `gh pr create` can exit at once; wait a few seconds. Rerun a stalled `test (fast)` job.

## Code pointers

Features use `hooks.py`; `harness.py` assembles them and `service.py` admits work. Research continuity is in `continuity/`, worker isolation in `worker/`, context/archive/folds in `history/`, and role transport in `confinement/`. The gateway chooses Responses or Messages adapters per alias. Launcher configuration is in `packages/launcher`; frontend in `web/workspace`. Regenerate workspace API types when response shapes change.

The owner's ledger rule: weakening a settled state needs no authority; strengthening needs the user or a passing gate. Main can reopen an obstructed question with a reason. Worker protocol changes require lockstep component upgrades; new routes alone do not.

## What is next

1. Per-role effort and explicit Resume override are signed in `43889b45`, independently reviewed and tested (215 integration plus focused gates). Deploy the frozen checkout/image at the saved checkpoint, confirm actual max effort, and obtain the requested deep SNOVA research plan.
2. Continue meaningful research/fold/Sage/recovery acceptance within the remaining shared $200 budget. Preserve artifacts and exact evidence, then verify complete disposable-lab teardown.
3. Keep signed local work unpushed/unmerged for review. Continue remaining UI/context-inspector and BACKLOG work after foundations acceptance.
