# Bounded-memory fixture alignment

The folded-view positive control called Archive.messages after that public API had
acquired its 8MiB aggregate cap. The resulting WorkingMemoryLimitError was correct.
The control now deliberately retains list(Archive.scan(...)) for the large assistant
and tool originals, reproducing accumulation without bypassing the production page
cap. All streamed and control Python/RSS/output thresholds remain unchanged. A direct
retained-fixture control measured42,115,866 Python and45,264,896 RSS bytes, exceeding
both original16MiB control thresholds. Failed subprocess assertions now include stderr.

The long-answer chat fixture now verifies the bounded final-record preview, its
explicit evidence marker and head/tail, the existing bounded event, and exact full
answer retrieval from the scoped assistant journal. Production behavior is unchanged.

Both affected modules:15passed in6.68s,4workers, outside-home cache basetemp and
live/integration opt-ins unset. Scoped strict pyright0errors; ruff/format/diff checks
passed. Raw log: /home/andy/.cache/pt/memory-fixtures.log
SHA256: cd60e36a8397413233c15e476e2d763406d709b6c88a94c260e269a0b62cc73a
