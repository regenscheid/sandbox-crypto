# Comment and reply math

Durable comment openings and replies now share the isolated mechanical checker,
findings and closing delivery of [document revisions](MATH-CHECKS.md) (D82,
comment portion of LD-53 to LD-55 and CR-36 to CR-38). Eligible MAIN fix/rebut
explanations have their own [additive slice](FINDING-PROSE-MATH.md). Ledger fields,
other excluded prose, idle scheduling and model repair proposals remain open.
The renderer stays operator-configured and unavailable by default.

## Immutable source and ordinary corrections

An opening or reply keeps its original entry, author and text. A correction
appends a thread entry and immutable body revision of that original. Projections
show the original and correcting actors, current body revision/hash and thread
revision. Addressed work requests stay pinned to their original immutable entry;
a displayed correction never substitutes a queued, leased or delivered request.

`document_comment_read` reads the exact current body and guards.
`document_comment_edit` requires the original entry, thread, expected thread
revision, body revision/hash and operation ID. Exact replay returns its original
result; changed payloads refuse. MAIN may correct same-session main/delegated
agent source. Delegated runs may revise only their original source and cannot
overwrite later MAIN corrections. Agents cannot revise USER, reviewer or foreign
source. The legacy opening-edit path applies the same current-body authority in
addition to its existing opener restriction. A finding grants no extra authority.

Authenticated USER corrections use the existing same-origin comments route and
revise only USER-owned original bodies. There is no USER takeover permission for
agent-original comments. Thread resolution and request responses keep their
existing authority. Empty corrected bodies are legal; original/corrected text
remains in immutable history.

The GUI retains an independent body draft and exact-payload retry ID for each
original entry. Stale thread/body guards preserve the draft. Adopting current
guards is explicit and retains its text; reply drafts remain separate.

## Checks and ownership

The annotation transaction saves body identity/ownership and advances a compact
math target atomically. Parsing and stable-ID matching happen before the write
lock. Unchanged/moved blocks retain their proved last-changing actor. An unrelated
opening, reply or anchor change cannot acquire that text. Legacy/unknown ownership
is excluded, never guessed from the latest thread author.

Bodies use ordinary Markdown, matching their GUI renderer. YAML-looking text and
document containers grant no front matter or macros. Each attempt pins the exact
body revision/hash, stable block/span, parent document/project, immutable parent
document macro revision/hash and project setting revision/hash. Resolved effective
definitions and context identity stay with the check. Document definitions override
the bounded [project setting](PROJECT-MACROS.md). Every span is rechecked when
effective document or project macros change;
invalidation remains separate from authorship and the two-reflag budget.
Only a new proved edit to the failing block consumes a reflag. Repeated macro or
renderer changes refresh exact assessment/history without consuming either failed
patch opportunity. Last available immutable ownership proof covers accepted checks
and older stale owner caches; unavailable or invalid proof cannot spend that budget.
Dependency refresh preserves unaddressed and explicit USER resolutions.

Thread history renders under the selected document's diagnostic macro context,
with a visible warning; it does not claim to reproduce each body's save-time
rendering. Exact mechanical check contexts remain accessible in check evidence.
Setting changes are discovered lazily by the active controller in bounded steps,
without an idle scheduler or unbounded work in the settings transaction. A stale
context cannot decide current findings or acknowledge their closing notices.

Admission, load, lease fence, current-result projection and ordinary reads derive
scope through the canonical original entry → thread → document/project. A derived
queue row cannot grant another document's macro context or project access.
Disjoint internal comment state keys never masquerade as document revisions or
read grants. Current accepted evidence or complete proved removal can confirm a
mechanical finding. Stale, pending, skipped, held and unavailable checks cannot.
Generic model reviewer decisions/supersession stay forbidden; USER decisions keep
their exact finding CAS and immutable history.

An older opening-edit writer may change history without this body hook. Checks
become visibly held, while explicit authorized ordinary edits still work and
establish a baseline with unknown unchanged ownership. Legal annotation Markdown
beyond derived parser/identity bounds is likewise retained exactly with held
checks. Returning to a parseable baseline cannot prove that old formula IDs were
removed. Former held revisions become explicit unavailable attempts when later
work resumes; they never silently resolve old findings or invent attribution.

