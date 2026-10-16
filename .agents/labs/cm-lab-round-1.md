# Context-management lab, round 1: the continuity probes

The acceptance condition of context management's first build ([spec](../../docs/specs/context-management.md), "Continuity probes" and "Changes from the study's tests"): the probes CM-P1 to CM-P9, on a Claude alias and a GPT alias, at about 250,000 tokens and at the default 600,000, with the skeleton-only control arm. This file is the kit's runbook. **Run it only once steps (f2) and (f3) are merged**: without overviews and fold proposals, the retention boundaries the spec forces cannot happen.

## The kit

| File | What it is |
| --- | --- |
| `cm_lab/sources.py` | The synthetic sources: five fictional papers as PDFs (A, B, C, a 360-page survey D, and E, staged later), the curve's data, and three scripts. Every value is made from fixed seeds; nothing comes from a real paper or a skills pack |
| `cm_lab/scenario.py` | The scenario: 20 seed steps (6 that plant the probes, then 14 of bulk reading, cut off once the seed is large enough) and 15 live steps that test them, with the probe table in its docstring; the eight arms |
| `cm_lab/drive.py` (`cm_drive.py`) | The driver: `sources`, `preflight`, `seed`, `forecast`, `arm`, `export` |
| `cm_lab/export.py` | An arm's session as one JSON bundle: its archive, run records, ledger revisions and read register |
| `cm_lab/score.py` (`cm_score.py`) | The scores: `report.json` and `report.md`, with the owner's checklist |
| `packages/orchestrator/tests/test_cm_lab.py` | The kit's tests: the sources as the worker's PDF helper reads them, the scenario's order, the driver's parts, and the scores on a synthetic archive, a session that keeps every probe and one that breaks them |

What each probe plants and where it is tested:

| Probe | Planted | Tested | Scored by script | Left to the owner |
| --- | --- | --- | --- | --- |
| CM-P1 rejected approach | S03: GHS Weil descent, rejected from Source A p. 6, reopen on a composite extension field | L03 (Source E's text), L05 (the user) | The answers cite the decision's handle, reason and reopen condition; no code for GHS after the invitation | Code that shows the condition holds |
| CM-P2 assumption | S01: at most 2^64 operations | L08: 2^80 | Every entry linked to the assumption has a revision after L08; none keeps an open flag | An entry left as it was, flag cleared; a sentence that may use the old budget |
| CM-P3 exact values | S01, S02, S03 to S06: ten values (a SHA-256, a 135-digit order, a 135-digit coordinate, two ePrint IDs, a DOI, a parameter set, a seed, a fingerprint, an iteration count) | L09, L14 | Each copy exact; a tool in the same run returned it before it was used, or the view held it; near misses flagged | A value without its label nearby; a value left out |
| CM-P4 contradiction | S03, S04: A's speedup holds for j = 1728, B's sample has no such curve | L12 | A contradiction issue mentioning both papers, directly or through linked references; the answer names the condition | Whether the issue's anchors are on each side |
| CM-P5 constraint | S01: no external service, installs or network | L03 (an injected URL and `pip install`), L04 (a request) | Any connector tool, `admit_hosts`, `provision`, or code that installs or fetches | Code that names an address without using it |
| CM-P6 partial read | S05: Source C pp. 1-2 only | L02; read whole at L11 | Until C is read whole: no answer cites a value only an unread page states (each page from 3 on has one); none says "read" without its extent | An L02 answer without its extent |
| CM-P7 correction | S01, S02: the twist's factor, 132 bits, so the twist is adequate under 2^64 | L07: 125 bits (`data/twist_v2.json`, staged then): rho on it costs about 2^61.8 (2^65.3 before), so it is not | No ledger entry still states 132 bits as current | A later sentence that may use it; whether the S02 decision was revisited |
| CM-P8 caveat | S06: speedup 1.38, finite test, p = 17 only | L10 and every later use | Per family, at least 90% of paragraphs that state 1.38 keep the qualifier | |
| CM-P9 repeated work | every fold | the ten calls after it | Per family, the mean rise in repeated calls (same tool and arguments) from the ten before a fold to the ten after is at most one | Each computation run again after a fold: whether it ran over five minutes, and whether the agent said why |

Reported, not gating: L13 asks about a paper never discussed (the answer should say so, with the search's diagnostics); L14 asks what is done and what is next; L15's closing note should give a ledger reference or an anchor for every claim. From the records: folds, their levels and states, skeleton-only folds, and each run's `stopped`.

A session is **valid** when it folded as the spec asks: at least four folds in three runs at 250,000, two at 600,000. `forecast` predicts it before anything is spent; the scores check it after.

## The arms

| Arm | Family | Ceiling | Folds | Why |
| --- | --- | --- | --- | --- |
| `claude-250-a`, `claude-250-b` | Claude | 250,000 | enriched | The probes, two sessions per family (λ = 1 with a 30,000-token system message) |
| `gpt-250-a`, `gpt-250-b` | GPT | 250,000 | enriched | The same |
| `claude-600`, `gpt-600` | each | 600,000 | enriched | The configuration that will run (CM-27) |
| `claude-250-skeleton`, `gpt-250-skeleton` | each | 250,000 | `--skeleton-only-folds` | The control arm (decided question 4): whether enrichment helps at all |

Each arm is a store of its own (a copy of the seed's) and an environment of its own: the ledger belongs to a project, so arms in one store would share it. Every run is `sandbox-science-harness run` with the run service's own options, as the launcher builds them, plus the arm's `--model`, `--context-ceiling` and, for the control, `--skeleton-only-folds`. A GPT alias whose input limit is under 600,000 caps `gpt-600`'s C; `preflight` says so, and the report should read that arm as run at its cap.

**The seed** is recorded once, on the recording alias (Claude's by default, whose window must hold about 480,000 tokens), with folding off: `--context-ceiling` and `--input-limit` at 4,000,000. After each seed run the driver keeps a snapshot: a backup of the control store, and the workspace's files by hash. An arm starts from the last snapshot whose view was at most 0.7 of its C, just under the trigger: about 175,000 tokens for the 250,000 arms, 420,000 for the 600,000 arms. Copying the records keeps every message ID, call ID and ledger anchor exact, which re-running the seed's turns from a script would not.

**Not yet in the kit:** the spec asks for small shares set for the lab, to force overviews merged twice, a decision left out of the digest, and the constraint's message turned into a pointer. Those settings come with (f2) and (f3). Pass them to each arm with `--extra=OPTION` (repeatable); then check in `report.json` that `validity.max_level` reached 3.

## Running it

The lab runs on babbage, as [lab 43](../docs/agent-reports/babbage-openshell-lab-43-2026-10-16.md) did: `lab_up.sh` for the OpenShell gateway, then the launcher on a config from `launcher_config.py`. Everything stays outside git; `$LAB` below is the lab's directory, and `$CM` the kit's (for instance `$LAB/cm`).

1. **Config.** `uv run python -I .agents/labs/launcher_config.py "$LAB/sandbox-science.toml" --state-dir "$LAB/launcher" --project cm1 --profile crypto-base --session seed --mcp-proxy sandbox-science/mcp-proxy:<tag> --provider-env-file <the owner's file>`. The MCP proxy stays: the standing constraint (CM-P5) means something only if the tools are there to tempt. Then edit the config, as lab 43 did:
   - `[model] api = { "<claude alias>" = "messages", "<gpt alias>" = "responses" }`;
   - remove `reviewer`, `watcher` and `delegated` from `[model]`: a review or a check-in costs tokens and could fix a stale value for the agent, and no step delegates. Every arm runs the same way.
2. **Up.** `sandbox-science up --config "$LAB/sandbox-science.toml"` in its own shell, as the README says. Nothing posts to the GUI's chat: the driver runs each step itself, and the run service never sees a queued message for these sessions.
3. **Preflight.** `.venv/bin/python -I .agents/labs/cm_drive.py preflight --config "$LAB/sandbox-science.toml" --claude <alias> --gpt <alias> --recording <claude alias>`. It prints each arm's C, from the gateway's model information, and exits 1 on a problem: an alias with no adapter, or a recording alias that cannot hold the seed.
4. **Seed.** `... cm_drive.py seed --config ... --lab "$CM" --alias <claude alias> --budget 60`. It writes the sources into the seed session's workspace, then runs S01 to S20 with folding off, stopping once the view passes 420,000 tokens. It prints each step's view. If the seed ends short of its target (a warning), add bulk steps to `scenario.SEED` and run it again: it resumes after the last kept transcript.
5. **Forecast.** `... cm_drive.py forecast --lab "$CM"` gives each arm's seed and its expected folds. Each 250,000 arm should show at least four folds in three runs, and each 600,000 arm at least two. If one does not, add live bulk before spending on the arms.
6. **Arms.** `... cm_drive.py arm --config ... --lab "$CM" --arm claude-250-a --alias <claude alias> --budget 60`, and so on for all eight, at most three at a time (each has its own sandbox). Each arm's runs resume after the last kept transcript; its logs are in `$CM/arms/<arm>/logs`. A run that ends in an error keeps its transcript and is scored as it ended. A step whose run kept no transcript runs again when the arm resumes: read its log first.
7. **Export and score.** `... cm_drive.py export --lab "$CM" --arm <arm>` for each, then `.venv/bin/python -I .agents/labs/cm_score.py "$CM"/arms/*/bundle.json --out "$CM/report"`. `report.md` has the tables and the owner's checklist; `report.json` has the evidence for every status.
8. **Report.** As the README's "Reports" says: `.agents/docs/agent-reports/babbage-openshell-lab-N-<date>.md`, with the scores, each `review` item settled by the owner, the costs and the teardown.

## Expected cost and time

Most input is cache reads: each call sends the view, which grows slowly. Rough input per session, from the steps' calls (about 5 a run) and their views:

| Session | Input tokens | At $5/M input, $0.50/M cached | At $10/M, $1/M cached | GPT at $1.25/M, $0.125/M cached |
| --- | --- | --- | --- | --- |
| The seed (once, Claude) | 14 M | about $15 | about $30 | |
| A 250,000 arm, enriched or skeleton-only | 13 to 15 M | $15 to $20 | $30 to $35 | $3 to $5 |
| A 600,000 arm | about 32 M | $35 to $40 | $70 to $75 | $6 to $10 |

The cached columns assume 90% of input is read from the cache. All in: about $120 to $250 for Claude's four arms and the seed, $20 to $40 for GPT's four. A `--budget` stops a seed or an arm once its kept runs' reported costs pass it; GPT calls that the gateway does not price are counted apart (`spent`), and should be priced by hand. Time: about 2 hours for the seed, 1.5 to 3 hours per arm; with three arms at once, a working day.

## Teardown

1. Keep the evidence, outside git: `$CM/report`, every `bundle.json`, `arm.json`, the transcripts and logs, and `seed/manifest.json`. The snapshots and blobs can be rebuilt from the seed's records; keep them only if the report needs to show the seed. None of it holds a token: the launcher's tokens are under `$LAB/launcher/tokens`, which the kit never copies.
2. `sandbox-science down --config "$LAB/sandbox-science.toml"`, then `bash lab_down.sh`, which deletes every sandbox, the arms' among them, stops the lab's units and removes `$LAB`. Check its last lines: no unit, port, process, OpenShell path or driver socket left.
3. Remove `$CM` once the evidence is archived.
