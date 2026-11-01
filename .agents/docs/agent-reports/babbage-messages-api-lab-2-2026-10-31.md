<!-- SPDX-License-Identifier: MIT -->
# Babbage lab: the Claude Messages API adapter with Claude Opus 5.5, round 2 (2026-10-31)

Run on babbage on 2026-10-31/11-01 (UTC 00:40 to 01:40) from a fixed detached worktree of `k8s-sync` at `0fc24841`. That build includes the zero-price fix and the Messages adapter's feature log. Lab script changes are on the local branch `labs/messages-api-2-2026-10-31`.

**Setup:**
- The orchestrator image `sandbox-science/orchestrator:4be39301` was built locally for this run and not pushed (digest `sha256:bb64f645…`). `minimal:48d206ba` was already present.
- The workspace UI was built from the same tree.
- Gateway alias: `arr-claude-opus-5.5`, after the owner's fix. Its provider model is now `claude-opus-5-5`, priced at $4 / $20 / $0.20 / $5 per MTok (input / output / cache read / cache write).
- No other Claude alias was called.

The first round of this lab ([report](babbage-messages-api-lab-2026-10-31.md)) was blocked by the alias's model ID.

**Claude spend: about USD 7.2** reported by the gateway, against the lane's USD 40:
- the Messages lab (`model_lab.py`, project Usage page): USD 4.05, 100 calls;
- the `chat` comparison lab: USD 3.08, 35 calls;
- direct probes and the live tests: under USD 0.10.

The project's local daily limit was USD 15 in each lab. Nothing is left running.

## Results

| # | Check | Result | Evidence (one line) |
| --- | --- | --- | --- |
| 1 | `/v1/messages` through the gateway, minimal request | PASS | 200, `end_turn`, the answer "pong". `x-litellm-model-name: claude-opus-5-5`, cost 0.000152. The response's `model` is the alias, not the provider's ID. |
| 2a | CM-F31, Claude's part | PASS | `{"model": "arr-claude-opus-5.5", "gateway": "1.102.1", "input": 422, "progress_notes": [], "text": null}`. The input is normalized as CM-28 says. No progress notes on the calculator task. |
| 2b | CM-F30 (report only) | FAIL before replay (inconclusive) | "the first answer has no thinking to replay": at the default effort the first answer had no thinking blocks, as in lab 43. No `[model.thinking_replay]` setting was printed or applied, and replay stays off. |
| 3a | Adapter feature log | PASS | `Messages adapter for arr-claude-opus-5.5: gateway model claude-opus-5-5; progress notes (display updates) on (on the CM-38 list); mid-run tool changes on (on the CM-38 list); adaptive thinking on`. This was logged once, at the runtime's first call. |
| 3b | B12 parent review | PASS | A accepted (`parent`, note "A is fine"). Feedback reopened B: result 2 edited `work/<B>/notes/beta.md`, then B was accepted. C was left alone and accepted at run end (`parent_ended`). |
| 3c | Write outside the write area, refused and recovered | PASS | `write_file results/outside.md` was refused (`policy_denied`). The child's next call wrote `work/<dlg>/results/outside.md`, and the parent accepted it. Opus 5.5 otherwise always wrote inside its area unprompted, B's `notes/beta.md` included, so the refusal took an explicit brief (stage `area`). |
| 3d | Soft-cap check-in | PASS | With `--task-turns 6`, a sequential 12-cell child checked in at "6 of 6" (`resource-4aefee…`). The parent answered `extend` with `resource_decide` (total 306), and the child ran cells 7 to 12 in the same run, with no new attempt and 13 calls in one run (the turn-ceiling fix). The first, unsequenced variant ran 12 cells as parallel calls in 2 turns and never reached the cap. |
| 3e | MCP connector calls on the direct path | PASS | Main: `mcp_tools` loaded `e-print-mcp_corpus_stats` and `nist-csrc-mcp_nist_search`, then called both. The child loaded and called `nist_search`. No errors. The fence note said "admitted by the checked-in classification, without warnings". |
| 3f | Usage and cost | PASS | All 57 main and delegated calls have `cost_source: gateway`. The cost recomputed from the tokens at the alias's prices matches each recorded cost exactly (max difference 0). `input_tokens` = fresh + cache read + cache write for every call. Unpriced: 0. The session and project Usage pages show the costs by role, reviewer included. They show totals only: cache reads and writes are not broken out in the UI (screenshot). |
| 3g | Progress notes rendered | NOT OBSERVED | `display: "updates"` and its beta reach Anthropic: without the beta header the gateway answers 400 "thinking.adaptive.display: Input should be 'summarized', 'omitted'", and with it 200, with no fallback warning in the runtime. But in 57 lab responses and about 10 direct ones, every thinking block had empty text. Opus 5.5's commentary between tool calls came back as ordinary `text` blocks before the `tool_use` ("First I'll add 48213 and 77409."), which the adapter keeps as the answer's text. So there was no note to render. |
| 3h | Tool calls with `purpose_note` | PASS | Every one of about 60 tool calls carried a specific purpose, kept as the record's `purpose`, for example "Iteration step 7: x from 723" and "Attempting the requested write at results/outside.md outside my write area". |
| 3i | Prompt caching and mid-run tools | PASS, with one partial miss | The 5-minute breakpoints hit on every call: each call reads the previous call's whole input but 2 to 4 tokens. The exception is the call right after `mcp_tools` loads tools: 39,299 of the previous 45,063 tokens were read and 5,764 rewritten, and after that hits were full again. |
| 4 | Same short scenario on `chat` | PASS (functional), costlier | B12 and MCP behaved the same, with the same `purpose` notes. On `chat`, the gateway itself caches with a 1-hour TTL (`ephemeral_1h_input_tokens`), billed at $8/MTok. Loading tools mid-run rewrote the whole cache: main call 2 read 0 and wrote 46,387 tokens for $0.37, against $0.047 on `messages`. The same two stages cost $3.08 on `chat` and about $1.96 on `messages`. |

