# Context-management lab, round 2: the continuity probes on Claude

Round 1 ([its runbook](cm-lab-round-1.md), [lab 44's report](../docs/agent-reports/babbage-openshell-lab-44-2026-10-16.md)) ran the probes CM-P1 to CM-P9 on GPT models. The spec asks for both families: round 2 runs them on Claude, from round 1's seed, and adds a delegated child that folds within its own run (CM-37). This file is the kit's runbook for round 2; round 1's describes the probes, the seed and the scorer's rules, which hold here unless this file says otherwise.

**The owner's conditions (2026-11-02):** USD 300 for the whole lab, everything included (the fact-finding pass, the live checks below, every arm and its child); Claude alone, on `arr-claude-opus-4-8`. Opus 5.5 declines the scenario's cryptanalysis steps (S03, S06, L05, L12) as `cyber`; Opus 4.8 answers them.

## What changed in the kit

| Part | Round 2 |
| --- | --- |
| `cm_lab/export.py` | Reads the archive with a 1 GiB page limit (`READ_BYTES`): GPT's native payloads in round 1's seed pass the archive's 8 MiB working limit. Exports every run's record, the delegated ones with their role, parent and delegation |
| `cm_lab/sources.py` | `ROUND_1_P`, the p round 1's seed was recorded with, and `literals_for(sha256)`: the values a session is scored with follow the `data/params.json` its seed holds |
| `cm_lab/score.py` | Scores a session with its seed's values (the arm's `meta.params_sha256`, or the SHA-256 the seed's tools returned). The main probes read the main runs alone; L16's child is scored apart (`child`) |
| `cm_lab/scenario.py` | L16, the delegated reading task; the project's delegation settings (`DELEGATION`); Claude's arms; the round's USD 300 |
| `cm_lab/drive.py` | `count` (each snapshot as Claude counts it), `plan` (the plan offline), a `preflight` that accepts the Messages route for Claude's aliases, a cost model with cache writes and rebuilds, caps from that model, the round's budget in `round.json`, children's costs counted, the delegated model set to the arm's alias, and the delegation settings set on each arm's store. The compaction arm's steps run with `--context-mode provider_compaction`; its forecast counts compactions (each leaves the prompt and a summary of about 5,000 tokens), and its cost adds each compaction's call: the view at its trigger written at the 1-hour rate (conservatively: the live check's compactions wrote to the 5-minute cache and read the system message and tools from it), and 4,000 output tokens |
| `cm_lab/export.py`, `cm_lab/score.py` | An answer that holds a compaction block keeps its summary in the bundle (`compaction`); the scorer counts it as the compaction arm's fold, standing for everything before it, wherever the probes look for folds (CM-P9's calls after each, L16's `folded_after` and `no_folds`, what a run had in view) |

## The model

`arr-claude-opus-4-8`, as the gateway's model information gave it on 2026-11-02: an input window of 1,000,000 and 128,000 of output; USD 5 a million input, 25 output, 0.50 a cache read, 6.25 a 5-minute cache write (10 a 1-hour write); no long-context rates. `[model.api]` sends it through `messages` (CM-41).

**Its tokenizer.** Round 1's seed was recorded on `arrcodex-gpt-6-sol`. Counted with the gateway's `count_tokens` (free) as the harness's Claude adapter renders the seed's last calls, with the main run's 70 tool schemas (36,966 tokens, which the seed's transcripts do not keep), each snapshot is about 1.5 times larger, and the bulk steps' growth 1.43 times: S08 is 155,046 tokens (98,364 on GPT), S10 228,146 (149,820), S20 586,064 (399,508). The system message and tools alone are 39,960.

## The seed

Round 1's, unchanged: the same records, message IDs and workspace files, so every arm starts from what round 1's arms started from, read by another model (CM-31). Its `data/params.json` holds round 1's p (`sources.ROUND_1_P`), from before the synthetic p was made odd; the scorer uses that file's SHA-256 for these arms. At C = 250,000 the arms start from the last snapshot under 0.7 C as Claude counts it: **S08, at 155,046 tokens** (round 1's GPT arms started from S10).

The lab's directory (`$CM` below) holds `seed` and `blobs` as round 1 left them (links to round 1's kit directory, kept on babbage with its evidence, are enough: the round reads them and writes nothing there), and this round's own files: `counts.json` (`count`), `round.json` (`preflight`), and `arms/`.

## The arms

