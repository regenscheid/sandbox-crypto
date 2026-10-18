# Handoff

Updated 2026-10-18. Development is local branch `dev` in `/home/andy/.cache/pt/dev-local`. Read [STATUS.md](STATUS.md), [BACKLOG.md](BACKLOG.md) and [CONVENTIONS.md](CONVENTIONS.md). Primary `/home/andy/Git_Projects/sandbox-crypto` is a different checkout; preserve its unrelated state.

## Current work and evidence

- Current continuation: child narrative/shared-grant code at `d6d37c70` passed 14,380 Python tests (50 skips). Reviewed cancellation accounting and automatic-recovery cleanup fixes are integrated through `84bb0a4e`; 164 final focused lifecycle checks and the final source-frozen broad suite pass: 14,390 tests, 50 prerequisite/opt-in skips; Ruff, Pyright, tier, hygiene, license and generated-protocol checks pass. The owned recovery lab first used `d20e5fe3`, then qualified the narrow host cleanup fix separately. It passed three-level archive retrieval, literal Python restoration and automatic recovery after a synthetic streamed EOF. Its genuine hour-long Sage job is still running; do not claim completion or stop it early.

- Relevant UI, writing-skill, agent messaging, history and autonomous-recovery work has been consolidated on `dev`. User wants one development branch and signed local commits, not repeated permission requests for routine implementation. No implicit push/deployment or paid-research continuation.
- `dbc40a4e` adds delegated-parent descendant-history retrieval; `198dd6cc` adds saved-attempt context inspection. Current follow-up adds same-work continuation navigation/search, bounded cancellable automatic scanning and an inspector entry on individual run pages. Each result identifies its attempt; children/siblings are not silently merged.
- `2f972a2c` adds four true process-death recovery cases. All four pass. Local browser/HTTP/DOM coverage and precise limits are in [context and continuity acceptance](docs/agent-reports/context-continuations-2026-10-18.md). The final source-frozen suite passed 14,355 Python tests (63 prerequisite/opt-in skips); all 2,218 web tests passed. The earlier mixed-source run was not green and is documented separately.
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
- Audited cumulative token grants are implemented. Broader adaptive time/turn/storage renewal is not. The owner's requested policy starts at 20,000,000 tokens and asks for each next 10,000,000-token increment; inspect actual configuration/grant before applying it.
- Parents wait for child responsibility; root-level Resume drives the tree. Automatic recovery and narrow literal Python reconstruction need more live qualification. No arbitrary kernel-memory restoration is claimed.
- Context inspector currently shows saved attempts. Live usage, exact working-context capture, time/fold placement and reviewer/summarizer accounting remain. Child narrative/overview folds now share the child's cumulative grant; original archive remains readable.
- Built-in writing skill prefers annotated outlines, verified citations and self-standing current findings. Tool `show_outline` is implemented; docs and immutable Markdown artifacts remain distinct.

## Secrets and source boundaries

The owner's real keys are under `~/.config/sandbox-science/secrets/`; never open, read, list, copy or print them. Programs receive existing paths only for explicitly authorized runtime work. Tests use synthetic credentials. No sudo/security-setting changes, Docker pruning or destructive operations on real data.

Product prompts are our own wording. Never import the two OpenScience skills derived from proprietary prompt collections; check licenses/notices before adapting sources. Features plug into existing hooks/routes; do not grow the central harness/app into unrelated responsibilities.
