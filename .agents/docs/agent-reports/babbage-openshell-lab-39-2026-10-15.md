<!-- SPDX-License-Identifier: MIT -->
# OpenShell Lab39: the M2 exit path again, with the MCP connector

Run on babbage on 2026-10-15 (UTC), against the OpenShell 0.1.3 lab that `.agents/labs/lab_up.sh` brings up, with the images `sandbox-science/minimal:02f85b3`, `sandbox-science/crypto-base:02f85b3` and `sandbox-science/mcp-proxy:23af8365`, built for this lab. Everything ran from main `31d85557`, unchanged; the lab's targets and policy were corrected by hand where [#327](https://github.com/regenscheid/sandbox-crypto/pull/327) and [#328](https://github.com/regenscheid/sandbox-crypto/pull/328) now fix them. This was the second M2 exit checkpoint ([plan](../../refactor-plan/PLAN.md), Phase 4 item 8), after [lab 38](babbage-openshell-lab-38-2026-10-15.md). Teardown was complete.

## Setup

- The config was written by `.agents/labs/launcher_config.py` with `--mcp-proxy` and `--provider-env-file`, naming the owner's Cloudflare Access file by its path alone, then edited: one project, `lab39`, on `crypto-base`; sessions `a` and `b` in its environment and `c` dedicated; `sandbox-orchestrator` as `name`, `reviewer` and `watcher`; the math checker with Node's path; and a copy of the example policy with crypto-research-mcp's address in place of its placeholder.
- `up` ran in a `systemd-run --user` unit. The workspace login was opened in the built-in browser through a one-shot loopback redirect, so the address was never printed. The browser pane was hidden, so the lab emulated a visible page in the tab, as lab 38 did.
- The project's MAIN budget was capped with `project budget` (800,000 tokens, then 790,000), which the run service checks when a run starts.
- After step 1 the services were stopped with `down --keep-sandboxes` and started again without `[mcp_proxy]`, to save tokens (see Tokens). Steps 2 to 7 ran without the connector.

## The provider helper

`up` registered `crypto-research-mcp` on the new gateway from the owner's file: the helper ran `provider profile lint` and `import`, then `provider create`, and said only "created the OpenShell provider crypto-research-mcp from" the file's path. It refused nothing. Neither of the two assumptions its author could not check was wrong:

| Check | Result |
| --- | --- |
| `openshell provider profile list -o json` | `[]` on a new gateway. With profiles, a top-level JSON array of objects, each with `id` (`"cloudflare-access"`), `resource_version`, `display_name`, `description`, `category`, `credentials`, `endpoints`, `binaries`, `inference_capable`, `source` and `scope`, and no `name`. The helper finds the profile by `id`. Its search also descends into the credentials, so a credential named `cloudflare-access` would match too; none does |
| Importing an existing profile | Exit 1. On stderr: "custom provider profile 'cloudflare-access' already exists", then "provider profile import failed". The helper's test for "exist" in stderr holds |

## Results

| Step | Result | Evidence |
| --- | --- | --- |
| 1. Paper, through MCP | Pass, with gaps | The first run's call to `e-print-mcp_search_eprint` came back `policy_denied`: the server lists bare tool names, and the example policy's `tool_prefix` denied them all (B1). With the prefix removed, the agent called `e-print-mcp_get_eprint`, `eprint_fulltext` twice and `eprint_file` for ePrint 2015/625 (Hamburg, Ed448-Goldilocks). `eprint_file` gave a temporary URL on the e-print service's own file host; the agent admitted only that host, downloaded the PDF into `$PROJECT_SHARED/papers` (254,510 bytes, sha256 `86544903…`), published it and read it with `pdf_info` and `pdf_pages`. It searched with `firecrawl_search` instead of `search_eprint`, and said so; `eprint_fulltext` found no passage for either query. No call escalated, so nothing was approved. The proxy recorded the run's 8 messages with `orchestrator` attribution and session `a`, each tool call under its exact allow rule; 4 more were the lab's own check, a `get_eprint` as the operator. The GUI lists the PDF under Results, and its viewer shows page 1 of 10 |
| 2. Sage | Pass | In `krn-a-sage`: p = 2^448 − 2^224 − 1 prime, the curve's order 4q and its twist's 4q′ from a Montgomery model, q and q′ prime; q is RFC 7748's value. In `krn-a-python`: Miller–Rabin on q and q′, and [4q]P = (0, 1) on a random point with the Edwards addition law. The numbers went to `results/ed448-order.json`, published, and correct. A `factor()` of a 446-bit composite timed out on the way |
| 3. Document | Pass | `ed448-order-check`, 11 blocks and 21 formulas. The math checker accepted all 21, none unavailable, and the GUI shows them with their math. Block b5's q′ had 132 digits where the computation has 135 |
| 4. User feedback | Pass | A GUI comment on b5, addressed to `a` while it was idle, pointed out the three missing digits. A run claimed it at once, with no chat message. The agent made revision 2 and answered in the thread with what it changed; the thread shows the reply, "Made revision 2; changed blocks: b5" and "Request to a: responded". The new q′ is still wrong: it put back "340" and dropped "160" further on. The agent did not read its JSON: `read_file` on a wrong path failed, and `find_artifacts` with the path it had been shown returned nothing (B4) |
| 5. Automatic review | Pass | Seven reviews, none capped and none failed (table below). Their findings: `search_eprint` not used (warn), the computation's output missing from the snapshot (warn, twice), and the document and ledger disagreeing on q′ (fail). The agent answered the fail finding with `fix`, after copying the document's q′ into the ledger; a confirmation review left it "claimed fixed". The session's review panel and the entry view list the findings |
| 6. Board | Partial | Without help, the agent found the JSON with `find_artifacts` and recorded `I-1` (issue, open) and `R-1` (result, proposed, answers `I-1`), with `supports`/`exact_computation` evidence pinned to the JSON at revision 1 with its SHA-256. Its first change was refused for a field's pattern. Its attempt to establish `R-1` was refused before the gate: "R-1 becomes established only in a change that puts in effect a decision that affects it"; there was no budget for a second attempt. The board shows `I-1` and `R-1`. The entry view shows the statement, fields, evidence, the user's check, three findings, links, the gate and history. `R-1`'s `q_prime` field carries the wrong value, while its evidence holds the right one |
| 7. Isolation | Pass | See below |

