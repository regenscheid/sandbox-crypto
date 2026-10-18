# Status

Updated 2026-10-18. Current development is local `dev` in `/home/andy/.cache/pt/dev-local`. Relevant research/UI/recovery branches have been consolidated there. Do not use the historical SNOVA trial paragraphs in older revisions of this file as live service status.

## Phase and focus

- Saved main/delegated attempts have an embedded context inspector with provider input/cache accounting, paged fold summaries and original-history search. Same-agent continuation attempts can be listed and searched using immutable work identity; children/siblings remain separate. Both saved transcript sections and individual run pages expose the dialog. Exact live context snapshots, time-aligned fold events and auxiliary usage are still open.
- Original-history tools reuse `history_read`/`history_search`, including bounded UTF-8 pages for oversized messages. Delegated parents can inspect descendant archives; independent checks remain own-run only. Full native provider payloads are preserved separately and are not presented as visible conversation text.
- Automatic inspector search is cancellable and pauses at 100 bounded pages or 200 new matches per click. Cursor continuation avoids restarting a scan, the browser retains at most 200 snippets, and sparse searches bound their SQLite index walk before filtering. Unsaved attempts are identified without broken links.
- Skills catalog/loading choices and the first MCP browser are implemented. Authenticated browser exercise uses a disposable synthetic project, not the personal or SNOVA store. Skills revisioned save, dated MCP observations, continuation navigation and large-message chunk viewing have been exercised; see the [acceptance report](docs/agent-reports/context-continuations-2026-10-18.md).
- Four actual process-death tests now cover final-record commit, mailbox acknowledgement, work finish, and ledger commit before child settlement receipt. Reopened stores preserve truthful confirmation/ACK state, typed handbacks, single ledger effects and parent readiness. Signed `2f972a2c`; all four focused tests pass. These are local subprocess tests, not OpenShell/host reboot qualification.

## Foundations already implemented

- Normal orchestration uses its own OpenShell VM; research code executes in separate worker VMs. Root Resume recovers the selected work tree, with durable parent waiting, child settlement, immutable inputs and recorded model/effort pins.
- Responses streaming and bounded transient retries are implemented. Unknown model charges remain visible but do not alone block local admission or explicit Resume; upstream LiteLLM is the primary spending control. Unknown worker/MCP effects retain their identities and require reconciliation before replay.
- Autonomous recovery and opt-in literal Python kernel reconstruction are merged locally and have scoped OpenShell qualification. Missing arbitrary volatile state still needs a decision. Automatic replacement after arbitrary VM/worker loss and host-loss recovery remain unqualified; do not claim arbitrary interpreter snapshots or replay of unknown effects.
- Audited cumulative delegated token grants exist. Broader adaptive time/turn/storage allowances, parent resource warnings and policy-based renewal remain separate work; do not confuse those with the implemented token-grant route.
- Active replay is bounded, while exact archived evidence survives folding. Children use the existing narrative/overview engine within their cumulative token grant; skeleton fallback preserves reporting room. Fold repairs retain bounded attempt diagnostics. Final saved summaries follow background-call settlement, and confined calls use their canonical journal without duplicate cancellation estimates.
- The workspace has chat/uploads, a drag-resizable half-window research pane, filters/document tabs, Markdown/LaTeX, revisioned living documents with comments, and built-in scientific-writing guidance with annotated outlines and `show_outline`.

## Verification and deployment boundaries

- Current continuation: reviewed child narrative/shared-grant, cancellation accounting and saved-failure recovery fixes are integrated through `84bb0a4e`. The final source-frozen suite passes 14,390 Python tests with 50 prerequisite/opt-in skips; 164 focused lifecycle checks, Ruff, Pyright, tier, hygiene, license and generated-protocol checks pass. [Implementation and review](docs/agent-reports/delegated-narratives-2026-10-18.md).
- The [owned OpenShell lab](docs/agent-reports/openshell-recovery-acceptance-2026-10-18.md) verified automatic recovery after streamed EOF, explicit tree Resume after agent-VM loss, literal Python restoration, descendant history reads, confined child narration and exact 100 MiB upload/delivery. Its Sage job ran for 3,600 seconds with 249,063,436 verified batches and retained its original execution through worker restart. All 40 sanitized evidence receipts match their manifest; owned VMs, six units, listeners, observed processes, one driver directory and private lab state were removed. Synthetic model replies establish runtime behavior, not scientific research quality. Personal and preserved SNOVA services are untouched.

- Prior `d20e5fe3` baseline: 14,355 Python tests passed with 63 prerequisite/opt-in skips. The later 14,390-pass suite above supersedes that Python count; all 14,440 currently collected tests belong to exactly one tier. Both suites have two expected serializer warnings from a deliberately malformed artifact fixture. The initial mixed-source pass is documented separately.
- Unchanged web/API evidence is reused from that baseline: 2,218 web tests, strict Svelte checks, formatting, generated API checks, licenses, both web/math builds and 38 post-build math tests pass. This continuation changed no web source or public API shapes and did not rerun the browser suite.
- No current changes were pushed or deployed into the personal or preserved research services. No paid model call or research continuation was started. The owner stopped the periodic SNOVA monitor; do not recreate it implicitly.
- Historical runtime evidence: [foundations](docs/agent-reports/foundation-acceptance-2026-10-17.md), [large artifacts](docs/agent-reports/large-artifacts-2026-10-17.md), and [bounded early SNOVA trial](docs/agent-reports/snova-research-acceptance-2026-10-17.md). Later SNOVA results remain in the acceptance archive. Verify actual service/store/guard state before any future resumption; older teardown reports describe only their own disposable labs.

## Next work

1. Finish context inspection: live attempts, timestamped usage/fold events, reviewer/fold usage and captured working-view composition; retain precise provenance and missing-data labels.
2. Extend recovery acceptance beyond the completed synthetic OpenShell cases: automatic infrastructure replacement, MCP failures and real-provider/scientific continuity. Preserve personal services and original research archives.
3. Implement remaining adaptive resource handling. Delegated narrative enrichment and bounded fold-repair diagnostics are implemented; extended real-model scientific acceptance remains separate.
4. Expand MCP tool schemas/service policy/current observations, then Overview/Memory/session usability. Detailed [backlog](BACKLOG.md) entries distinguish design, code and runtime acceptance.

Earlier lab findings (MCP query matching, PDF extraction quality, scientific reviewer checks, package/image compatibility and long-run job capacity) are historical leads, not a verified current defect inventory. Reproduce against current `dev` before changing behavior. Lockstep component upgrades and the worker boundary still apply.
