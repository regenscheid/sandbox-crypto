# Bounded run memory

Stored runs keep their exact conversation and tool evidence in the immutable archive.
The live model view and historical evidence are separate: folding may change the view,
while historical sequences are paged from disk. A history slice is a lazy sequence;
only the bounded active view is materialized for a model request. Unstored development
runs retain the existing in-memory representation.

Per-run historical message, tool, usage and delivery collections use scoped durable
storage. Final records contain an explicit versioned reference to those collections;
readers must page that reference rather than treating omitted inline history as empty.
Saving validates and serializes bounded metadata, then acknowledges deliveries in
bounded pages only after the final record commits. Recovery follows the same pages.
No persistence failure permits further model/tool dispatch.

The supported default working view is at most 8 MiB of encoded JSON. Individual
historical reads are at most 1 MiB unless a caller explicitly requests offline full
materialization. Collection discovery pages contain at most 100 identities. A process
admits at most four root runs, including their bounded child allowances; the service
waits before admitting further roots. A view that cannot fit its semantic content is
reported as a context limit, preserving its archive; it is never silently cut to fit.

Review ingestion consumes newly appended messages and tool records incrementally;
archive matching does not reconstruct the run. Fold calibration uses a bounded sample,
while cumulative usage remains durable and exact. Fold/view metadata and preserved
user words are streamed or reduced under the existing semantic rendering rules.

Acceptance compares the same production loop with 10x history at fixed view and
per-call payload limits, crossing multiple folds and review checkpoints, final save,
reopen and exact early/middle/late evidence retrieval. Before execution, the fixture
fixes an additional Python allocation ceiling of 32 MiB, RSS ceiling of 64 MiB and
final inline record ceiling of 1 MiB. Both scale cases must stay within these bounds;
a positive full-history control must exceed one of the memory ceilings. This measures
implementation memory, not model research quality or a live OpenShell deployment.

The scoped history reference is `run-history-v1`. Its collections are internal paged
indexes; tool rows contain retained references from their first write, while the one
currently observed full record exists only as a transient overlay. Scratch runtimes
encode native assistant bytes and adapter provenance explicitly as JSON/base64 rather
than flattening them into chat dictionaries. Successful-response receipts retain the
first incorporating assistant message for each source/notice identity.

Schema 63 makes collection rows immutable at the database boundary. Evidence export
v4 includes those rows and successful-response receipts; verification retains the
unchanged v1/v2/v3 table contracts. The authenticated run history endpoint pages at
most 100 rows and 128 KiB, with exact JSON bytes available in 64 KiB chunks when a
single row exceeds the discovery bound. Mutable delivery caches are not evidence.

Archive preflight reads byte-size metadata without retaining per-payload handles.
SQLite3.43+ uses octet_length, which does not load TEXT/BLOB bodies. On older
supported SQLite, the exact Blob-length check and bounded payload fetch share one
short-lived readonly connection/read snapshot. Closing that connection also releases
CPython's accumulated closed-Blob weakrefs. This preserves the existing SQLite
baseline and rejects oversized reads before materializing their payload in Python.

Every remaining incremental Blob reader uses a short-lived readonly snapshot for
one bounded read: final run-record preflight/fetch, tool-record chunks, collection
chunks and each evidence-export chunk. The connection closes before a value is
returned or yielded, including on refusal and cancellation. Repeated API reads and
one arbitrarily long export therefore cannot accumulate closed-Blob weakrefs on a
persistent application connection. Search uses metadata-only sizing on modern
SQLite; its older-SQLite fallback closes the whole search snapshot after the bounded
result page. Project/session/run selectors and immutable-row checks remain in the
same snapshot as the read. Original bytes and evidence format remain unchanged.

*Proposed 2026-10-25 (B9, awaiting the owner's review):* the provenance that memory recall gives each item ([project memory](project-memory.md#b9-5), PM-13) includes a `scope` that may be `run`. Bounded run memory is the storage of a run's history, which the history tools read, not notes a memory tool recalls. So no memory item has the scope `run` today, and nothing on this page changes.
