# Bounded structured tool retention

2026-10-16, local `codex/research-foundations`, following `1f7c0445`.
Implementation and deterministic verification only; independent review, integration
and live OpenShell acceptance remain pending. No model gateway or real worker ran.

## Change

After committing a structured tool result, the run prepares a detached historical
copy. Records up to 64 KiB stay inline. Larger records keep at most 16 KiB of preview
JSON, including a versioned project/message reference, omission count and bounded
outcome metadata. Preview fields are whole, individually bounded values. The original
result is available to live observers and the on-tool callback; a `finally` block then
replaces it and releases coroutine locals, including on error or cancellation.

The journal remains the full evidence source. The final saved/exported run keeps the
preview and reference without loading the full original. Event fallback understands
the stored outcome; the workspace labels a preview and identifies archived skill-use
details. A run without an archive retains its full inline evidence.

## Memory evidence

`test_tool_retention_memory.py` uses Python 3.13.15 on Linux, fresh processes and a
benign in-process tool producing 1 MiB text results. It measures the real loop,
archival, final record validation, control-store save and JSON export together.
Peak RSS is Linux `VmHWM` above the initialized process, and Python allocations are
measured with `tracemalloc`. No fixture payload exists before measurement.

| Case | Result payload volume | Python allocation peak | Additional peak RSS | Final run JSON |
| --- | ---: | ---: | ---: | ---: |
| Journal, 4 calls | 4 MiB | 3,472,629 bytes | 9,207,808 bytes | 16,814 bytes |
| Journal, 40 calls | 40 MiB | 3,623,532 bytes | 10,743,808 bytes | 55,842 bytes |
| Inline positive control, 40 calls | 40 MiB | 134,043,525 bytes | 217,055,232 bytes | 41,991,082 bytes |

The journal cases pass the fixed 12 MiB Python / 32 MiB RSS / 256 KiB final-record
gates. The inline control exceeds both memory gates and keeps the full payload volume
in its run JSON. A separate wide-record gate constructs 200,000 fields before measuring
preview creation and requires less than 1 MiB of additional Python allocations.

The targeted 14-test gate also covers exact Unicode/NUL and original argument recovery
after reopening, normal/failing/cancelled observers, callback failure, report-round
refusals, no-journal compatibility, detached snapshots and deeply nested previews.
Browser checks: 2,179 tests pass, including preview labeling and archived skill details;
type/format checks and the production build pass. The build reports its existing large
chunk warning; the DOM tests log disabled iframe loading while passing.

The final full orchestrator/workspace gate passes **4,550 tests**, with 17 skips and
two warnings in 317.79 seconds. Four skips require a real gateway; thirteen require
the separate math build. Warnings come from the existing weak legacy metadata fixture.
Real gateway/integration flags were removed from the test process environment. Ruff,
pyright, repository hygiene, generated API-type checks and installed license checks pass.

Raw evidence stays outside git under `/home/andy/.cache/pt/`:

| Log | SHA-256 |
| --- | --- |
| `research-foundations-retention-verified.log` | `7a63ccd469c3312afbbfd52ed8bafdf3e0e7e4552759c4e7a2a2022e000c64c1` |
| `research-foundations-retention-web-final.log` | `82777e92f68bca54969b4381eeb02d7d99eae8895d2a161b703cf2ff77554545` |
| `research-foundations-retention-web-build.log` | `3773ddf284735b6119fd3ed3b2838b7a2e1c1458e3847dd8bd0238808992b174` |
| `research-foundations-retention-final2.log` | `a2cc17ee14f8e777c3c1b47da38b528b91e5c37b934d4b9c38802d719d77b20a` |

## Limits

This isolates structured output retention. Model messages, the number of small records,
usage/delivery state, review caches and archive/view reconstruction still need their
own bounds. Standalone run JSON now contains references for large archived records;
it is not a portable full-evidence bundle. The existing byte-page API retrieves those
records. No whole-run bounded-memory, tree Resume or OpenShell confinement claim follows
from these results. See [research continuity](../../../docs/specs/research-continuity.md).