## Shared budgets and delivery

Comments and documents share two active attempts, one running lease per parent
document, 32-span steps, the eight-step live yield and existing global logical
attempt/observation/ownership budgets. Quotas hold backlog visibly without
evicting unresolved evidence or vetoing ordinary annotation saves. Canonical
bounded annotation history retains its existing append-only storage contract;
automatic compaction is absent. Metadata is prefix-bounded before decoding, even
for damaged parent/queue/body rows. Complete body/maps are read one source at a
time under the existing 16 KiB body and 16 MiB parsed-map bounds.

Comment notices share closing-only delivery with document math and terminal
reviewer findings, and the same maximum two author response rounds. Successful
model delivery and durable run save precede acknowledgement. Exact current
source/body/macro/attempt, USER state and claim ownership are rechecked at
finalization. No independent comment response budget or idle controller exists;
outside-turn notices wait for the session's next live main-agent run.

## Disposable acceptance fixture

Build `web/workspace` with `npm run build:math`, then run:

```bash
uv run python .agents/labs/comment_math.py create \
  --node /absolute/path/to/node \
  --launcher /absolute/path/to/web/workspace/dist-math/check-math.mjs \
  --bundle /absolute/path/to/web/workspace/dist-math/mathcheck.mjs
```

This creates marked `/tmp/comment-math-browser-*` state with synthetic text and
actual local KaTeX; no model, worker or service calls occur. `fixture.json` and
`initial-evidence.json` retain exact source/provenance and check snapshots. Cases
include opening/reply failures, USER exclusion, held text, accepted/removal,
unavailable, two reflags and later macro/anchor context. A pre-macro assessment
proves an unrelated USER opening edit did not reflag the delegated reply.
The final audit also requires unchanged agent opening/delegated reply reflags zero
after the macro/anchor update, while two actual failed body edits remain at two.

`main-fix --state-dir <marked-state>` makes an ordinary MAIN correction of the
delegated reply. `concurrent-user-edit` corrects the USER-owned opening for stale
draft/CAS tests. Both take the same renderer paths; `audit` needs only the marked
state. Every action checks the marker before opening a writer. Browser/server
acceptance and cleanup require separate coordination. Native browser acceptance
and final combined gates are separate from these local fixtures.

## Validation boundary

Signed source `c1142e1` and exact-main union `7fa43e6` passed predecessor review
and combined gates (9,613 Python tests). Preliminary browser acceptance then found
the shared dependency-only reflag-budget bug. Final signed source `77ecc171` and
helper SHA `972b459ba8223cb374818098e375c948ddddbee3dc817ac0b5027051b116a62e`
passed independent exact-head review, including accepted-owner reconciliation,
old persisted caches, USER decisions and bounded corrupt proofs. Seventy-eight
focused cases and all corrected-source gates pass: 9,631 Python tests on 3.13.15
(43 opt-in skips, two existing Pydantic serializer warnings), 441 web tests,
24 post-build math checks, locked/static/license/protocol checks and both builds.
The supported Python 3.12 matrix is unchanged.

Fresh root-owned [browser acceptance](../.agents/docs/agent-reports/comment-math-2026-10-12.md)
used actual local KaTeX 0.18.9/Node 24.20.0 and synthetic state. It verified
dependency-only zero reflags, two actual failed edits/unaddressed, unavailable
holds, USER-only-own-body editing, independent body/reply drafts across reload
and stale 409, explicit current-head adoption/save, and MAIN correction of a
delegated reply with immutable original author/history. Exact accepted evidence
and confirmed-fixed history, a distinct removal reason and session-b isolation
passed. Final capture had no warning/error logs. The browser tab, transient server
and marked state were removed at 13:50:58.272097 UTC. No model, worker or provider
calls occurred. Source/test/helper/assets stayed frozen through gates and final
acceptance. PR #87 merged at `d2b133f` after all three fresh CI checks passed.
The subsequent [project-macro validation](PROJECT-MACROS.md#verification-boundary)
covers setting inheritance, exact attempt pins and dependency refresh. Ledger,
excluded prose, model repair proposals, idle scheduling and automatic compaction
remain open.
