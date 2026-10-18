# Context continuity and local recovery acceptance

Date: 2026-10-18. Target: local `dev` at `/home/andy/.cache/pt/dev-local`,
starting at `198dd6cc`. This report covers local implementation and synthetic
acceptance. It does not establish deployment or live OpenShell recovery.

## Scope and branch check

Inspected the local branch/worktree inventory and remote heads before starting.
Remote `main`, `codex/agent-messaging` and `spike/pydantic-ai` contained no newer
matching inspector implementation or crash tests. Existing descendant-history,
Skills, MCP, document and autonomous-recovery work was already on `dev`; it was
extended rather than rebuilt. The primary checkout and real services were left
alone. No paid model calls or research continuations were made.

The context inspector can now list an agent's immutable work attempts, select an
earlier saved attempt and search original history across its continuations. Each
match keeps its original attempt identity. Parent, sibling and descendant work
are separate; a legacy attempt with no work identity is shown alone. Attempts
without a final run record are labelled and have no broken run link.

Automatic search traverses empty pages as well as matching pages, with a limit of
100 requests or 200 new matches per click. Continuation resumes from the saved
cursor; only the latest 200 snippets remain in browser memory. Stop, tab changes
and dialog destruction abort the active request and discard stale responses.
The backend bounds the session index slice before filtering agent/role/kind, so
a sparse agent does not cause an unbounded walk over other agents' messages.
Existing byte limits and omitted-field disclosures remain in force.

Individual run pages now expose the saved-context dialog, so a continuation link
can actually be followed back into context inspection. Loading is on demand.
The initiating button stays focusable during its asynchronous load; Escape
restores focus to it. Timeline and folds still describe one saved attempt.
Unavailable live/current-context evidence is not reconstructed from previews.

## Independent review

`context_review` examined storage scope, query bounds, continuation provenance,
cancellation and frontend state. Its findings led to bounding the session-position
walk, fixing its missing SQL bound parameter, handling unsaved attempts explicitly,
and preserving query/cursor state. The final review reported no remaining high or
medium findings. It did not run the Python suite.

`merge_review` independently checked the subprocess tests and disposable fixture.
It found no P1/P2 issues: the death points wrap real commits, require the expected
exit code and reopen independent stores; fixture writes and credentials are
synthetic and cleanup is confined to the owned temporary directory/listener.

## Process-death cases

Signed commit `2f972a2c` adds four tests in
`packages/orchestrator/tests/test_child_settlement_crashes.py`. Each subprocess
performs the real operation and then exits with `os._exit(86)`, bypassing Python
cleanup. Import resolution is pinned to the tested checkout. The parent closes
its old fixtures and reopens independent project/document connections.

| Death point | Verified after reopening |
| --- | --- |
| Final run record committed | Saved record survives; unconfirmed work finalization stays a visible persistence failure |
| Mailbox ACK committed | Delivered/acknowledged state remains truthful, without inventing unconfirmed work completion |
| Work finish committed | Typed child handback remains available for parent settlement |
| Ledger change committed before settlement receipt | Reconciliation applies one ledger effect, records its receipt, then makes the parent ready; repeated drain is idempotent |

All four focused cases passed in 4.16 seconds. No new production recovery defect
was found in these windows. This is application process death against local
SQLite, not simulated host power loss, kernel snapshots, or an OpenShell restart.
The temporary agent worktree was integrated into `dev` and submitted for archival.

## Browser acceptance

Built the current production workspace bundle and served it through the real
authenticated workspace application using `.agents/labs/context_browser.py`.
The fixture creates its own `/var/tmp/ss-context-browser-*` directory, synthetic
login, project and archive, and binds an ephemeral loopback port. No model,
worker, or external MCP client is supplied.

Observed in the browser:

- Main and delegated run pages each open their own context-management dialog.
- Four synthetic provider input observations render with a correctly weighted
  50% cache-read share; cache writes remain unavailable.
- Searching `CONTINUITY_NEEDLE` across root continuations returns exactly two
  messages from their respective original attempts. The child is excluded.