## Defects and observations

No adapter defect was found. There were no malformed or refused requests, no thinking or caching errors, and no usage mismatches. The runtime logged no warnings beyond two `kernel_check` "the kernel observation failed; no kernel notice", which are the busy-lane case the post-tool hook already tolerates.

1. **Progress notes did not appear on Opus 5.5 (spec, CM-38a / CM-F31).** CM-38a expects Opus 5.5's between-call text as progress-update thinking blocks under `display: "updates"`. Through this gateway the model wrote that text as `text` blocks instead, and returned empty thinking blocks. The adapter handles this (the text is kept and shown as the answer's text), so nothing is lost. But the notes path (`notes`, `[progress note]` sent back, archive indexing as notes) is still not exercised live.
   - **Suggested next step:** re-run CM-F31 with a longer, multi-step agentic prompt at `effort: high`.
   - **Possible extension:** have CM-F31 report the text blocks that come before tool calls, so the test says which form the commentary took.

2. **The adapter sends no `output_config.effort`.** Opus 5.5 therefore runs at its default, `medium`. This is not a defect, but it is a choice the deployment does not see. Suggested: make effort a per-alias or per-role setting. Main and reviewer at `high` would follow Anthropic's guidance for agentic work. It may also make thinking, and so CM-F30, testable.

3. **One partial cache miss when tools are loaded mid-run (messages).** About 5.8k tokens are rewritten once per load, about $0.03 here. The deferred-tool path keeps the system and tools prefix cached, so this is much better than `chat` (finding 4). Not investigated further: it may be the API re-rendering the first user turn when the first `defer_loading` tool appears.

4. **On `chat`, the gateway injects 1-hour cache breakpoints itself.**
   - **Evidence:** `ephemeral_1h_input_tokens` on a plain `/chat/completions` call; the cache write is billed at about $8/MTok.
   - **Effects:** every tool-list change rewrites the whole prefix, and the gateway's model information lists only the 5-minute write price ($5/MTok). A call priced from model information (when the gateway reports no cost) would therefore be understated on `chat`. In this lab every call carried the gateway's cost.
   - **Fix:** this is the owner's gateway setting, not product code.

5. **Lab script: the project's environment sandbox was unrecorded at cleanup.** The worker creates the project's environment sandbox after `up`, so `owned-sandboxes.json` did not list it. `cleanup` then stopped at "unrecorded sandbox", after it had already stopped the gateway, middleware and cache. Recovery needed those three units restarted by hand before `cleanup` passed.
   - **Fixed on the branch:** `ModelLab.cleanup` records the lab gateway's sandboxes first, since they are all the lab's. The chat lab's cleanup then passed on the first try.

## Lab script changes (`model_lab.py`)

- **New stages:**
  - `mcp`: main and child connector calls on the direct path;
  - `area`: a child's write outside its area, refused and then recovered;
  - `checkin-seq`: a child whose cells must run one per turn, so it reaches its soft turn cap.
- **Receipts** now keep each tool call's arguments (the journal stores them as JSON text, which the receipt used to drop) and its `purpose`.
- **`cleanup`** records sandboxes made after `up` (finding 5).

## Recommendation: `messages` as the default for Claude aliases

**Yes, with two conditions.** With Opus 5.5, everything the platform does ran correctly on `messages`: delegation and review, write areas, check-ins, MCP on the direct path, purpose notes, usage, and cost. Its own caching is cheaper and survives mid-run tool loading, unlike the gateway-injected caching on `chat`. To make it the default:

1. **Change the launcher's default.** The `api` default should become "`messages` when the gateway's `/model/info` gives the alias the provider `anthropic`, else `chat`", with an explicit `[model] api` entry still winning. Make the same change in the Kubernetes bundle's model profiles (`kubernetes_models.py`). Log the API chosen, beside the adapter's feature line.
2. **Keep replay off.** Thinking replay stays off until a LiteLLM release passes CM-F30. That does not block the default, since replay is off by default and every call here ran with it off.

Before relying on the progress notes (CM-38a), re-run CM-F31 at higher effort (finding 1). Consider an effort setting per alias or role (finding 2). Context-management lab 44's round 2 (the Claude arms) can then run on the default.

## Evidence

All of it is outside git. Each directory has a `SHA256.json`; the SHA-256 of that manifest file is given here.

| Lab | Directory | Manifest SHA-256 |
| --- | --- | --- |
| Messages API (`--api messages`) | `/var/tmp/ss-modellab-evidence-723bbc47/` | `7506260b61aea29f42bf5efd171a31ad6fe3732a4708efeda89528eb889f8c6c` |
| Chat comparison (`--api chat`) | `/var/tmp/ss-modellab-evidence-f1dec9d0/` | `080a3ad6c79d41e13fa7132af78ab9499c8e83331698214ebc07f426c33a0f23` |

`screenshots/session-usage-checkin.jpg` (`1f9582ea…`), added after export, shows the `checkin` session's Usage tab: $1.70 over 47 calls by role, with the local USD 15 limit.

Both labs' teardowns deleted their sandboxes, stopped their units, checked ports, processes and driver directories, and removed their roots. The first Messages lab cleanup needed the manual restart described in finding 5. The personal instance and the cluster were not touched. The model key and the Cloudflare Access file were passed by path only.