**The gate for `R-1` at revision 2,** as its panel read it: "5 of 6 clauses hold: the main agent cannot establish r2 yet."

| Clause | Answer |
| --- | --- |
| Evidence that counts | Holds: one `exact_computation` link |
| An independent check, when the evidence is a proof | Holds: no evidence is a proof, so no check is needed |
| Computational evidence pinned to an artifact | Holds |
| A completed review that held the entry whole | Holds: a completed review covered `R-1` at revision 2 |
| No unresolved fail finding | Not yet: `finding-0469…`, claimed fixed and not confirmed |
| No open flag | Holds |

The review clause held, which it could not in lab 38. No user check was recorded: `R-1` claims no proof.

**Reviews.**

| Review | Kind | Outcome | Calls | Input | Output |
| --- | --- | --- | --- | --- | --- |
| runs 1 and 2 | terminal | findings | 9 | 251,050 | 2,487 |
| run 3 | checkpoint | inconclusive | 7 | 70,250 | 770 |
| run 3 | terminal | passed | 4 | 14,380 | 457 |
| run 4 | checkpoint | findings | 7 | 55,443 | 1,209 |
| run 4 | terminal | findings | 6 | 41,520 | 1,144 |
| run 4 | confirmation | findings | 7 | 58,744 | 1,697 |
| run 4 | terminal | findings | 9 | 100,412 | 1,235 |

**Isolation.** The budget left no chat for `b` or `c`, so the probes went through the worker's API with the run service's token, as lab 38's did for `a` and `c`. `c`'s environment was made with `ensure_environment`, as a run's admission makes it.

