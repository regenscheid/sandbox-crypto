<!-- SPDX-License-Identifier: MIT -->
# OpenShell Lab40: delegation and the MCP connector, first real use

Run on babbage on 2026-10-15 (UTC), against the OpenShell 0.1.3 lab that `.agents/labs/lab_up.sh` brings up, with the images `sandbox-science/minimal:02f85b3`, `sandbox-science/crypto-base:02f85b3` and `sandbox-science/mcp-proxy:e5e55c86`, built for this lab. Everything ran from main `8070b4a1`; from 14:41 the run service ran with the fix of [#394](https://github.com/regenscheid/sandbox-crypto/pull/394), without which no check could be delegated. This was the first real-use lab after M2 ([lab 39](babbage-openshell-lab-39-2026-10-15.md)): delegation plus the MCP connectors, with tools loaded on demand, the citation and copied-number checks, questions to the user, the gateway's costs and skill import. Teardown was complete.

## Setup

- The config was written by `.agents/labs/launcher_config.py` with `--mcp-proxy` and `--provider-env-file`, naming the owner's Cloudflare Access file by its path alone, then edited: one project, `lab40`, on `crypto-base`, with session `a`; `sandbox-orchestrator` as `name`, `reviewer`, `watcher` and `delegated`; the gateway `https://gateway.example.invalid/v1 [redacted in repository]` and the owner's key file, by path; no daily cost limit; Node's path for the math checker; and the example's `index` and `about` for the tool index, which the script does not write.
- `project delegation --depth 1`: at most 10 delegated runs at once, a check gets 150,000 tokens and 12 turns, no models to choose and no fallback.
- `up` ran in a `systemd-run --user` unit and registered `crypto-research-mcp` from the owner's file. The workspace login was opened in the built-in browser through a one-shot loopback redirect; the pane was hidden, so the lab emulated a visible page in the tab, as labs 38 and 39 did.
- At 14:41, after B1, the services were stopped with `down --keep-sandboxes`, the runtime patched with #394, and `up` run again; the research sandbox was kept, and the proxy got a new sandbox.

## Results

| Step | Result | Evidence |
| --- | --- | --- |
| 1. Paper through MCP | Pass, with gaps | Asked for Hamburg's Ed448-Goldilocks paper with the e-print tools. An `mcp_tools` query loaded five e-print tools; `search_eprint` found 2015/625, then `get_eprint` and `eprint_fulltext` with "prime", "twist" and "work". The answer gave the ID, the date stamp, p, the curve with d = −39081, and Curve25519's work factor W ≈ 126. Before its first connector call the model spent nine calls on other tools: `shell` with the search's arguments, `admit_hosts` for eprint.iacr.org, and more loads (see Tokens). The proxy recorded run 4's `eprint_fulltext` under its exact allow rule, with `orchestrator` attribution and session `a`; the first proxy's records went with its sandbox at 14:41 |
| 2. A proved claim and its check | Pass, after B1 | `C-1`, a lemma: p = 2^448 − 2^224 − 1 is 3 mod 4, so −1 is not a square, and a^((p+1)/4) is a square root of a square a. Proof in block b4 of document `goldilocks-mod4-lemma` r1, as `supports`/`proof` evidence. The first `delegate` was refused (B1). With #394: `delegate` of C-1 r1, `wait_delegations`, outcome returned, `confirmed`, `complete`; linked as `independent_check` in C-1 r2. Then D-1, in effect and affecting C-1, established it in one change (r3). Details below |
| 3. A published result with a quote | Pass | The category `ecdlp-work-factor` was defined through the GUI form: measure `work_factor` in bits, higher is better, condition key `attack_model`. The agent re-read the passage with `eprint_fulltext`, then recorded `REF-1` (ePrint 2015/625) and `R-1` (published, `recorded`, `work_factor` 126, `attack_model` generic) with a `quote` of REF-1's ePrint: the date stamp and the text as the tool returned it. Three changes were refused first: an origin on the reference, a condition key `curve` the category lacks, a `builds_on` link to a reference. The checkpoint review recorded a citation check, `supported`, from the `eprint_fulltext` call. The frontier shows R-1 as best known, "Confirmed by the citation check: the consistency reviewer found every quote of r1 supported", with 4 open findings: two `fail` findings on the unit "bits", which the extracted text ("W := 12 logq≈ 126") does not show, and two inconclusive |
| 4. A copied number | Pass, with gaps | Sage counted points on a Weierstrass model of the curve: N = 4q, with q the 135-digit prime RFC 7748 gives. Four writes of the JSON from Sage failed (a missing directory, Sage integers that `json` cannot write), so the agent wrote it by hand: its `p` field holds N. Published as r4, and q copied into the document's block b5. The terminal review's snapshot held the document and the JSON, and no finding came: the copy was right. Asked for a one-digit typo, the next run recorded `C-2` with q pinned to the JSON, but unchanged, while saying it had changed digit 60; the check rightly found nothing. Told so, the next run changed digit 60 in C-2 r2. Its terminal review recorded the platform's finding: `fail`, `contradicting_value`, "a 135-digit number … 1 edits from it, the first difference at digit 60". The finding card's digit-by-digit comparison marks the one place. The agent then rebutted the finding seven times (B2) |
| 5. A question to the user | Pass | Two `ask_user` questions. The first offered two approaches; the GUI showed both as buttons with "Let the agent decide" and "Cancel the question", and the click answered it ("You picked …"), `picked`. The second had no options and was answered in the message box with "Send as the answer", as `own_words`. Both closed `answered`, and each run went on with the answer |
| 6. Cost | Pass, with a gap | The chat panel shows each call's cost; the run view, "30 model calls (sandbox-orchestrator), 835,513 input and 2,874 output tokens, $0.04"; the project page, "$0.14 in the last 24 hours, $0.14 in all, as the gateway reported it": MAIN's $0.1408 and the check's $0.0023. The reviewer's calls have no cost (B5) |
| 7. Skill import | Pass | `skills import --git https://github.com/regenscheid/crypto-analysis-skills` resolved (public, no license): 3.0 s wall, commit `4089e994`, root `skills/`, 162 skills accepted, none excluded, no file left out, no warning. Pinned to `lab40`, revision 1. Nothing else from the pack is in this report |

**The check of C-1.** Its brief was the platform's: the task, its one input (the proof block, to read with `document_read`), the entry and revision, the limits and the alias; the parent's note was empty. The child had 31 tools: no ledger, document or memory writes, and no plan, review, delegation, question or connector tools; it kept the sandbox's tools, `write_file`, `publish` and `provision` among them. It read C-1 r1 with `ledger_read` and block b4 with `document_read`, and checked p mod 4, p's primality and the identity in Sage: 4 calls, 20,741 tokens. Its coverage listed four items examined and none not examined; `read_entry` and `read_proofs` are true. The platform recorded its outcome as a work record on C-1 r1 (`assessment`, by the delegated run, its note "confirmed, complete: …"). The session's run tree shows the delegated run under its parent with its limits (12 turns, 150,000 tokens), the brief read-only, "The platform observed that it read the entry at its revision and read each proof's target", the verdict, the summary and what it examined.

**The gate for C-1 at r2,** as its panel read it before the establishing change: "Every clause holds: the main agent's decision could establish r2."

| Clause | Answer |
| --- | --- |
| Evidence that counts | Holds: one `proof` link |
| An independent check, when the evidence is a proof | Holds: the delegation confirmed a revision with the same statement, fields, assumptions and evidence, in a fresh context |
| Computational evidence pinned to an artifact | Holds: none is computational |
| A completed review that held the entry whole | Holds: the terminal review of the linking run (`passed`) covered C-1 at r2 |
| No unresolved fail finding | Holds |
| No open flag | Holds |

The establishing change was accepted. A later terminal review then raised a `fail` finding on C-1 (B3), so the panel on r3 reads "It is established" with the finding clause "Not yet".

## Tokens

The main agent used 2,573,599 tokens (2,563,292 in, 10,307 out, $0.1408) in 7 runs, well over the 1.5 million aimed for: runs 4 and 7, both stopped by the turn limit while answering findings, took 1,594,878 of it. The check used 21,139 ($0.0023). The reviewer used 1,132,395 (1,118,974 in, 13,421 out) in 87 calls and 12 reviews: 1 passed, 4 inconclusive, 7 with findings, none capped, one with incomplete coverage. The whole lab used 3,727,133 tokens.

| Run | What it did | Calls | Input | Output | Cost |
| --- | --- | --- | --- | --- | --- |
| 1 | the paper through the e-print tools | 20 | 343,729 | 827 | $0.0242 |
| 2 | the lemma, the document and C-1; `delegate` refused (B1) | 4 | 59,250 | 777 | $0.0063 |
| 3 | `delegate`, wait, link | 5 | 75,015 | 269 | $0.0051 |
| 3's check | the delegated run | 4 | 20,741 | 398 | $0.0023 |
| 4 | establish C-1; REF-1 and R-1; findings until the turn limit | 30 | 753,023 | 3,468 | $0.0411 |
| 5 | the first question; Sage, the JSON, the document | 20 | 366,103 | 1,602 | $0.0179 |
| 6 | the second question; C-2 without the typo | 7 | 130,659 | 490 | $0.0054 |
| 7 | the typo; the rebuttal loop until the turn limit (B2) | 30 | 835,513 | 2,874 | $0.0407 |

- **Tools on demand.** The proxy listed 70 tools, with 66,524 characters of descriptions and schemas; the index in `mcp_tools`'s description, with the config's `about` and `index`, is 4,718 characters. Each main run's first call took 13,796 to 15,452 input tokens, where lab 39's took about 25,000 with every tool's schema, and about 13,000 without the connector.
- **Schemas per call.** None until a load. In run 1: 5 from its second call, 10 and then 12 after two more loads, at 14,597 to 24,505 tokens a call as its conversation grew. In run 4: 15, ten of them unrelated (B4). Runs 2, 3 and 5 to 7 loaded none.

## Bugs

- **B1, high: no check could be delegated with the owner's gateway** ([#394](https://github.com/regenscheid/sandbox-crypto/pull/394)). `delegate` was refused: "model_unavailable: the model gateway lists no model 'sandbox-orchestrator', and the project names no fallback". The gateway serves the alias, as MAIN's calls in the same run showed, but `GET /models` leaves it out, and the harness took the list as the truth for every alias. The fix calls the delegated role's own alias all the same, as MAIN's is never checked; a brief's chosen alias that the list leaves out is still refused.
- **B2, high: an agent's own rebuttal came back to it as a notice** ([#397](https://github.com/regenscheid/sandbox-crypto/pull/397)). A rebuttal leaves the finding open at a new revision, and every open review finding at the interrupt severity was queued as a notice, whoever moved it. Run 7 rebutted the copied-number finding seven times (revisions 2 to 8) until its 30 turns ran out, at 835,513 input tokens; run 4 did the same after one rebuttal. The fix queues no notice for an agent's response.
- **B3, medium, reported: the reviewer cannot see what an established entry rests on.** A ledger revision brings into the review's snapshot the artifacts its evidence pins, but not a proof's document block or a delegation's outcome. The terminal review after C-1 was established said "The proof block is only referenced, not included in this snapshot" and took run 2's refused `delegate` for the check's outcome, and raised a `fail` finding on C-1, which now holds its finding clause shut. To see it, establish a claim whose proof is a document block and whose check is a delegation, and read the next review's findings on it.
- **B4, medium, reported: an `mcp_tools` query loads its best matches even when none fits.** The model looked for `ledger_change` and `ledger_frontier` through `mcp_tools` four times in run 4: the queries loaded ten unrelated attack-catalog and Firecrawl tools, whose schemas every later call carried, and the request by name answered `unknown`. Run 1's later queries loaded seven Firecrawl and paper-search tools it never called. A query should load nothing below some score, and the tool could say that the harness's own tools need no loading.
- **B5, medium, reported: the project's cost leaves out the reviewer.** Review calls keep tokens but no cost, so the project page and a daily cost limit count MAIN and delegated runs only; here the reviewer made 30 % of the lab's tokens.
- **B6, low, reported: a check's work record reads as having no verdict.** The entry view shows the delegation's record as "Assessment … A critique, no verdict." while its note says "confirmed, complete", and its provenance as "imported: {"delegation": …}".
- **B7, low, reported: the lab's config script leaves things out.** `launcher_config.py` writes no `delegated` alias and no `index` or `about` for the tool index, which the example keeps in comments; the lab added them by hand.
- **B8, low, reported: the category form's hint.** It suggests condition keys "such as attack model or cost model", then refuses a key with a space.

## Observations, not fixed

- **The model is the weak part.** Besides the nine stray calls in run 1, it wrote the JSON's `p` as the curve's order, said it had changed a digit it had not, sent the same unchanged statement twice ("the change to C-2 changes nothing"), and rebutted a finding it agreed with. The reviewer, on the same alias, said C-2's q matched the JSON's `curve_order` (it matched `q`) and twice said a tool rejected its submission.
- **`eprint_fulltext` matches single words.** "Pollard rho security" found no passage in 2015/625; "rho", "prime", "work" and "security" did.
- **A citation check and a finding can disagree.** R-1's quote is `supported`, so the frontier shows R-1 confirmed, while two `fail` findings say the quote does not support "bits".
- **The copied-number check reads numbers, not names.** The JSON's `p` field holding N raised nothing.
- **A check keeps the sandbox's write tools** (`write_file`, `publish`, `provision`); the check changes no entry, document or memory, as #382 has it.
- **The browser pane blocked the app's script once** (`ERR_BLOCKED_BY_CLIENT`) on the first load after the redirect; a reload worked.
- **There is no forward unit with the launcher:** the forward is one of `up`'s parts, which `down` stops.

## Whether the first real-use milestone works

Yes, with #394 and #397, and with conditions. Through the connector the agent found and read the paper, and a quote of its ePrint passage was confirmed by the citation check on the frontier. A proved claim got a delegated check in a fresh context, linked as its independent check; the gate's six clauses held, a completed review among them, and the main agent established it. The copied-number check stayed quiet on a right copy and caught a one-digit change, and both kinds of answer to a question worked. Missing:
- #394 must merge, or delegation does not work with the owner's gateway at all; #397 should, or a single rebuttal can spend a run's remaining turns;
- the reviewer needs a proof's block and a check's outcome in its snapshot (B3), or it raises `fail` findings on entries the gate passed;
- `mcp_tools` should not load tools nobody asked for (B4), and the reviewer's cost should count (B5);
- with this model, a session of seven requests cost 2.6 million MAIN tokens, though only $0.14.

## Evidence and teardown

The evidence is kept outside git, in an archive on babbage (sha256 `ae0e58874b9dea000a0831f59f433652df2eda9abc61986999392a2c80d0cec4`, 47 entries). It holds the config and the lab's policy, the scripts, the notes, the command outputs, the run transcripts, the token report, the proxy's records, the GUI's texts, the skill import's report, a copy of the control store and the parts' logs. It holds no tokens, no login address, nothing from the owner's secrets directory and nothing of the gateway's database.

Teardown ran `down`, which suspended the research sandbox and deleted the proxy's; there was no forward unit to stop first. The research sandbox was then deleted, and `lab_down.sh` stopped the lab units and removed the lab directory, with the gateway's database, its key and the registered provider in it. The launcher's state and config directories and the lab's checkout were removed. Afterwards no lab unit, port, process, OpenShell path or driver socket was left. Only the image `sandbox-science/mcp-proxy:e5e55c86` was added.
