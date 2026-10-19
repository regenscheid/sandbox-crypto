# Delegation history memory

A parent retains at most its configured session concurrency cap (maximum20) of
live or not durably settled child task handles. Before admitting more work and
at response boundaries it releases completed handles only after reading the
immutable terminal delegation record. A failed terminal write retains ownership
and blocks admission at the cap; it never makes a child disappear successfully.

Exact started and model-consumed outcome identities live in scoped disk-backed
run collections/members scoped by the full timestamped attempt name. Started
collections and canonical response receipts are included in evidence exports; the
auxiliary members table itself is not part of export v4. Started identities commit in
the same transaction as child admission; failed bookkeeping cannot launch a child.
The final summary retains at most100 identities per category plus an exact collection reference and count
when larger. Final acknowledgement streams consumed identities in batches100;
canonical response receipts remain authoritative before final save. A completed
child remains available to status/wait/cancel through its durable record, with
cancel reporting false after settlement. No source outcome is deleted.

Finding notice bookkeeping remains bounded by the existing128 notices per run
ceiling in LeasedNotices.offer. Pending unconsumed tool handbacks are bounded by
the active semantic transcript ceiling and are cleared on each successful response.