- The continuation list shows the two root epochs and predecessor. Following
  the first attempt opens its run page and permits reopening the inspector.
- The delegated inspector finds only its `CHILD_ONLY` message. Its current-view
  tab explicitly says no exact working-context snapshot was captured.
- Escape closes the asynchronously loaded dialog and returns keyboard focus to
  the initiating Context management button.
- The Skills catalog loads the synthetic pack; changing its loading rule and
  saving advances the settings from revision 0 to 1 and clears pending changes.
- MCP services show the configured `papers` service and a dated historical
  observation of three listed tools, with no current-health claim.
- A 1,100,009-byte original message uses the chunk viewer. Reading its next page
  reaches the exact byte total and final `LARGE_END` marker, without retaining
  more than 1 MiB of displayed message text per click.
- Logout removes the authenticated workspace/inspector and displays the login
  instructions.

Screenshots retained outside Git:

- `/home/andy/.cache/pt/context-continuations-20261018.png`
  SHA-256 `3659763a037ca7dc3552c723d703d03f3f275f6b27de5e11b673e5a7f3267e8e`
- `/home/andy/.cache/pt/skills-browser-20261018.png`
  SHA-256 `1550315680a0a241ad173ab0948f516f84dbb645b03843888464328ff906cd2c`

The final fixture used port 39903 and directory
`/var/tmp/ss-context-browser-m81p7o3o`. Its browser tab was closed, SIGTERM completed
with exit 0, the directory was confirmed absent and the listener was confirmed
closed. The personal instance and preserved research sandbox were not touched.

## Verification

- Final source-frozen full Python suite: **14,355 passed, 63 skipped, two warnings**
  in 676.45 seconds. Skips require live/integration/image configuration or the
  math build; the nine math prerequisites were subsequently built and tested
  separately. Both warnings come from the deliberate malformed artifact-size
  fixture and are expected serializer diagnostics.
- Focused history/API/generated-contract tests: 21 passed, including all five
  generated-type tests described below.
- Four new subprocess cases: 4 passed.
- Full web suite: 2,218 passed across 118 test files. This includes 11 inspector
  DOM tests covering scope, sparse pages, cancel/stale results, the 200-match and
  100-page bounds, unmount cancellation and focus restoration.
- Full Python type check: 0 errors. Svelte check: 0 errors/warnings.
- Production web build passed, with the existing large-bundle advisory.
- Math renderer build and post-build Python checks passed: 38 tests, including
  the nine real-renderer cases skipped before the bundle existed.
- Ruff, generated API/protocol checks, repository hygiene and license checks
  passed. All 14,418 collected Python tests belong to exactly one tier
  (13,459 fast, 959 slow). Final whitespace and both formatter checks pass.

The first broad Python pass was not green: 14,343 passed, 63 prerequisite/opt-in skips and
five generated-type import failures. Python/API source was edited while that
suite was running, so its subprocess imported a newer generator against modules
already collected from older source. The five failures passed in the focused
rerun. The final broad run holds source unchanged and disables live-gateway and
integration test flags. A unique `/var/tmp` pytest basetemp avoids the unrelated
Git repositories enclosing `/tmp` and `/home/andy` in this environment.

Final suite evidence (outside Git):

- `/home/andy/.cache/pt/dev-python-final-20261018.log`
  SHA-256 `339b384bea8f6538149d02b3cb8735aba1a38e6c14673b3bc17d57a717683931`
- `/home/andy/.cache/pt/dev-web-final-20261018.log`
  SHA-256 `f46a5fe1a0c4593213cad6324542c045ac220909ba1e03a0070642486c05e697`

## Remaining work

Live attempts, timestamp-aligned usage/fold events, reviewer/summarizer usage and
captured working-view composition still need integration. Search indexing and
broader real research acceptance remain separate from the bounded scans here.
The MCP browser still lacks tool schemas, detailed policy and current health.
Autonomous recovery, guarded kernel reconstruction and descendant-history tools
still need a fresh scoped OpenShell acceptance run, including sustained Sage
work and process loss. This turn does not restart the stopped SNOVA monitor,
change spending/grant policies or claim scientific research quality.
