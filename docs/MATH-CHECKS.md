# Durable text math checks

Document revisions now connect durable save scheduling, the isolated renderer,
mechanical findings and closing-round delivery (D82, document portion of LD-53
to LD-55 and CR-36 to CR-38). [Comments and replies](COMMENT-MATH.md) add immutable body revisions under the
same checker and delivery bounds. Ledger entries and finding prose remain outside
these slices. The renderer remains unavailable until configured by the
operator; an unavailable check never establishes rejection or a fix.

`orchestrator.mathcheck.MathChecker` runs one formula in a fresh Node process.
It imports the same pinned KaTeX 0.18.9, options, bounded front-matter macros
and deterministic repair rules as the workspace GUI. Formula text is JSON
data; it never selects JavaScript, a shell command, a path or a network endpoint.
The checker makes no model calls and returns no rendered HTML.

The result is `accepted`, `rejected`, or `unavailable`. Only a KaTeX ParseError
proves rejection. Size, macro-expansion and definition skips, missing runtimes,
timeouts and invalid responses are inconclusive. A rejected formula can carry
the first deterministic repair that the same renderer accepts. The source is
unchanged. Model repairs and the shared per-project repair cache remain separate.

## Build and configure

From `web/workspace`, install the locked dependencies with
`npm ci --ignore-scripts`, then run `npm run build:math`. The `dist-math/`
directory contains the standalone `mathcheck.mjs` bundle, `check-math.mjs`
launcher and the KaTeX/project license notices. Distribute the directory
together. It uses the GUI's existing dependency lockfile; no second KaTeX
installation is introduced.

The service operator supplies absolute paths to Node, the launcher and bundle
when constructing `MathChecker`. Node 22.12 or newer is required; acceptance
uses 24.20.0, the existing web CI runtime. Node is not bundled into Python
wheels or the orchestrator image by this slice. No save hook or fallback
silently enables the checker when those paths are absent.

The persistent harness accepts `--math-node`, `--math-launcher` and
`--math-bundle` together. They select trusted operator code, never document
content or a workload path. Without them, saved revisions and inconclusive
observations remain durable. No provider, service or runtime is installed.

Pass a `Formula` with a stable caller-owned ID, TeX, display mode and effective
macros to `check`, or at most 32 distinct formulas to `check_many`. The caller
must retain the containing revision and location; this component does not
grant evidence access or decide author/session attribution. Results record
the formula-input, bundle and launcher SHA-256 hashes, KaTeX version and Node
runtime version. Each invocation copies bounded trusted code into a private
temporary directory so an operator rollout cannot change the bytes mid-check.

## Bounds and failures

- At most two processes and 32 admitted calls per checker; a batch has at most
  32 unique span IDs. A default two-second deadline includes waiting for a
  process slot; operators can choose 0.05 to 10 seconds.
- Requests are at most 256 KiB, stdout at most 32 KiB and stderr at most 4 KiB.
  No raw stderr is returned to callers. Trusted configuration reads require
  ordinary files and cap the bundle at 8 MiB and launcher at 64 KiB.
- The GUI's 5,000 UTF-16-unit formula limit, 200 macros, 1,000 characters per
  macro, 2,000 expanded macro characters and KaTeX expansion/size limits apply.
  KaTeX is always untrusted, with fresh macro state for each attempt.
- Each Node process has a 96 MiB V8 old-space cap and 4 MiB semi-space cap.
  These are heap controls, **not an operating-system RSS limit**. Deployment
  memory controls remain the operator's responsibility.
- Timeout, cancellation, oversized output and failures kill/reap the process
  group before temporary code is removed. The child inherits no credentials
  or Node startup options from the service environment.
  Unread pipe bytes are drained and discarded after killing the group, so a paused pipe cannot block process reaping on Python 3.12.
  Repeated cancellation keeps the admission slot and private code until the
  process ownership handoff and cleanup finish.
  Cleanup covers descendants remaining in that process group. The fixed
  KaTeX bundle creates no child processes; operator-configured code is trusted.
- IDs, protocol version, engine version, settings, result shape and runtime
  floor are checked. Mismatches produce `unavailable`; a skipped or failed
  check must never close a finding as fixed.