| Arm | Folds | Starts from | Forecast | Estimate (USD) | Cap | Why |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-250` | enriched | S08, 155,046 | 4 folds in 4 runs; the child 2 | 28.44 + child 6.53 | 55 | The probes, at C = 250,000 |
| `claude-250-skeleton` | `--skeleton-only-folds` | S08 | as above | as above | 55 | The control arm: whether enrichment helps |
| `claude-250-compaction` | Anthropic's server-side compaction (`--context-mode provider_compaction`, CM-38b) | S08 | 2 compactions in 2 runs; the child 1 | 31.13 + child 8.09 | 60 | The provider-native arm: whether Anthropic's compaction does as well as folds. Its trigger is the fold trigger, 0.75 C = 187,500; the state block is kept after each compaction, so the arms differ only in how older work is condensed |
| `claude-600` | enriched | S15, 407,487 | 2 folds | 76 | 115 | *Not run*: with the three 250,000 arms' caps and what was spent outside the kit, it would leave under USD 15 of the 300 for overruns or a rerun, and its forecast is at its validity bar (two folds; round 1's 600,000 arm made one) |
| round 1's arms | | | | | | Listed for round 1's bundles and scores; the driver refuses them |

A cap is 1.5 times the arm's estimate, rounded up to USD 5: round 1's steps took up to 1.4 times the calls planned. The three caps and the USD 4.29 spent before the round (the fact-finding pass and the live checks) add up to USD 174.29 of the 300; the compaction mode's live checks (below) spent USD 1.51 more outside the kit, so `preflight --elsewhere` takes 5.80. A compaction frees nearly the whole view where a fold frees a chunk, so the compaction arm's validity bar is two compactions in two runs, not four folds in three.

Every arm runs with the lab's small shares, as round 1's did: `--extra=--words-share --extra=0.01 --extra=--region-share --extra=0.02 --extra=--digest-share --extra=0.01` (round 1's runbook, "The lab's small shares").

## L16: the delegated child

The last live step asks the main run to delegate a long reading task to one task: pages 41 to 190 of Source D (150 pages of walk tables, which no run of the session read from S08), writing `shared/fast_walks.md`, one line per curve whose walk was under 0.87 sqrt(n), each marked within or beyond a comparison budget of 2^56 operations (rho costs about 2^(bits/2)). Before reading, the child runs `scripts/rho_experiment.py --p 17`. It reports page 115 with `message_parent`; the parent then sends it, with `message_child`, a change: the budget is 2^60, and the lines already written are to be revised. The child folds within its own run, at the arm's C, about twice.

| Probe | Planted | Tested | Scored by script |
| --- | --- | --- | --- |
| CM-P3 exact values | the child's first call: the experiment's seed and fingerprint | its result | Each quoted exactly and returned by the child's own tools; a near miss fails; a value left out is the owner's |
| CM-P2/P7 a change | the parent's message, after page 115 | its result and its notes | No sentence of its result states 2^56 as current; its result uses 2^60; in the notes as last written whole (`write_file`), no curve of 113 to 120 bits is marked beyond the budget. Notes written another way are the owner's. `folded_after` says whether a fold came after the message |
| CM-P8 a caveat | the brief: finite test, p = 17 only | its result | Every paragraph that states 1.38 keeps the qualifier |
| CM-P9 repeated work | each of the child's folds | the ten calls after it | The mean rise in repeated calls is at most one |

A child that never folded tests none of these: the report marks it `no_folds`. An L16 whose main run read the pages itself fails it. The project's delegation settings, set on each arm's store before its first run (`scenario.DELEGATION`): depth 1, a task's soft caps of 30,000,000 tokens and 400 turns, and a soft cost check-in at USD 40, well above the child's estimate, so nothing interrupts it. The child calls the arm's alias: the driver sets `--delegated-model` to it.

The arm's cap is checked before each step, as in round 1; the child runs within L16, so its cost counts once L16 ends (its record is kept under the arm's `runs/delegated/`).

## Checked before the round (2026-11-02)

- **Server-side compaction on Opus 4.8.** `arr-claude-opus-4-8` accepts Anthropic's compaction through the gateway (the `compact_20260112` edit, beta `compact-2026-01-12`). The trigger is at least 50,000 input tokens. At a 50,000 trigger, a request of about 89,000 tokens came back with a `compaction` block before its text; `usage.iterations` lists a `compaction` iteration (its input written to the 1-hour cache: 88,755 tokens) and the `message` iteration, while the top-level usage counts only the message's (267 input tokens). The gateway's reported cost included the compaction (USD 0.895). The next request, with the block passed back, was sent 302 tokens. A request whose messages begin with an assistant message holding a compaction block is accepted, and one that carries a compaction block without the beta is refused (400). The run service's `provider_compaction` context mode is built on these (CM-38b).
- **The compaction mode, live** (`--context-mode provider_compaction --compaction-trigger 50000`, the harness's `run` on an in-process local worker, `arr-claude-opus-4-8`, 2026-11-02; evidence kept on babbage). A first pass of three runs found that a request with tools can get a compaction whose summary is null (the model called a tool instead), which the adapter then took for the conversation's start, losing the run's task (the model recovered it with `history_search`); the adapter now sends its own summarization prompt (`sandbox-compaction-1`, telling the model to call no tool) and never sends a summaryless block (USD 1.19). The second pass: a run of about 52,000 tokens compacted at its first call (summary of 1,326 characters; the compaction's 30,117 tokens written to the 5-minute cache, since the requests carry the adapter's cache markers), called `history_search`, and answered from the summary with the code word on its second call, sent 39,779 tokens; the next run started at the compaction (40,281 tokens) and recalled the code word. The record kept the compaction and its iterations; the gateway's costs: USD 0.29 and 0.02. The checks spent USD 1.51 in all.
- **The scenario's steps on Opus 4.8.** Each live step's message (L01 to L16), the child's brief and the parent's change, alone, with the main run's tools: no refusal; each began with tool calls (USD 0.66). L05 and L12 again with Source A's and B's text and the GHS decision as tool results: no refusal; at L05 the model cited the decision and asked the user before writing code (USD 0.21).

## Running it

As round 1's runbook says, with these changes. `D` stands for `.venv/bin/python -I .agents/labs/cm_drive.py`.

1. **Config.** `uv run python -I .agents/labs/launcher_config.py "$LAB/sandbox-science.toml" --state-dir "$LAB/launcher" --project cm1 --profile crypto-base --session seed --mcp --provider-env-file <the owner's file>` (the project and session are the seed's). Then edit it: `[model]` `name = "arr-claude-opus-4-8"` and `delegated = "arr-claude-opus-4-8"`; `[model.api]` `"arr-claude-opus-4-8" = "messages"`; remove `reviewer` and `watcher`, as round 1 did.
2. **The lab's directory.** `$CM` with `seed` and `blobs` from round 1's kit directory (links).
3. **Up.** `sandbox-science up --config "$LAB/sandbox-science.toml"`.
4. **Count.** `$D count --config ... --lab "$CM" --alias arr-claude-opus-4-8`: about 20 `count_tokens` calls, free. It prints each snapshot's count and writes `$CM/counts.json`.
5. **Preflight.** `$D preflight --config ... --claude arr-claude-opus-4-8 --lab "$CM" --seed-reused --elsewhere <USD spent outside the kit>`. It checks the alias, its route, its window and prices, and `delegated`, writes `$CM/round.json`, and prints the plan; it exits 1 on a problem, such as an arm forecast under its validity bar or caps over the budget. `$D plan --lab "$CM"` prints the same plan offline, at the prices above.
6. **Arms.** `$D arm --config ... --lab "$CM" --arm claude-250 --alias arr-claude-opus-4-8 --extra=...` (the small shares), then `claude-250-skeleton` and `claude-250-compaction`. They may run at once. The compaction arm needs a run service with the `provider_compaction` mode (this branch or later); its config is the others' (the driver adds the mode to its steps). Each prints its seed and cap, sets the delegation settings on its store, and runs L01 to L16.
7. **Export and score.** `$D export --lab "$CM" --arm <arm>` for each, then `.venv/bin/python -I .agents/labs/cm_score.py "$CM"/arms/*/bundle.json --out "$CM/report"`. `report.md` has the main table, the per-family table, L16's child table and the owner's checklist.
8. **Report and teardown.** As round 1's runbook says.

## Expected cost and time

The cost model (`drive.plan_arm`): an arm's 170 calls, their views spread between 0.45 C and 0.75 C, each reading its prefix from the cache and writing its share of the live steps' growth; each run's first call and the first after each fold write the view again from the state block on (the fact pass measured this on Claude: the system message and tools stay cached, everything after them is written again). The child: 60 calls, from its prompt and brief to its trigger, its 150 pages written once, and a rewrite at its start and each fold. 400 output tokens a call. About USD 35 an arm, 39 for the compaction arm, 109 for the three.

Time: about an hour an arm, as round 1's; the child adds 20 to 30 minutes to L16. A compaction is one longer call (the probe's took about a minute at 89,000 tokens).
