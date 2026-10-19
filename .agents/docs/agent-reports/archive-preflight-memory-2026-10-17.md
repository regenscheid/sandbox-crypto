# Archive preflight resident-memory repair

This was a product regression. Repeated Archive.message preflight opened closed
sqlite3 Blob handles on the long-lived control connection. CPython retains their
weakrefs until connection close; review rescans amplified their count. Instrumented
400-turn reproduction peaked at 44,814,142 Python bytes. At 100 turns the Blob-open
line held 47,077 objects/4 MiB; at 200 it held 136,894 objects/11.5 MiB.

Modern SQLite now reads octet_length metadata without loading payloads or opening
Blob handles. Older supported SQLite retains exact Blob size checks, but performs
preflight and bounded payload fetch in one short-lived readonly store/snapshot.
No SQLite floor change, migration, payload deletion or threshold relaxation.

The unchanged 40/400-turn fold/review/finalization/retrieval scale gate passed after
this product fix. Its default pytest capture did not retain exact passing metrics;
only compliance with the original 32 MiB Python/64 MiB RSS/1 MiB final-record ceilings
is claimed. Three new tests initially failed due to missing fixture native_format;
corrected fixtures and existing archive/bounds/folded-view coverage passed 52 tests
in 12.49 seconds. A 5,000-read guard bounds retained allocation below 512 KiB. Forced legacy
and modern paths verify exact Unicode/NUL/native byte accounting and connection
closure after both successful read and cap refusal. Independent review clear.

Scoped strict Pyright reported 0 errors; Ruff, format and diff checks passed. Four pytest workers,
live/integration opt-ins unset, outside-home cache basetemp. No services accessed.
Raw scale gate (one scale pass, three subsequently corrected new-fixture failures):
/home/andy/.cache/pt/archive-preflight.log
SHA256: 99f54fb0644becc7dc1d88ff04451018c0da74f037ad4efd48f0697d4ccb8812
Raw focused gate: /home/andy/.cache/pt/archive-preflight-focused.log
SHA256: 881012ef79a0c3428fec1d47c9a8519003871657717d003b3d43a0e16c7814b4
