<!-- SPDX-License-Identifier: MIT -->
# OpenShell Lab43: meaning search, the GPT and Claude adapters, and history across runs

Run on babbage on 2026-10-16 (UTC), from 08:36 to 09:24, against the OpenShell 0.1.3 lab that `.agents/labs/lab_up.sh` brings up, with the images `sandbox-science/minimal:02f85b3`, `sandbox-science/crypto-base:02f85b3` and `sandbox-science/mcp-proxy:e5e55c86` (no image input changed since lab 41; nothing was built). Everything ran from main `4e4709d0`. A short lab for live evidence of what merged since [lab 42](babbage-openshell-lab-42-2026-10-16.md): meaning search (#446), the GPT Responses adapter (#452), the Claude Messages adapter (#455), the session view (#454), the history tools (#443), and the workspace's Skills page (#468) and research-state views (#463). The state block's digest (#465) merged at 08:43, during the lab, so the instance, started from `4e4709d0`, did not have it. Teardown was complete.

## Setup

- The config was written by `.agents/labs/launcher_config.py` with `--project lab43 --profile crypto-base --session main --session claude`, `--mcp-proxy`, `--provider-env-file` (the owner's Cloudflare Access file, by its path alone) and the new skills options (#447): `--skills-pack crypto=https://github.com/regenscheid/crypto-analysis-skills`, `--skills-embedding-model arr-text-embedding-3-small` and `--skills-metadata-model sandbox-orchestrator`. Nothing had to be added by hand for the skills.
- The lab then set `[model] api = { "sandbox-orchestrator" = "responses", "arr-claude-fable-5" = "messages" }` and dropped the reviewer, to keep tokens down. Thinking replay stayed off (no `[model.thinking_replay]`).
- The gateway's aliases were listed through the harness's own code (`ModelGateway.aliases()` with its `read_secret`, given the key file's path; the key was never printed). It serves `arr-claude-fable-5`, `arr-claude-opus-5`, `arr-claude-sonnet-5`, `arr-claude-opus-4-8` and `arr-claude-sonnet-4-6`, but no Opus 5.5. Fable 5 is one of the models the adapter asks `display: "updates"` for, so the lab used it.
- Session `main` ran on `sandbox-orchestrator` through `/v1/responses`. Then the instance was restarted with `[model] name = "arr-claude-fable-5"` for session `claude`, through `/v1/messages`.
- `test_messages_live.py` (CM-F30 and CM-F31's Claude part) ran with `SS_LIVE_GATEWAY`, `SS_LIVE_GATEWAY_KEY_FILE` (the path) and `SS_LIVE_CLAUDE=arr-claude-fable-5`, with its pytest basetemp outside `/tmp`. Small probes through the adapters' own classes filled the gaps.

## Results

| Step | Result | Evidence |
| --- | --- | --- |
| 1a. Vectors stored at import | Pass | "skills: crypto imported at 4089e99415f3, 162 skills; the pass found 4 topics, 29 items dropped", with `embeddings` "computed": 162 in 1,536 dimensions, $0.0011. `skill_vectors` holds 162 rows. The built-in pack's vectors were made in the background by the first run |
| 1b. Search and discovery by words and meaning | Pass | Search records `scoring: hybrid` and answers `"ranked": "by words and meaning"`, with a `meaning` score (0.53) beside the words. Run 1's discovery was `partial` (the built-ins' vectors pending), later ones `hybrid`. The notice's framing reads "matched this message by its words or its meaning" |
| 1c. A query only meaning can match | Fail (B1) | "how long would a big computer cluster need to find two inputs that collide", searched word for word, gave word matches only (`differential-family-analysis` on "cluster", "find", "input"). Its best cosines, measured with the stored vectors, were 0.360 (`rebound-analysis`) and 0.358 (`generic-group-and-collision-search-analysis`), under the 0.45 cutoff |
| 2a. CM-F31, GPT's part | Not run: no live test exists | `test_*_live.py` holds only Claude's part. The checks below stand in for it |
| 2b. Encrypted reasoning comes back and is replayed | Pass through the adapter; not seen in real runs | Raw `/responses` calls returned `reasoning` items with `encrypted_content` (429 to 1,549 reasoning tokens). Through `ResponsesModel`, a second call's input counted the first call's `rs_…` item, and its input rose from 46 to 645 tokens. But in the 15 calls of the real runs, and 8 of a tool-loop probe, the model never reasoned before a tool call: 0 reasoning tokens. So replay inside a real run was not exercised |
| 2c. Usage and cost recorded | Usage pass, cost fail (B2) | Every call's usage is recorded and normalized (`input_tokens` 18,464 …). Every cost is `null`: LiteLLM 1.102.1 sends no `x-litellm-response-cost` on `/responses`, and its `x-litellm-response-cost-original` reads `0.0`. The project page after four GPT runs (380,200 tokens): "$0.0037 in all", with no main agent line |
| 2d. Nothing breaks across runs | Pass, with B3 | Runs 1, 2 and 4 answered, each rebuilt from the session view. Run 3 failed: "the model's response failed: Our servers are currently overloaded", a failed response the adapter does not retry |
| 3a. CM-F31, Claude's part | Pass | `{"model": "arr-claude-fable-5", "gateway": "1.102.1", "input": 424, "progress_notes": [], …}`; input normalized as CM-28 says |
| 3b. `anthropic-beta` and `display: "updates"` | Forwarded | A raw `display: "updates"` call with the beta header got 200. The same call without the header got 400: "thinking.adaptive.display: Input should be 'summarized', 'omitted'". `summarized` returned 644 characters of thinking. No fallback was needed. But the adapter would not recognize that refusal if a gateway dropped the header (B4) |
| 3c. Progress notes archived, searchable, not in answers | Partly: no notes came | Fable 5 wrote its sentence before each tool call as a text block ("I'll compute 2^127 − 1 mod 1000003 in Python."); its thinking blocks came back empty in every call with tools. That text is archived, and `history_search` found it in the next run. Final answers hold only the answer |
| 3d. CM-F30 fails or is refused on 1.102.1 | Fails, before replay | "the first answer has no thinking to replay": adaptive thinking chose none on the calculator task. Replay stayed off in every call: "off: no thinking replay setting" |
| 3e. A short real session on Claude | Pass | Session `claude`, two runs: two Python calls and an answer, then `history_search` and `history_read`. Costs were recorded: $0.5665 and $0.2582 |
| 4a. History tools across runs | Pass | GPT run 3 called `history_search` ("twist_order =") and `history_read` on run 1's Sage result. Run 4, after run 3's gateway failure, quoted the 78-digit twist order exactly with its message ID, `msg-a81580636688e93b`. Its view held run 3's read, so it called no history tool itself. Claude run B found `333986` and its own earlier sentence the same way |
| 4b. State block and digest | Not exercised | No view reached half its input, so none folded and no state block was made. #465, the digest, was not in the instance. The ledger held C-1 |
| 5a. Skills page (#468) | Pass, with B5 | It lists the 169 skills a run is given with revisions and descriptions, the settings form, overrides, both pins and the import ("Derived by sandbox-orchestrator"). The Topic column is empty for every skill, and no entry skill is marked, though the runs' index has 4 topics and 6 entry skills |
| 5b. Research-state views (#463) | Pass | C-1's page shows "Research state: Basis interpreted: the agent's own reading; Kind observation; Statement …; Evidence Supports, exact computation: artifact … r1" and its gate |

## Tokens and cost

| Run | What it did | Alias, API | Calls | Input | Output | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Sage factorization, C-1, a file | `sandbox-orchestrator`, responses | 7 | 152,643 | 1,092 | none recorded |
| 2 | meaning search, embedding degree | same | 4 | 105,169 | 303 | none recorded |
| 3 | `history_search`, `history_read`; failed upstream | same | 2 | 58,845 | 60 | none recorded |
| 4 | the exact twist order, search verbatim | same | 2 | 61,932 | 156 | none recorded |
| A | two Python calls | `arr-claude-fable-5`, messages | 3 | 111,416 | 477 | $0.5665 |
| B | `history_search`, `history_read` | same | 3 | 125,077 | 694 | $0.2582 |

MAIN used 617,864 tokens in 6 runs. `sandbox-orchestrator` used 380,200, unpriced by the gateway (B2). `arr-claude-fable-5` used 237,664 for $0.8247, $0.46 of it the first call's cache write. Embeddings cost $0.0012 (14 calls, 177 texts) and the import's pass $0.0025. There was no reviewer and no watcher call. The project page: "$0.83 in all … The main agent: $0.82". The direct probes and the two live tests, about 25 gateway calls, are not in these figures.

## Model errors, and whether the platform caught them

| Error | Caught |
| --- | --- |
| The import's pass wrote 23 short descriptions for skills whose own fit, placed 4 skills twice or wrongly, and bound 2 start-up variables | Yes: 29 items dropped with reasons |
| Run 1 passed `ledger_change` unknown fields (`anchors`, `basis`) | Yes: refused; the retry made C-1 |
| Run 2 padded the user's meaning-only search with its own keywords | No (the lab searched again verbatim) |
| Claude run A wrote `pow(2, 127, 1000003) - 1 % 1000003` | No; harmless here, as the precedence gives the same value |

## Bugs

- **B1, medium: the meaning cutoff is above what paraphrases score.** With `arr-text-embedding-3-small`, a query that shares no words with the right skill scores 0.36 at best, against the 0.45 cutoff. Search and discovery then rank by words alone while saying "by words and meaning". The right skill was second by cosine. Per-pack thresholds (SK-34) are not built. Reproduction: the verbatim query above.
- **B2, high: GPT calls through `/responses` have no cost.** LiteLLM 1.102.1 sends no `x-litellm-response-cost` on that route and prices the alias at `0.0` (`x-litellm-response-cost-original`); `usage.cost` is `null`. Every main call's cost is `null`, the project page shows no main agent cost, and a daily cost limit cannot stop these runs. The fix may be in the gateway's pricing for the alias on that route, or the platform could refuse to count an unpriced call as free.
- **B3, medium: a failed response is not retried.** A 200 whose `status` is `failed` with "Our servers are currently overloaded" raises a plain `ModelError` in `responses.py`'s `parse_response`, so run 3 stopped with `error` after two calls. The chat adapter retries an overloaded gateway.
- **B4, medium: the Claude adapter would not see a dropped beta header.** It falls back to `summarized` when a refusal names `thinking.display`, the beta or `anthropic-beta` (`_UNDISPLAYED`). Anthropic's real refusal says "thinking.adaptive.display: Input should be 'summarized', 'omitted'", which holds none of them, so the run would fail rather than fall back. Today's gateway forwards the header, so this did not happen in a run.
- **B5, low: the Skills page shows no topics or entry skills.** The workspace calls `SkillStore.effective(project)` without the metadata alias, and the launcher gives the workspace none, so every Topic cell is empty.
- **B6, low: without a reviewer, every safe point records a failed review.** Eleven reviews reading "The reviewer model alias is unavailable; this window was not reviewed." `reviewer` is documented as optional, "none by default".

## Observations, not fixed

- **The gateway reports aliases as model names** for embeddings, GPT and Claude (`"model": "arr-claude-fable-5"`). The embedding space is then keyed by the alias, as SKILLS.md warns.
- **GPT through `/responses` reasons only when no tool fits.** A request with instructions and a fitting tool gets 0 reasoning tokens and an immediate call; without the tool, the same prompt gets about 600.
- **Fable 5's first call cost $0.46**: 36,780 input tokens written to the cache. Later calls cost about $0.05 to $0.10.
- **A failed run's messages stay in the next run's view**, which is how run 4 had run 3's `history_read` result (CM-29a).

## Whether what merged works

Meaning search now works: vectors are stored and scored. But its cutoff keeps paraphrases out (B1). Both adapters work through LiteLLM 1.102.1 across runs. The Claude adapter's beta header and `display: "updates"` pass through, its usage and cost are recorded, and its replay stays off. The GPT adapter's encrypted reasoning comes back and is sent back. But its calls are unpriced (B2), and a transient failure ends the run (B3). The history tools and the session view return exact values from earlier runs on both models. The Skills page and the research-state view show what they should, except topics (B5).

## Evidence and teardown

The evidence is kept outside git, in an archive on babbage (sha256 `26f94b0c3827ef37f5a7b344e00dc49d71e1a332cf02888a0f828790361ee14b`, 66 entries). It holds the config as written and as run, the helper scripts and probes, the alias listing, the live tests' output, the run transcripts, the token report, the proxy's records, the logs, the GUI's texts and a copy of the control store. It holds no tokens, no login address, nothing from the owner's secrets directory and nothing of the gateway's database.

Teardown ran `down`, which suspended the research sandbox and deleted the proxy's; the launcher unit then stopped, with its forward. The research sandbox was deleted, and `lab_down.sh` stopped the lab units and removed the lab directory (14 GB), with the gateway's database, key and registered provider. The launcher's state and config directories were removed. Afterwards no lab unit, port, process, OpenShell path or driver socket was left, and no image was added.
