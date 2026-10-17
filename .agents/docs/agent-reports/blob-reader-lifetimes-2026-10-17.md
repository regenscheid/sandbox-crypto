# Bounded lifetimes for remaining Blob readers

The remaining Blob readers also used persistent application connections, retaining
closed-handle weakrefs across requests and export chunks. Added isolated_read: parent
initialization/migration and nested-read checks remain under its lock, while one
readonly child snapshot closes before returning data. ProjectStore.run, collection
chunks, tool-record chunks and each evidence-export chunk use this scope. Search
uses metadata-only sizing on modern SQLite and one isolated search snapshot on older
supported SQLite. Prior legacy archive preflight paths now reuse the same seam.

Independent review caught first-read migration compatibility: opening only a readonly
child would reject older files before a lazy writer could migrate. The shared helper
now initializes the original store first. Regressions verify readonly refusal and
writer-first migration through both run-record and legacy archive reads.

The aggregate fixture performs1000 repeated run/chunk/search reads plus10 streamed
multi-chunk exports on one persistent store, keeps exact UTF8/NUL bytes and export
checksums, and stays below512KiB retained Python growth. It asserts zero active child
readers before every consumer pause, after scope/cap errors and after generator
abandonment. No existing thresholds, evidence format or security selectors changed.

Focused control-store/projects/tool-journal/evidence-export/preflight/new-memory gate:
137passed in13.32s,4workers, outside-home cache basetemp, live/integration opt-ins
unset. Scoped strict pyright0errors, ruff/format/diff checks pass. Independent product
review clear. No services or host configuration accessed.
Raw log: /home/andy/.cache/pt/blob-readers.log
SHA256: 6d2af2bd6ddb27195710b257fbc269b65f472e364e15ccb3f88c8613c5ce7c0e