| Session | Sandbox | Python kernel | `lab39_a_secret` | Shared marker | Session `a`'s file |
| --- | --- | --- | --- | --- | --- |
| `a` | `ss-03f6…` (the project's) | `krn-a-python`, pid 826 | `'a-only'` | written | written |
| `b` | `ss-03f6…` | `krn-b-python`, pid 862 | not defined | read | `../a` is there: one container |
| `c` (dedicated) | `ss-9750…` | `krn-c-python`, pid 598 | not defined | read, synced | absent; only `sessions/c` |

## Tokens

MAIN used 1,098,078 tokens (1,090,230 in, 7,848 out) in 4 runs, over the 800,000 aimed for. The reviewer used 600,798 (591,799 in, 8,999 out) in 7 reviews. The whole lab used 1,698,876.

| Run | Session | Calls | Input | Output |
| --- | --- | --- | --- | --- |
| paper, denied by the policy | a | 10 | 251,089 | 698 |
| paper through the e-print tools | a | 9 | 240,925 | 698 |
| Sage, Python, the JSON and the document (no connector) | a | 15 | 231,720 | 3,367 |
| the comment, the ledger, the finding (no connector) | a | 17 | 366,496 | 3,085 |

- **The connector's tools.** crypto-research-mcp lists 72 tools, about 67,000 characters of descriptions and schemas once the harness has cleaned them: some 17,000 tokens in every model call of every run of the project. With them a run's calls cost about 25,000 tokens each; without them, about 13,000 at the start of a run (B5).
- **The first run** spent 7 of its 10 calls reading, making and activating a session plan, and both its connector calls were denied.
- **The comment's run** took the most. It did the document, the ledger and the establishing attempt, then got the fail finding while it ran and went on answering it, with a growing conversation in each call. The budget is checked only when a run starts, so the cap did not stop it.

## Bugs

- **B1, high: the example policy denies every crypto-research-mcp tool** ([#328](https://github.com/regenscheid/sandbox-crypto/pull/328)). Through its Access address the server lists bare names (`e-print-mcp_search_eprint`), and `examples/policy.yaml` declared `tool_prefix: crypto_research_mcp-`, so the library refused every call as outside the prefix, and the compiled fence admitted only prefixed names. The live list also differs from the 2026-10-10 fixture: the seven `nist-mcp_*` tools are absent, and `firecrawl_monitor_delete` is new, which asks first.
- **B2, medium: the lab tooling could not start the proxy, nor reach the server** ([#327](https://github.com/regenscheid/sandbox-crypto/pull/327)). `lab_up.sh`'s targets gave the proxy no `filesystem`, so OpenShell could not start its entrypoint ("Permission denied (os error 13)"), and `launcher_config.py` kept the example policy's placeholder address.
- **B3, medium: `up` fails after a change to the config's policy** ([#329](https://github.com/regenscheid/sandbox-crypto/pull/329)). `project create` refuses a project on another policy ("revise its policy instead"), and the launcher ran it for every project. The lab revised the project by hand.
- **B4, medium: `find_artifacts` does not find a file by the path it lists** ([#330](https://github.com/regenscheid/sandbox-crypto/pull/330)). It lists `sessions/a/…`, and its `path` filter put that under the session again. The agent then told the reviewer the artifact was not there.
- **B5, high, reported: the connector's tools are in every model call.** Every run of the MCP project gets all 72 tools, whatever its task, at about 17,000 tokens a call. With the project's 2,000,000-token day, the path cannot run twice. A run needs only the tools its task may use: a configured subset of a server's tools, or tools offered on request. To see it, compare the first input tokens of a run with and without `[mcp_proxy]`.
- **B6, medium, reported: nothing checks a number against the computation that holds it.** The model copied a 135-digit q′ by hand into the document, wrongly twice, and into `R-1`'s field. The reviews compared the document and the ledger with each other. The run-3 checkpoint review had both the document and the JSON in its snapshot and did not see the gap; the comment run's reviews had no artifact in their snapshots, although `R-1`'s evidence pins the JSON, and said the output was missing. The fail finding about the disagreement then held the gate shut on a "fix" that copied the wrong value. To see it, compare b5 or `R-1`'s `q_prime` with `q_twist` in the JSON.

## Observations, not fixed

- **`eprint_fulltext` found no passage** for "field prime 2^448 curve equation d" or "order twist cofactor prime"; the paper's text came from the PDF tools after `eprint_file`.
- **The agent chose `firecrawl_search`** over `search_eprint`, as the reviewer noted.
- **A connector call needs an active plan only when the session has a plan**, but the model made one before its first call.
- **The ledger tool's result fields:** the first change was refused for `category`, "String should match pattern …".
- **`R-1` stays proposed with no decision in effect.** The agent read the structural refusal as the gate's answer and did not try with a decision.

## Whether the M2 exit criterion is met

Met, with conditions. Two workbooks shared the project's container with distinct kernels, a third ran in its own, and the path ran end to end with the real model: the paper through the connector's e-print tools, Sage with Python, a document with checked math, a comment that woke the idle session, reviews that completed, and the board with pinned evidence and the gate's answer. Every gap lab 38 named is closed. The conditions:
- with main as it is, step 1 fails: #328 (B1) and #327 (B2) must merge, and #329 (B3) and #330 (B4) should;
- with the connector in every call, the path does not fit the daily budget (B5);
- the result's numbers in the document and the ledger are wrong, and no check caught it (B6); the gate held `R-1` back only through a finding about another disagreement.

Establishing was not reached, and the M2 criterion does not ask for it.

## Evidence and teardown

The evidence is kept outside git, in an archive on babbage (sha256 `881e6069fc69085c84eb36cbee5bb207ee5440d59953aa55de08266b71a1cf47`, 59 entries). It holds the configs and the lab's policy, the scripts, the notes, the command outputs, the run transcripts, the proxy's records, the isolation records, the published JSON, the screenshots, a copy of the control store and the parts' logs. The e-print service's temporary download URL was replaced in the transcripts and the store's copy. It holds no tokens, no login address, nothing from the owner's secrets directory and nothing of the gateway's database.

Teardown ran `down`, which suspended both research sandboxes; the proxy's sandbox had been deleted by the `down` after step 1, and no forward unit was left. Both sandboxes were then deleted, and `lab_down.sh` stopped the lab units and removed the lab directory, with the gateway's database, its key and the registered provider in it. The launcher's state and config directories were removed. Afterwards no lab unit, port, process, OpenShell path or driver socket was left. Only the image `sandbox-science/mcp-proxy:23af8365` was added.
