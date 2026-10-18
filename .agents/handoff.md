# Handoff

Updated 2026-10-18. Development is local branch `dev` in `/home/andy/.cache/pt/dev-local`. Read [STATUS.md](STATUS.md), [BACKLOG.md](BACKLOG.md) and [CONVENTIONS.md](CONVENTIONS.md). Primary `/home/andy/Git_Projects/sandbox-crypto` is a different checkout; preserve its unrelated state.

## Current work and evidence

- Latest batch: bounded live context timeline (`3ad476f4`, `fa8218fd`), durable resource requests/parent decisions and exact USER token increments (`6583743b`, `1fd46dd6`, `9d4b2c73`) are integrated on local `dev`; workspace controls and final fixes are signed through `a8ef3c5d`. The [integration report](docs/agent-reports/context-resources-2026-10-18.md) records 14,413 Python passes, 50 skips, 75 focused passes and 50 notification-order repeats. Independent GPT-6.1-Sol review is clear.
- Owned `context-live-timeline` worktree archive was requested but remains queued/present; source is integrated. The other two task worktrees are archived.
- Owner now authorizes **$150 aggregate new test spend** and requires all six completion items; this supersedes the old $75 proposal. No paid calls started yet. Root owns recovery/live labs/tracking; agents own resource policy, captured context/history index, and workspace browsers in separate managed worktrees. See [campaign](docs/agent-reports/completion-campaign-2026-10-18.md). Do not reuse SNOVA budget or recreate its monitor.

- Prior child narrative/shared-grant, cancellation accounting and automatic-recovery cleanup fixes through `84bb0a4e` remain integrated. Its 14,390-test pass is superseded by the current full suite above; scoped OpenShell evidence below is unchanged.
- The [owned OpenShell lab](docs/agent-reports/openshell-recovery-acceptance-2026-10-18.md) used baseline `d20e5fe3`, qualified the narrow host cleanup fix, then tested final host/image `84bb0a4e`. It passed three-level archive retrieval, literal Python restoration, automatic streamed-EOF recovery, child narrative accounting and exact 100 MiB upload/delivery. The genuine Sage job completed 3,600 seconds and 249,063,436 verified batches under its original execution after worker restart. All 40 sanitized evidence receipts match their manifest; owned VMs, six units, listeners, observed processes, one driver directory and private lab state were removed. No real model quality or arbitrary kernel restoration is claimed.

- Relevant UI, writing-skill, agent messaging, history and autonomous-recovery work has been consolidated on `dev`. User wants one development branch and signed local commits, not repeated permission requests for routine implementation. No implicit push/deployment or paid-research continuation.
- `dbc40a4e` adds delegated-parent descendant-history retrieval; `198dd6cc` adds saved-attempt context inspection. Current follow-up adds same-work continuation navigation/search, bounded cancellable automatic scanning and an inspector entry on individual run pages. Each result identifies its attempt; children/siblings are not silently merged.
- `2f972a2c` adds four true process-death recovery cases. All four pass. Local browser/HTTP/DOM coverage and precise limits are in [context and continuity acceptance](docs/agent-reports/context-continuations-2026-10-18.md). The preceding `d20e5fe3` baseline passed 14,355 Python tests (63 prerequisite/opt-in skips); its web evidence is superseded by the current 2,228-test pass and 32 affected DOM checks. The earlier mixed-source run was not green and is documented separately.
- Disposable UI testing uses `.agents/labs/context_browser.py`. It creates only its own `/var/tmp/ss-context-browser-*` directory and loopback listener, with visibly synthetic credentials/data and no worker/model calls. SIGTERM/SIGINT cleanup removes that fixture. Do not confuse it with the user's UI at 29183 or design instance at 5175.
- The preserved SNOVA run is not being resumed. Its monitor was stopped by the owner. Older lab teardown/episode details are historical evidence, not current acceptance-service state. Any future paid research requires the current authorization/budget and actual service/archive inspection.

## Workflow

- Keep docs/specs and development trackers current with code. Before implementation, inspect local and remote branches for overlapping work. The last remote-head check found `main`, `codex/agent-messaging` and `spike/pydantic-ai`; no newer matching inspector/recovery test implementation.
- Use signed commits through the owner's existing git configuration; no attribution lines. The owner asked to consolidate local work on `dev`. Separate temporary agent checkouts may be used for isolated tasks and archived after integration.
- Independent read-only review for security, storage, concurrency and new features; light review for test-only changes and fixes to review findings. Earlier owner authorization for GPT-6.1-Sol reviews remains applicable. Current tool/developer instructions govern agent spawning.
- Run one Python suite at a time, at most `-n 4`, with `SS_LIVE_GATEWAY`/`SS_INTEGRATION` unset. Use a unique `/var/tmp/sandbox-science-test-cache-*` basetemp: `/tmp` and `/home/andy` are Git checkouts, so security fixtures below them can fail correctly. Do not edit Python/generated API source while a broad suite runs; subprocess/import tests can otherwise mix old and new modules.
- Logs/screenshots go under `~/.cache/pt/`. Regenerate workspace API types on contract changes. Python uses the locked `.venv`; `pyright --pythonpath .venv/bin/python` resolves the workspace environment explicitly.
- Never launch a real service from an actively edited checkout. New runtime qualification must preserve worker isolation, personal services and archived data, then fully tear down only the owned lab.

## Design boundaries

- Unknown model prices are visible estimates, never silently zero, but do not by themselves block admission/explicit Resume. LiteLLM enforces the primary cost ceiling. Unknown worker/MCP effects still reconcile by original identity before replay.
- Durable token/turn check-ins and direct-parent decisions are implemented. Parents may renew turns within the configured ceiling; only USER can approve each next 10,000,000 tokens, from new defaults of 20,000,000. The new workspace route requires an inactive tree and exact revision, and never auto-retries an uncertain write. Nested ancestor approval is separate. Time/storage policy and root self-renewal remain open.
- Parents wait for child responsibility; root-level Resume drives the tree. Saved model-phase automatic recovery and narrow literal Python reconstruction passed scoped OpenShell probes. Automatic VM/worker replacement and arbitrary kernel-memory restoration remain unqualified.
- Context inspector now opens unsaved live attempts, starts with the latest 50 answer/event rows and follows bounded timestamped observations while visible. Fold/reviewer usage is a separate latest-intent snapshot. Unknown cache counters are unavailable, never zero. Exact working-context composition remains uncaptured. Original archives and same-work continuation search stay available.
- Built-in writing skill prefers annotated outlines, verified citations and self-standing current findings. Tool `show_outline` is implemented; docs and immutable Markdown artifacts remain distinct.

## Secrets and source boundaries

The owner's real keys are under `~/.config/sandbox-science/secrets/`; never open, read, list, copy or print them. Programs receive existing paths only for explicitly authorized runtime work. Tests use synthetic credentials. No sudo/security-setting changes, Docker pruning or destructive operations on real data.

Product prompts are our own wording. Never import the two OpenScience skills derived from proprietary prompt collections; check licenses/notices before adapting sources. Features plug into existing hooks/routes; do not grow the central harness/app into unrelated responsibilities.