Focused tests exercise actual Node/KaTeX, renderer-tested repairs, effective
macros, process cancellation, output flooding, queue deadlines, invalid UTF-8,
FIFO/missing configuration, deeply nested malformed responses and protocol mismatches. The web CI job builds the
bundle and runs the Python boundary tests with Node available.

## Durable scheduling and ownership

The shared `control.sqlite3` transaction that saves an immutable revision also
advances its document's math target. Failure rolls back both. The target and
completed cursors reference retained revisions rather than copying an unbounded
queue on every save. Writer preparation and admission reconcile targets
monotonically with authoritative document heads, including revisions written by
an older writer. Read-only workspace requests never migrate or advance state.

Every parsed span is checked on each saved revision under its effective macros.
An unchanged formula is therefore checked again after a front-matter macro change.
Check invalidation and authorship are separate: the last revision that added or
changed the containing stable block proves its owner. Moves and unrelated edits
preserve that owner, even across later USER changes. Historical jobs reconstruct
that proof from immutable maps, in bounded steps; legacy provenance that cannot
be proved is explicitly unknown. The latest document author is never substituted.

Only proved main/delegated agent or attributed worker blocks with an established
session receive author findings. USER, reviewer, import, platform, unknown and
no-session blocks still have visible observations. A USER takeover keeps earlier
unresolved history but excludes it from new author notices and reflags. Only an
accepted current formula, complete parsed removal, or explicit USER decision can
resolve it.

## Mechanical findings and delivery

Findings have additive `review` or `math` origin. Existing reviewer records,
immutable history, IDs and USER compare-and-swap revisions survive the schema
migration. Mechanical findings are `warn`/`math`, name their exact check and
document/block/span/input identities, and have no fabricated reviewer run or model.
Model reviewer tools cannot create, confirm, supersede or otherwise decide this
origin. Existing authenticated USER decisions retain their exact-head guard.

An accepted current span confirms the finding mechanically. A span absent from a
complete current parse is confirmed with an explicit removal reason; it is never
superseded. Pending, stale, skipped or unavailable observations cannot resolve or
reflag. A late result is retained as evidence but cannot change newer canonical
state. Same-head retries consult the latest exact attempt, so an older rejection
cannot cause a new handoff while current evidence is pending or unavailable.

A newly proved edit to the failing block can reflag the same finding. Macro and
renderer dependency changes refresh the exact assessment and audit history without
consuming either failed-patch opportunity when the block owner revision is unchanged.
Dependency refresh preserves unaddressed and explicit USER resolutions; a previously
mechanically accepted formula can reopen on a new rejection. Available accepted
checks also update the assessed block owner. Previous ownership is reconciled from
the last immutable available observation, including pre-fix accepted rows; missing
or invalid proof never consumes a reflag. Duplicate checks, unchanged spans,
unrelated edits and restarted attempts do not consume the two-reflag budget.
Two failing block edits leave it unaddressed.
Current prose, error, evidence and eligible session attribution follow the new
check, while append-only history keeps the prior assessment and owner.

These notices are collected only at closing, together with terminal consistency
findings. They share at most two author response rounds; warnings never trigger
generic threshold interruptions. A notice counts as delivered only after a
successful author model call, and is acknowledged only after the run transcript
is durably saved. Failed/zero/final-turn and late-result cases stay pending for a
later live turn. After the bounded response opportunity, the decision to leave a
still-rejected finding unaddressed is staged under the exact notice claim. It is
finalized only after durable transcript save, in the acknowledgement transaction,
with current source, exclusion, USER head and latest rejected evidence rechecked.
A failed save leaves the open notice retryable; an expired claim can rebind that
staging record, and a stale save callback cannot decide another run's claim.
This never invents an additional renderer rejection. Saved summary counts describe
the closing boundary before this save finalization.

## Queue and history budgets

At most two globally active attempts are admitted. Each step renders at most
32 spans or reconstructs at most two historical maps; a live controller yields
after eight steps and holds closing for at most ten seconds. Actual thread work
and subprocess cleanup remain owned through repeated cancellation before leases
or store lifetime are released. Cross-process leases fence reads, commits and
recovery. An interrupted attempt resumes its cursor with the same check ID.

