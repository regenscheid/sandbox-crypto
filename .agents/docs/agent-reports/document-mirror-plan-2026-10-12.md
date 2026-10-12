# Document mirror and helper: interface plan

Prepared 2026-10-12 from LD-3 and LD-49 to LD-52 in the
[living-documents specification](../../../docs/specs/living-documents.md).
**Proposal only. No mirror/helper implementation or acceptance is claimed.**
Source changes wait for coordination with the comments/consistency author.
The existing document parser, source-to-patch engine and control-store rules
remain authoritative; this is an independent implementation.

## Boundaries to agree first

| Interface | Proposed owner and behavior |
| --- | --- |
| Revision publication | Control author: durable outbox for every saved/deleted/promoted document, including GUI, import and helper changes; worker: authenticated project-scoped ingestion and durable catalog |
| Direct-edit reconciliation | Worker: durable candidate/base/observed attribution; control author: worker-origin source-to-patch entry point, retained rejection and normalized current revision |
| Helper dispatch | Worker/contracts: typed numbered request event and bound reply; harness: allowlisted document operations with delegated authority, including the comments author's operations |
| File-tool completion | Harness/worker: drain the same durable direct-edit outbox before the outer write-file tool result, since file requests currently have no execution event stream |
| Findings and attribution | Control author: a worker-origin Author variant with environment and optional observed session/execution; normal comment anchors and later math checks apply to committed revisions |

`DocumentStore.edit_source` already derives bounded named operations through
`documents.source.operations`, but permits only USER attribution. It cannot be
called unchanged for worker edits. `Author` currently has no worker variant.
`WorkerClient.run` supports an event callback; `Tools._run` currently supplies
none. These are the concrete integration seams, not completed features.

## Proposed records and transport

- **Publication:** project/document ID, immutable revision, monotonic catalog
  sequence, canonical scope/name/session path, content SHA-256 and bounded
  UTF-8 bytes or a tombstone. Catalog sequence is separate from revision because
  workbook promotion changes a path without necessarily changing its text.
  Snapshot/delta pages have record and aggregate-byte bounds. A partial page
  never authorizes deleting absent files; a new environment needs a completed
  current snapshot before its first dispatch.
- **Direct edit:** worker-generated edit/operation IDs; project, environment and
  sandbox incarnation; document/path, known base revision and raw digest;
  observed candidate sessions/executions; bounded candidate content or a retained
  raw-content reference. Persist it before publication to the control adapter.
  An acknowledgement includes the document ChangeResult/rejected patch ID and
  normalized current publication. Retries preserve the operation and payload.
- **Platform call:** worker-generated request ID, original execution/attempt and
  incarnation, allowlisted operation, bounded arguments and deadline. Code sends
  no author or project/session context. The worker observes and binds context;
  the harness narrows authority to delegated even if a main agent started it.
  A reply binds that exact pending request and context; duplicate identical
  replies replay, conflicting or late replies refuse.

Candidate/reply rows and helper lifecycle events must survive worker/harness
disconnect independently of evictable stdout chunks. Source cursors commit with
the corresponding worker events/state. A document mutation that committed before
its acknowledgement was lost must return that original outcome, never a second
revision. Pending work has explicit count/byte/time bounds and backpressure.

## Worker implementation sequence

1. Add a separate `documents_sync.py`, catalog and written-revision ledger.
   Expose `PROJECT_DOCUMENTS`; project files use `documents/<name>.md`, workbook
   files `documents/sessions/<session>/<name>.md`. Stage authenticated revision
   bytes and atomically publish through descriptor-relative safe file operations.
   `shared/` sync never traverses or adopts this area. Record every written
   revision/time for each environment and managed path.
2. Capture pre-dispatch revision windows and post-call differences for calls,
   jobs and file writes. Overlapping writers retain the oldest possible base;
   the finishing call alone does not prove authorship. An unknown base is retained
   as a rejection against the current revision. Restore current normalized bytes
   only after durable candidate retention/acknowledgement. Deleted/renamed files
   do not delete/rename documents; new and stray files stay local and are reported.
3. Provide stdlib `ss-doc` and `ss_doc` in every environment independently of
   research packages. Use a local executor socket, existing daemon channel,
   worker execution event and harness reply. No sandbox route to the orchestrator.
   Bind peer PID/start lineage or the observed active kernel where established;
   ambiguous attribution remains at environment/session level. With no active
   call, including a kernel thread between cells, return `unavailable`.
4. Add bounded cancellation/reply deadlines, daemon replay, lifecycle shutdown,
   new-incarnation reset and disconnected control-adapter recovery. First expose
   document read/create/patch/comment operations agreed with the control author;
   ledger and other platform names refuse until their adapters exist.

Worker ownership: contracts/protos, worker code/tests and `docs/WORKER.md`.
Likely hooks: executor, daemon, backend helper upload, manager dispatch/finalization,
store and file tools. Control ownership: Author, source reconciliation/rejections,
publication outbox, document dispatcher and comment/finding semantics. Parent
coordinates WorkerClient/Tools, tracking and the real OpenShell lab.

## Retention and writer-close questions

LD-49 forbids replacing a file that a process holds open for writing. The current
`being_written` check explicitly skips unreadable peers; Lab20 demonstrated that
OpenShell peer `/proc` permissions can prevent ownership inspection. Reusing that
check would not prove this rule. Publication should conservatively defer while
relevant calls/known descendants may write, and uncertainty must produce a held
sync notice rather than overwrite. Long-lived kernel/background writers still
need an agreed close-proof strategy before claiming LD-49 complete.

Non-UTF8, oversized and deleted-document candidates also need an explicit bounded
raw rejection representation. Do not report an edit fully retained if only a
sample was stored, or destroy its only copy before durable retention. These two
seams require interface agreement; this note chooses no privileged process
inspection or unbounded storage workaround.

## Verification

Cover LD-F23 to LD-F27, disjoint rebase versus same-block conflicts, normalization
and surviving comment anchors, oldest-base overlapping sessions/jobs/delegations,
unknown bases, tombstones/promotion, partial snapshots, invalid/oversized input,
and local-only stray files. Negative tests attack symlinks, FIFO/devices, hardlinks,
parent rename/swap, open writers, forged context, cross-project access, delegated
status changes, duplicate/conflicting replies, timeout and cancellation. Inject
disconnects at candidate/report/apply/ack commits and require one revision.

Local fixtures tear down helpers and daemons. A later real OpenShell lab must
exercise two environments, normalized direct edits/rejections, helper
read/patch/comment, a late kernel-thread refusal, disconnected recovery and the
writer-close boundary. Local passing tests alone do not establish those results.
