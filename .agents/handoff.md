# Handoff

Updated 2026-10-17 on `codex/research-ui`, foundation base `631dc246`. Replace; under 8 KB. Read with [STATUS.md](STATUS.md), the [plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **Requested episode delivered:** the reviewed SNOVA DISCOVER plan is revision 8, notebook revision 11, active scientific plan version 7. All three later max-effort children and the parent completed their scoped handoffs; no verified novel attack. [Final acceptance report](docs/agent-reports/snova-research-acceptance-2026-10-17.md) has exact evidence and limits.
- **Preservation and shutdown:** the local acceptance cache holds the research export/archive: current and historical documents/artifacts, sources, ledger, plans and 1,977 original visible messages. This is not an automatic control-store import or kernel snapshot. All disposable VMs, eight lab units, 21 captured processes, owned state and listening ports are gone. Preserve UI 29183 and Local Codex 5175. Do not restart paid research implicitly.
- **Checkouts:** development remains the managed research-foundations worktree on `codex/research-ui`. Final frozen runtime/image `e2cf7f42` passed 17 container/Landlock checks; its worktree is archived. Schema 68, worker 9, executor 3, private runtime protocol 3; upgrade together. MAIN9/planning1/index3 remained pinned through Resume, distinct from new dev guidance.
- **Acceptance boundaries:** max was verified on parent and children. Parent narrative/skeleton folds and two child skeleton folds continued to reports. Measured fold retry repair `46ccc5a9` passed 51 tests/review and bounded live exercise; explicit retry-reason telemetry remains pending. Live 100 MiB upload and 512 MiB retained artifact download after worker deletion passed ([report](docs/agent-reports/large-artifacts-2026-10-17.md)). Root Resume recovered an append without duplication; volatile kernel recovery needed explicit recomputation. Observed shared spend $66.2841164; one unknown paid call retains a separate $4/token-upper-bound reservation, within the $200 ceiling.
- **Research depth:** MAIN10/planning2 (`f96d8d15`) and skills framing4 (`36147403`) add extended-research and required-reference guidance for new work. The old tree kept its pins, subsequently read the references and corrected its initial shallow baseline. Toy invariants do not establish novel attacks.
- **Adaptive allowances:** [spec](../docs/specs/adaptive-research-resources.md) records renewable time/token/turn/storage grants with parent escalation, hard outer ceilings and durable safe-point recovery. Planned, not implemented.
- **UI:** signed local sidebar/chat/research workspace, 100 MiB uploads, half-window drag pane, document filters/tabs. Synthetic preview remains for owner review. Context inspector prototype is reviewed; integration pending. Earlier foundation [acceptance](docs/agent-reports/foundation-acceptance-2026-10-17.md) and [STATUS](STATUS.md) retain historical scope.

## How work is done

- **Review by risk:** independent read-only review for security, storage, concurrency and new features; diff/latest-check review for tested code moves, docs and test-only fixes. Current instructions govern delegation.
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

1. Keep signed local work unpushed/unmerged for review. Research artifacts and exact evidence are preserved; disposable-lab teardown is verified. No more calls or experiments are pending for this episode.
2. Implement adaptive allowances, delegated narrative enrichment with shared accounting, explicit fold-retry metadata and context-inspector integration. Actual child folds are skeleton-only; cache hits are measured, but no layout comparison or broad scientific continuity is proved.
3. Future acceptance still needs hour-long meaningful Sage computations, broader scientific continuity and the deferred model/context arms. Any resumed SNOVA investigation starts from the retained plan, sources, code and results; the export does not automatically restore the runtime.
