<!-- SPDX-License-Identifier: MIT -->
# Babbage lab: the Claude Messages API adapter with a real Claude model (2026-10-31)

Run on babbage on 2026-10-31 from a fixed detached worktree of `k8s-sync` at `b0ed39ee`. Lab script changes are on the local branch `labs/messages-api-2026-10-31`. The owner named the gateway alias `arr-claude-opus-5.5` for this lane (Claude Opus 5.5). No other Claude alias was called.

**Outcome: BLOCKED at the gateway.** The alias sends Anthropic a model ID that does not exist, so every call answers 404 before any model runs. Nothing in the adapter could be qualified live. **Claude spend: USD 0.00.** Two 404 calls were made, with a reported cost of 0 and nothing billed.

## Results

| # | Check | Result | Evidence (one line) |
| --- | --- | --- | --- |
| 1a | Find the alias | PASS | `GET /model/info` lists `arr-claude-opus-5.5`: provider `anthropic`, `litellm_params.model` = `claude-opus-5.5`. The gateway is LiteLLM 1.102.1. |
| 1b | `/v1/messages` through the gateway, minimal request | **FAIL (gateway configuration)** | HTTP 404 `not_found_error`: "model: claude-opus-5.5 was not found. Did you mean claude-opus-5-5?" (Anthropic, relayed by LiteLLM). `x-litellm-response-cost: 0`. |
| 2a | CM-F31, Claude's part (`test_messages_live.py`) | BLOCKED | The same 404 from `MessagesModel.complete` (`ModelError: the model gateway returned 404 …`). |
| 2b | CM-F30 (thinking replay) | BLOCKED, not run | Its first call is the same request, and no gateway image digest was given. No `[model.thinking_replay]` setting was produced, and replay stays off. |
| 3 | `model_lab.py` on `messages`: B12 review, write-area refusal, soft-cap check-in, MCP on the direct path, usage and cost, progress notes, `purpose_note` | BLOCKED, not run | Every model call would 404. The lab is ready to run once the alias is fixed (runbook below). |
| 4 | The same scenario on `chat` | BLOCKED, not run | Same alias and same provider model, so the same 404. |

## Findings

1. **Gateway: the alias names a model ID that does not exist (blocks this lane).** `arr-claude-opus-5.5` has `litellm_params.model: claude-opus-5.5`, but Anthropic's ID is `claude-opus-5-5`, with hyphens. Fix: set the deployment's model to `claude-opus-5-5` (or `anthropic/claude-opus-5-5`). This is the owner's gateway, so the lab did not change it.

2. **Gateway: the alias has no prices.** Its `model_info` has `key: claude-opus-5`, `input_cost_per_token: 0`, `output_cost_per_token: 0`, and null cache prices. LiteLLM reports a cost of 0 for each call. Fix: set Opus 5.5's prices, $4 / $20 per MTok input/output, $0.20 per MTok cache read, $5 per MTok for a 5-minute cache write. Per token: `4e-6`, `2e-5`, `2e-7`, `5e-6`. The other `arr-claude-*` aliases are priced.

3. **Product defect: a zero price in the model information counts as a real price, so Claude calls would be recorded as free.**
   - **Where:** `packages/orchestrator/src/sandbox_science/orchestrator/pricing.py`. `_price` accepts `0`, and `prices_of` returns `Prices(input=0.0, output=0.0, …)` for this alias. `GatewayModel._priced` (`model.py:466`) already treats a gateway cost of 0 on a call that used tokens as "no cost" (lab 43, B2), and falls back to these prices.
   - **Symptom (computed offline from the live `/model/info`):** a call with 10,000 input and 1,000 output tokens and no cache write costs `0.0`, source `model_info`. The Usage tab would show USD 0.00, and the project's daily cost limit would never trip. A call that writes the cache comes out unpriced, because the cache-write price is null. So the same alias would show a mix of "free" and "unpriced" calls.
   - **Suggested fix:** in `prices_of`, treat a deployment whose input and output prices are both 0 as unpriced (return `None`), as B2 does for the cost header. This makes such calls count as `unpriced`, which the Usage view and the unpriced warning already handle.

