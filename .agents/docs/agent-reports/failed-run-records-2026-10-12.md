# Failed-run records acceptance — 2026-10-12

The actual harness CLI retains partial observations after an ordinary model
failure, and the production workspace displays the failed status, completed
tool result and absence of a final answer. Cancellation propagates through
later cleanup errors without saving or acknowledging a run.

## Source and checks

- Final signed source: `123b2e6efcc4762cac0bf78e43681c92d2e9516f`, based on main `3c91c303`.
- [Disposable helper](../../labs/failed_run.py) SHA-256:
  `a96c5257c37eb1b406b32e78f509e306a2dc2e311637d297234ead6b4d97603a`.
- Independent review passed partial model/tool/harness failures, incomplete
  tool calls, delivered versus unseen notices, failed saves, session ownership,
  legacy records and mixed scientific-review/math closing. A failed record can
  acknowledge delivered math text after saving without finalizing staged
  ignored-math outcomes; scientific decisions already completed remain intact.
- Cancellation probes cover loop body, context entry and cleanup, plus CLI
  model/setup/cleanup cancellation followed by another cleanup failure. The
  first interruption propagates; no run or notice acknowledgment is created.
- Final Python validation: **9,749 passed, 43 opt-in skips and two existing
  serializer warnings**, in **327.71 seconds**. Locked sync, Ruff formatting
  and lint, strict Pyright, licenses and generated protocol checks pass.
- The unchanged GUI passed **445 web tests**, **24 post-build math tests**,
  locked npm installation with scripts disabled, lock/installed license checks,
  Svelte/TypeScript checks and both production builds.

Two earlier Python runs were deliberately interrupted for the cancellation
corrections (exit 143 and 130). Their separate logs are retained as incomplete
evidence. They are not counted as completed gates. The web/helper/assets stayed
unchanged; fresh PR CI remains required before merge.

## CLI and browser evidence

The marked fixture starts synthetic loopback HTTP peers and the actual CLI
subprocess. Its first model reply requests a file read; the second receives a
hostile synthetic HTTP 429 error body. The command exits 1 and retains four
messages, the completed tool record, `stopped="error"`, `failure="model"` and
`final_text=null`. The explicit transcript and saved run are identical. Exact
synthetic key values and the hostile provider body are absent from the saved
record and command output. This establishes the error path without contacting
a real model or executing research code.

Browser acceptance used signed freeze `50e20b1` before the Python-only
cancellation corrections. The GUI/server/helper source and all 67 built assets
are identical to the final source. The workspace visibly showed **Failed run
(model)**, the partial-transcript notice, the completed `read_file` content
`6 * 7 = 42`, and zero Answer headings. Console and server warning/error
records were empty. A final-source CLI run separately reconfirmed the ordinary
failure path. [Screenshot](failed-run-2026-10-12.jpg).

## Archive and teardown

[Evidence archive](failed-run-evidence-2026-10-12.json): **107,728 bytes**, SHA-256
`c1d9e2b87f5f87eb53aad2a350ec4162c7aa2fecd2d0ee7e6b771f790e4044c3`.
It retains 22 exact UTF-8 records with byte lengths and SHA-256 values, including
the CLI transcript, browser checks, source/asset pins, complete final gates,
separate incomplete predecessor logs and cleanup. The screenshot is hashed
separately. Disposable login values are absent; raw service journals, key files
and databases are excluded.

Browser teardown was verified at **17:39:04.081374 UTC**: tab 11 closed,
`sandbox-failed-run-browser.service` inactive, port 45581 free, both marked
fixtures and private login/port files removed. Final-source CLI fixture removal was verified at 17:47:42.873911 UTC.
The helper also reaps its owned CLI child on timeout before closing its peers.

## Limits

This completes the narrow W4 ordinary failed-run item. Setup/isolation refusal
before the loop, cancellation/process-death persistence, an unwritable state
directory, live streaming and automatic session-end handling remain separate.
Failure metadata contains only a fixed phase; normal observed transcript
content is retained as before. The synthetic gateway's HTTP 429 tests retention,
not a product retry policy. The user's five-minute capacity retry preference
governs agent execution and adds no product model retry behavior.