Default logical history budgets are 10,000 attempts, 100,000 observations and
256 MiB of observation payloads, plus 100,000 historical ownership proofs and
64 MiB of their author payloads. Strict positive bounded settings can lower these
budgets. Reaching one holds the durable backlog visibly; it does not evict
unresolved evidence, lose revisions or veto ordinary document saves. Automatic
retention/compaction is not implemented. Individual observations are at most
32 KiB; APIs page twenty records and prefix-bound persisted metadata before
parsing. Existing document/text/map limits also apply.

Unavailable and configuration-changed checks get one explicit current-head retry
per controller activation, recorded as a new immutable attempt. They do not spin
on an unavailable renderer. Each attempt pins bundle, launcher, engine, operator
Node identity and actual runtime; changes or mixed replies become unavailable.
No idle scheduler is created. CLI exit stops live work safely; durable queued,
held and same-head retry work remains visible for a later activation.

The workspace's authenticated session routes list **project-wide** checks, so
USER/no-session evidence is visible. A valid project/session pair is required;
cross-project access and foreign finding decisions remain refused. The panel
shows unavailable counts, held/pending revisions, escaped mechanical evidence and
ownership proof. Saved run summaries disclose pending work; live push updates are
still absent.

## Disposable browser fixture

After building the math bundle, run `.agents/labs/document_math.py create` with
explicit absolute `--node`, `--launcher` and `--bundle` paths. It creates marked
temporary synthetic state and exercises actual local KaTeX, with no model or
worker calls. It includes agent and USER failures, preserved block ownership,
accepted/removal resolution, unavailable evidence and two failing reflags.
`fix`, `unavailable` and `reopen` subcommands require that marker before opening
a writer; `reopen` supports concurrent USER CAS/draft checks. Native browser
acceptance is recorded separately from unit/static checks.

## Validation boundary

Independent review covers populated legacy migrations and rollback, historical
ownership, stale/unavailable/configuration retries, bounded corrupt-state reads,
actual-work cancellation, closing-round delivery and failed-save/ACK races.
[Native browser acceptance](../.agents/docs/agent-reports/document-math-2026-10-12.md)
uses actual local KaTeX 0.18.9 and Node 24.20.0 with synthetic documents, no
provider or worker calls, and retained USER drafts through concurrent CAS changes.
All disposable browser state was removed. Rebuilt production GUI bytes match
the archived acceptance assets.

Artifact integration required three `None` defaults in a manually constructed
CLI test namespace. Its cleanup assertions and timeouts are unchanged. The
combined affected publication/math/migration/workspace API suite then passed
68 tests; this fixture correction changes no product, GUI or fixture-builder
behavior.

Final combined validation passed 9,459 Python tests (43 opt-in skips) on Python
3.13.15, 434 web tests, locked dependency installation, formatting, lint, strict
Python/Svelte/TypeScript checks, Python/npm license checks, protocol generation
and both GUI/math builds. The actual Node boundary runs against the built math
bundle. The supported Python 3.12 matrix remains unchanged; fresh PR CI and merge
are separate delivery gates. Comment/reply checks have their own additive [slice and validation boundary](COMMENT-MATH.md). Ledger and finding-prose checks,
idle scheduling and automatic history compaction remain open.

The additive comment/reply slice also corrected shared dependency-only reflag
accounting: macro/renderer changes refresh evidence but only a new proved failing
block edit spends an author opportunity. Final signed `77ecc171` passed independent
review, 78 focused document/comment cases and all combined gates (9,631 Python
tests, 43 opt-in skips, two existing warnings; 441 web and 24 post-build math checks).
Fresh [comment/reply browser acceptance](../.agents/docs/agent-reports/comment-math-2026-10-12.md)
passed against the frozen source/helper; all disposable state was removed. This
extends durable source coverage to comments/replies, with ledger/finding prose,
model repair proposals and idle dispatch still open. Fresh PR CI is required
before merge.
