# Durable text math checks

Saved document revisions are checked for math that KaTeX rejects, through durable
save scheduling, an isolated renderer, mechanical findings and closing-round delivery
(D82, document portion of LD-53 to LD-55 and CR-36 to CR-38).
[Comments and replies](COMMENT-MATH.md), [MAIN explanation fields](FINDING-PROSE-MATH.md)
and [ledger statements](LEDGER.md) use the same checker and bounds. Other finding
prose and ledger fields other than statements are not checked. The renderer is unavailable until the operator configures it; an
unavailable check never establishes rejection or a fix.

`orchestrator.mathcheck.MathChecker` runs a batch of at most 32 formulas from one
source in one fresh Node process (`check_many`), and more than one when the batch's
formulas exceed one request's 256 KiB; each formula gets its own macro definitions,
and no process serves two batches.
It imports the same pinned KaTeX 0.18.9, options, bounded effective project/document macros
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
launcher, the established `LICENSE-KaTeX.txt` and `LICENSE-Sandbox-Science.txt`,
complete `THIRD-PARTY-NOTICES.txt` and `license-manifest.json`. Distribute all six
files together. The shared checked collector retains KaTeX's root MIT, exact
React-derived Apache helper attribution and glyph-derived SVG attribution with
the full primary Apache/OFL texts. No external font/CSS assets or Svelte runtime
are shipped. Missing/changed license inputs, uncovered origins/files or a
mismatched emitted file fail the build. The manifest hashes all other files;
the existing copier entry point verifies the final directory. The same command
builds the figure checker beside it, in `dist-math/figures`, with notices of its own
([figures](specs/figures.md#the-figure-check-as-built)). See
[primary inputs](../web/workspace/licenses/README.md). It uses the GUI's existing
dependency lockfile; no second KaTeX installation is introduced. Renderer and
launcher runtime bytes are unchanged by the notice emission.

The service operator supplies absolute paths to Node, the launcher and bundle
when constructing `MathChecker`. Node 22.12 or newer is required; the web CI
job uses 24.20.0. Node is not bundled into Python wheels or the orchestrator
image. No save hook or fallback
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

- At most two processes and 32 admitted formulas per checker; a batch has at
  most 32 unique span IDs. A default two-second deadline for each process includes
  waiting for a process slot; operators can choose 0.05 to 10 seconds. A batch
  whose process misses it, fails or answers wrongly is checked again in halves,
  down to single formulas, so one such formula leaves only itself unavailable.
- A request (the batch) is at most 256 KiB, each formula's answer at most 32 KiB
  and stderr at most 4 KiB. A formula that alone exceeds a request, or the
  launcher's 20,000 UTF-16 units, is unavailable without being sent.
  No raw stderr is returned to callers. Trusted configuration reads require
  ordinary files and cap the bundle at 8 MiB and launcher at 64 KiB.
- The GUI's 5,000 UTF-16-unit formula limit, 200 macros, 1,000 characters per
  macro, 2,000 expanded macro characters and KaTeX expansion/size limits apply.
  The launcher also refuses (as unavailable) a formula nested more than 150 levels
  deep, counting braces and `\left`/`\right` and adding its deepest macro
  definition: how deep KaTeX's parser gets before the stack runs out depends on how
  warm the process is, so without the limit a formula's verdict could depend on its
  batch. A cold process manages more than 450 levels.
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
advances its document's math target. Failure rolls back both. The math checks are
their own package, `orchestrator.mathchecks`, which depends on the documents and the
consistency review; neither depends on it. A save reaches it as an event: each process
that saves documents calls `mathchecks.subscribe` on its store (the harness when it
makes its `Stores`, the document commands, the workspace), and the store runs the subscriber in the save's
transaction (`ControlStore.subscribe` and `notify`). The target and
completed cursors reference retained revisions rather than copying an unbounded
queue on every save. Read-only workspace requests never advance state.

Only the current revision is checked (D82 as amended on 2026-10-14; refactoring
plan, decision 7). When the checker reaches a document, it claims the revision that
is current then and renders every parsed span of it under its effective macros;
the revisions saved since the last check are superseded and never rendered. The
same holds for a comment's and an explanation's body revisions. A save during a
check is checked next. Block ownership still comes from the history: the last
revision that added or changed a block proves its owner, whichever revision is
checked. A reflag therefore counts a check that finds the block's formula failing
after a newer proved edit, not every save. Results of earlier checks are not reused
for unchanged formulas.
Document front matter overrides the bounded [project setting](PROJECT-MACROS.md).
An unchanged formula is therefore checked again after a front-matter or project
macro change. A document revision atomically records the project setting revision it
was saved under; an explicitly numbered history view uses that revision, while a
current unnumbered view uses the current setting. Legacy history without a recorded
revision is explicitly unknown. Each check records the setting revision it used and
whether its context was held; the resolved definitions, warnings and context
identity are derived from that revision and the immutable sources when read.
Current-head setting refresh is lazy and bounded by the
active controller; changing settings does not fan out unbounded work or start an
idle scheduler. A late old-context result stays evidence but cannot decide current
findings, prove removal or finalize closing ACK.
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

Findings have `review` or `math` origin. Mechanical findings are `warn`/`math`, name their exact check and
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
Dependency refresh preserves a finding at its re-flag bound and explicit USER resolutions; a previously
mechanically accepted formula can reopen on a new rejection. Available accepted
checks also update the assessed block owner. Previous ownership is reconciled from
the last immutable available observation, including pre-fix accepted rows; missing
or invalid proof never consumes a reflag. Duplicate checks, unchanged spans,
unrelated edits and restarted attempts do not consume the two-reflag budget.
Two failing block edits leave it open at its re-flag bound, not offered again.
Current prose, error, evidence and eligible session attribution follow the new
check, while append-only history keeps the prior assessment and owner.

These notices are collected only at closing, together with terminal consistency
findings. They share at most two author response rounds; warnings never trigger
generic threshold interruptions. A notice counts as delivered only after a
successful author model call, and is acknowledged only after the run's record
is saved in the control store. Failed/zero/final-turn and late-result cases stay pending for a
later live turn. After the bounded response opportunity, the decision to leave a
still-rejected finding at its bound is staged under the exact notice claim. It is
finalized only after the run's record is saved, in the acknowledgement transaction,
with current source, exclusion, USER head and latest rejected evidence rechecked.
A failed save leaves the open notice retryable; an expired claim can rebind that
staging record, and a stale save callback cannot decide another run's claim.
This never invents an additional renderer rejection. Saved summary counts describe
the closing boundary before this save finalization.

[Project macros](PROJECT-MACROS.md) give document and comment checks their effective
contexts, and MAIN explanations their agreed original anchor contexts. Contradictory or unknown explanation contexts stay held and allow
only an inconclusive reviewer outcome; they cannot prove a mechanical fix or
scientific confirmation. Exact checked context remains available even where a
thread history uses explicitly diagnostic rendering. Renderer configuration
remains operator-owned and unavailable by default.

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
32 KiB; APIs page twenty records, and the tables check metadata when it is
written. Existing document/text/map limits also apply.

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
ownership proof. Saved run summaries disclose pending work; there are no live push
updates.

## Disposable browser fixture

After building the math bundle, run `.agents/labs/document_math.py create` with
explicit absolute `--node`, `--launcher` and `--bundle` paths. It creates marked
temporary synthetic state and exercises actual local KaTeX, with no model or
worker calls. It includes agent and USER failures, preserved block ownership,
accepted/removal resolution, unavailable evidence and two failing reflags.
`fix`, `unavailable` and `reopen` subcommands require that marker before opening
a writer; `reopen` supports concurrent USER CAS/draft checks.

## Validation boundary

Tests cover historical ownership,
stale/unavailable/configuration retries, write-time checks of the math tables, actual-work
cancellation, closing-round delivery and failed-save/ACK races. Checked in the
browser with actual local KaTeX 0.18.9 and Node 24.20.0 on synthetic documents in
[document math acceptance](../.agents/docs/agent-reports/document-math-2026-10-12.md)
and [comment math acceptance](../.agents/docs/agent-reports/comment-math-2026-10-12.md).
