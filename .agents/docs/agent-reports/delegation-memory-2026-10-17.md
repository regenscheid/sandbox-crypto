# Bounded delegation history

The parent retains only live/unsettled child task handles (at most configured cap,
maximum20). Exact started identities commit atomically with admission into the
exported run collection; consumed tool outcome IDs use disk-backed members. Final
summaries retain at most100 IDs per category and name the exact backing table,
collection, run and total. Final acknowledgement streams100 IDs at a time.
The auxiliary received-members table is not directly exported by v4; canonical
response receipts are exported and preserve the exact successful consumption.
Canonical response receipts still protect the observed-before-final-save window.
Completed historical cancellation resolves the immutable delegation parent/outcome.

A real240-child sequential lifecycle fixture covers wait, successful response,
prune, old-child retrieval/cancel and final acknowledgement of all240 IDs. It
asserts zero completed task handles after each response, exact disk history, bounded
summary and less than2MiB traced resident growth between24 and240 children.
Injected history-write failure rolls back admission before any child task starts.
An unsettled terminal record retains its task handle and blocks new admission.

Validation: initial affected delegation/lifecycle/depth/tasks/settlement gate:
130passed,4 old policy fixtures failed (40.42s). After integrated budget/status
fixes and narrow fixture alignment, final focused gate7passed (6.27s), including
all three new memory/atomicity tests. The token-reservation fixture now expects no
unreachable delegated model request; all report/evidence assertions remain.
Strict pyright on both changed product modules and the new test:0errors. Scoped
ruff and git diff --check passed. Tests used4workers, cache basetemp, and no live
or integration environment. Pytest output was captured by the tool transcript;
no separate raw log file/hash was retained. No services were accessed.

Independent review identified and fixed a fallible post-admission history-write
window: history now commits within the admission transaction. Finding notice
sets were separately checked: LeasedNotices.offer caps them at128 per run.

Full attempt scope follow-up: both admission and hook bookkeeping use the complete
parent name, including its timestamp. Two attempts reusing one CLI run ID retain
separate started/received identities; each full-name EvidenceExport finds its own
started collection. Four memory tests passed in4.31s; scoped strict pyright0errors,
ruff and diff check passed. Root independent review cleared the scope correction.
Raw log: /home/andy/.cache/pt/delegation-full-scope.log
SHA256: 014cfbe7040de81244acdde9907e68b009d6f092a04692e9b8d24d32561a85ce
