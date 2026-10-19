# Cache layout experiment, 2026-10-16

Offline experiment through the production Responses and Messages adapters. A temporary loopback fixture captured 24 requests. It was stopped when the script returned. No provider or personal service was contacted.

The fixture has 80 alternating history messages, about 359 KB of rendered history, a state block, and appended arithmetic work. It compares consecutive requests: initial call, ordinary append, later-run start and first call after replacing the oldest 40 messages with a fold. State retains user-message authority.

| Adapter | Layout | Later-run shared history bytes | After-fold shared history bytes |
| --- | --- | ---: | ---: |
| ResponsesModel | state_first | 49 | 128 |
| ResponsesModel | state_after_history | 358,777 | 27 |
| ResponsesModel | unchanged_snapshot | 358,999 | 130 |
| MessagesModel | state_first | 55 | 107 |
| MessagesModel | state_after_history | 359,388 | 10 |
| MessagesModel | unchanged_snapshot | 359,700 | 109 |

Moving changed state after the stable history preserves a much longer serialized prefix at a later-run start. Reusing an unchanged state snapshot preserves almost all of the ordinary appended history, but is valid only when both content and identity are unchanged. A fold that replaces early history breaks the long prefix in all three layouts. The first-ever call has no preceding prefix to compare. Ordinary appends preserve more than 358,000 bytes in both adapters for every layout.

These are serialized history-array byte comparisons, not token counts or observed cache hits. Cache-control annotations remain in the comparison; the Messages adapter also merges adjacent user messages, so its item boundaries differ. Provider cache reads/writes, latency and cost are unavailable. The fixture does not establish semantic continuity under a real model.

Production layout remains unchanged. Next acceptance requires identical live inputs and continuity probes under each layout, reported separately for later-run starts and after-fold calls. A session cache key alone is not evidence of a hit.

Reproduce with `uv run python .agents/labs/cache_layout.py --output <outside-repository-file>`.

Evidence: `/home/andy/.cache/pt/research-foundations-cache-layout.json`; SHA-256 `4a7a690f9952339a813f776b1819599982db4c3fb4c04a1b5eef03aca1776cf6`.
