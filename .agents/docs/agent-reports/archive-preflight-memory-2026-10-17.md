# Archive preflight resident-memory repair

This was a product regression. Repeated Archive.message preflight opened closed
sqlite3 Blob handles on the long-lived control connection. CPython retains their
weakrefs until connection close; review rescans amplified their count. Instrumented
400-turn reproduction peaked44,814,142 Python bytes. At100turns the Blob-open line
held47,077 objects/4MiB; at200 it held136,894 objects/11.5MiB.

Modern SQLite now reads octet_length metadata without loading payloads or opening
Blob handles. Older supported SQLite retains exact Blob size checks, but performs
preflight and bounded payload fetch in one short-lived readonly store/snapshot.
No SQLite floor change, migration, payload deletion or threshold relaxation.

The unchanged40/400-turn fold/review/finalization/retrieval scale gate passed after
this product fix. Its default pytest capture did not retain exact passing metrics;
only compliance with the original32MiB Python/64MiB RSS/1MiB final-record ceilings
is claimed. Three new tests initially failed due to missing fixture native_format;
corrected fixtures and existing archive/bounds/folded-view coverage passed52tests
in12.49s. A5000-read guard bounds retained allocation below512KiB. Forced legacy
and modern paths verify exact Unicode/NUL/native byte accounting and connection
closure after both successful read and cap refusal. Independent review clear.

Scoped strict pyright0errors; ruff/format/diff checks passed. Four pytest workers,
live/integration opt-ins unset, outside-home cache basetemp. No services accessed.
Raw scale gate (one scale pass, three subsequently corrected new-fixture failures):
/home/andy/.cache/pt/archive-preflight.log
SHA256: 99f54fb0644becc7dc1d88ff04451018c0da74f037ad4efd48f0697d4ccb8812
Raw focused gate: /home/andy/.cache/pt/archive-preflight-focused.log
SHA256: 881012ef79a0c3428fec1d47c9a8519003871657717d003b3d43a0e16c7814b4
