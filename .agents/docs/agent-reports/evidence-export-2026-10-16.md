# Stored-evidence export, 2026-10-16

Local implementation adds an authenticated, scoped streaming download and an offline
verifier. The download preserves the stored final run JSON, messages, native provider
bytes, saved text samples, tool intents and full tool records. External files and
referenced document revisions remain references. This is not a restore/replay API.

The targeted suite passed 6 tests. It covers exact Unicode/NUL/native bytes,
interleaved projects/sessions/runs, later appends excluded from a captured snapshot,
an interrupted intent with no response, empty fields, truncation/corruption refusal,
HTTP validation and two-download admission with release after disconnect.

Fresh processes exported and verified five large fields, including a legacy final
run JSON. Fixed gates were Python peak < 2 MiB, additional RSS < 12 MiB, and each
encoded line < 90,000 bytes. At 2 MiB per field: Python peak 484,601 bytes and RSS
increase 4,861,952 bytes. At 20 MiB: Python peak 612,544 bytes and RSS increase
5,029,888 bytes. The positive full-read control at 20 MiB used 42,039,579 Python
bytes and 43,941,888 additional RSS bytes, exceeding both gates.

Evidence: `/home/andy/.cache/pt/research-foundations-evidence3.log`; SHA-256
`1ffd1ad985e19098c3fff9f0348dd9376f7fa6b0dfb125bc7b03a3e2b187d09c`. The full affected-member suite is
recorded separately in the handoff when it completes. No real model or OpenShell
instance was used for these fixtures. Whole-run working-memory bounds, portable
external artifact packaging and tree recovery remain pending.