4. **Adapter observation: what the adapter enables depends on how the gateway spells the model ID.**
   - **Where:** `messages.py`, `_UPDATES` and `_TOOL_CHANGES`, matched against `litellm_params.model`.
   - **Behaviour:** `claude-opus-5.5` matches neither, so for that spelling the adapter would ask for no progress notes and would not use deferred mid-run tools. It would not report this anywhere. `claude-opus-5-5` and `anthropic/claude-opus-5-5` match both.
   - **Impact here:** none, because Anthropic refuses that ID anyway. But the adapter infers features from an operator-typed string. When the string matches none of the known models, it says only that the gateway "does not say which model" (if it names none).
   - **Suggested fix (not urgent):** log the provider model and the features chosen for it once per alias (updates, tool changes, adaptive). Optionally, prefer the gateway's `model_info` flags (`supports_adaptive_thinking`, `supports_mid_conversation_system`) when present.

5. **Lab script (minor):** `model_lab.py abort` on a root that was prepared but never brought up fails with "No active gateway" and keeps the root. The root was removed by hand here; no unit or process existed. Suggested fix: when `gateway.toml` is absent, `abort` should skip the OpenShell sandbox deletion.

## Lab script change

`model_lab.py prepare` takes `--model ALIAS` and `--api chat|responses|messages`. The defaults are unchanged: `sandbox-orchestrator` on `responses`. The chosen alias and API are written into the lab's private launcher config for every role. This was checked: `prepare --model arr-claude-opus-5.5 --api messages` produced a config that `load_config` reads as `api = {"arr-claude-opus-5.5" = "messages"}`. The prepared root was then removed.

## Runbook once the alias is fixed (budget USD 40)

```bash
# 1. Probe: one /v1/messages call with max_tokens 256; check usage and x-litellm-response-cost > 0.
# 2. CM-F31 and CM-F30 (SS_LIVE_GATEWAY, SS_LIVE_GATEWAY_KEY_FILE by path, SS_LIVE_CLAUDE=arr-claude-opus-5.5,
#    SS_LIVE_GATEWAY_IMAGE=<the gateway image digest as deployed>):
.venv/bin/python -m pytest -s packages/orchestrator/tests/test_messages_live.py
# 3. Real-model lab on the Messages API:
.venv/bin/python -I .agents/labs/model_lab.py prepare --model arr-claude-opus-5.5 --api messages
.venv/bin/python -I .agents/labs/model_lab.py up "$LAB"
.venv/bin/python -I .agents/labs/model_lab.py send "$LAB" review review          # B12 + A6 write-area refusal
.venv/bin/python -I .agents/labs/model_lab.py delegation "$LAB" -- --task-turns 6
.venv/bin/python -I .agents/labs/model_lab.py send "$LAB" checkin checkin-child  # soft-cap check-in
.venv/bin/python -I .agents/labs/model_lab.py send "$LAB" connectors connectors3 # MCP, direct path
.venv/bin/python -I .agents/labs/model_lab.py capture "$LAB" <session> <name>     # usage/view, tools (purpose_note)
.venv/bin/python -I .agents/labs/model_lab.py cleanup "$LAB"
# 4. Repeat a short stage with --api chat for comparison.
```

Before this runs, check that each call's cost source is `gateway` and the cost is above 0. If the gateway's prices are still missing, finding 3 makes every call look free.

## Recommendation on `messages` as the default for Claude aliases

**Not yet.** There is no live evidence for the adapter: CM-F31, CM-F30 and a whole-product run on `/v1/messages` are all still open. To make it the default:

1. Fix the alias's provider model and prices (findings 1 and 2).
2. Pass CM-F31, and run the lab above with no adapter defect. Replay stays off until a LiteLLM release passes CM-F30.
3. Fix finding 3, so that a Claude alias without prices is never billed as free.
4. Change the launcher's default `api` from `chat` to "`messages` when the alias's provider is `anthropic` per `/model/info`, else `chat`", and do the same in the Kubernetes bundle's model profiles (`kubernetes_models.py`). An explicit `[model] api` entry should still win.
5. Run context management lab 44's round 2 (the Claude arms) on that default.
