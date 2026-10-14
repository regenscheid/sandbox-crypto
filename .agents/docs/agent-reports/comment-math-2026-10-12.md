# Comment and reply math browser acceptance — 2026-10-12

The production workspace GUI was checked on babbage against marked, disposable
synthetic control state. Final acceptance used independently reviewed signed source
`77ecc171675005b6ac24586681af6033cf1ccdf9`, including main `9c07725`, and the
[`comment_math.py`](../../labs/comment_math.py) fixture with SHA-256
`972b459ba8223cb374818098e375c948ddddbee3dc817ac0b5027051b116a62e`.
The fixture used actual local KaTeX 0.18.9 under Node 24.20.0; normal development
and Python tests used 3.13.15. No model gateway, worker, OpenShell VM or research
execution was contacted.

## Observed behavior

| Check | Observed result |
| --- | --- |
| Opening and reply rejection | Agent source produced open mechanical math findings with exact original entry, body revision/hash, stable block/span, document macro revision/hash and renderer identity. No reviewer model was used. |
| Dependency refresh | An unrelated USER opening edit and later document macro/anchor change retained the delegated reply's original author and zero reflags. The unchanged agent opening also retained zero reflags. |
| Actual failed corrections | Two distinct failing body edits produced two reflags and `unaddressed`; a later macro refresh preserved that state and budget. |
| Accepted and removed formulas | Both were `confirmed fixed`, with distinct immutable history reasons: current renderer acceptance versus removal from complete current source. |
| Unavailable and held checks | An unavailable latest attempt left its historical finding open. Legal deeply quoted Markdown remained stored exactly, with visibly held derived math checks. |
| USER scope | USER source had mechanical observations and no agent finding. Only the USER-original body exposed the authenticated USER body editor; agent-original bodies did not. |
| Independent drafts | A body correction and separate reply draft both survived a browser reload. A concurrent ordinary USER correction advanced the body/thread guards. Saving the stale body draft was refused as a conflict and retained both drafts. |
| Explicit adoption | Selecting current body/thread revisions retained the correction text. Saving then appended USER body revision 4; the independent reply draft survived until explicitly discarded. |
| MAIN correction | An ordinary MAIN correction appended body revision 2 to the delegated reply. Original delegated authorship/text and immutable thread history remained visible; current text and correcting MAIN author were separate. |
| Mechanical confirmation | The exact MAIN-corrected `$x^2$` body, macro revision 2 and MAIN ownership were shown in accepted renderer evidence. Its finding became `confirmed fixed`, revision 3, with zero reflags and the exact acceptance reason. |
| Session scope | Session `b` showed no session `a` findings or selected evidence. The project-wide mechanical check inventory remained visible as intended. |
| Diagnostics | The final console capture contained no warnings or errors. The stale-save conflict above was intentional and visible in the GUI. |

Screenshots retain the initial pinned reply finding (archived outside git, sha256 `ec8a0d10bdfa`),
stale retained drafts (archived outside git, sha256 `1693e2197c42`),
saved USER correction (archived outside git, sha256 `c1d175c3c677`),
MAIN correction and original delegated source (archived outside git, sha256 `f027e4239d37`)
and mechanical confirmation history (archived outside git, sha256 `a1c0373a821d`).

The evidence archive (archived outside git, sha256 `18f334198b5c`) retains 21
exact UTF-8 records, including initial/final canonical audits, browser observations,
the final raw Python test log and cleanup. Each record and screenshot has its byte
length and SHA-256. All 71 GUI/math asset hashes were checked before and after
acceptance; the helper remained unchanged. Authentication URLs, cookies, raw
server journals and SQLite databases are excluded.

## Preliminary failure and corrected acceptance

The earlier browser run on signed `7fa43e6` found that a document dependency-only
refresh spent the shared document/comment failed-patch budget without any author
edit. Preliminary observations and their complete cleanup are retained separately
in the archive. They are not final acceptance evidence.

The corrected source separates exact assessment refresh from proved changes to the
failing block. It also reconciles accepted owners and retained available assessment
proofs, preserves USER decisions and unaddressed state on dependency refresh, and
bounds damaged proof data before decoding. Independent source/probe review cleared
`77ecc171`; the fresh fixture above confirmed the corrected behavior. The first
archive assertion used an incorrect spelling of the stored `confirmed fixed`
status; correcting that bookkeeping assertion required no source, fixture, test or
timeout change.

## Validation boundary

Final corrected-source gates passed 9,631 Python tests, 43 opt-in skips and two
existing serializer warnings, 441 web tests and 24 post-build math checks. Locked
dependency sync, Ruff format/lint, strict Pyright, Python/npm license checks,
generated protocols, GUI checks and both builds exited successfully. The supported
Python 3.12/3.13 CI matrix remains unchanged. Fresh CI on the final signed docs
descendant is a separate merge gate.

This acceptance covers the comment/reply portion of LD-53–55 and D82, ordinary body
corrections, source ownership, mechanical findings and browser conflict handling.
Separate local/adversarial tests cover authority, migration, corrupt metadata,
canonical parent scope, operation replay, global budgets and closing delivery.
It does not validate a live model turn, model judgment, idle scheduling, ledger
fields, finding-prose math, model repairs or worker document publication.

## Cleanup

The root-owned browser tab was closed, the transient loopback server stopped and
the marked fixture removed. Unit inactivity, no listener on port 35999 and absent
fixture state were verified at **13:50:58.272097 UTC**. No persistent unit,
provider configuration or worker state was created.
