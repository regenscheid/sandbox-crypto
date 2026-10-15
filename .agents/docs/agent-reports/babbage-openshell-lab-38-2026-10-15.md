<!-- SPDX-License-Identifier: MIT -->
# OpenShell Lab38: the M2 exit path with a real model

Run on babbage on 2026-10-15 (UTC), against the OpenShell 0.1.3 lab that `.agents/labs/lab_up.sh` brings up, with the images `sandbox-science/minimal:02f85b3` and `sandbox-science/crypto-base:02f85b3`. The lab infrastructure and the web build were main `25feb394`. The launcher, worker, run service and workspace ran from a checkout of that main with the fixes of [#295](https://github.com/regenscheid/sandbox-crypto/pull/295) and, from 04:16, [#297](https://github.com/regenscheid/sandbox-crypto/pull/297): without them the run service does not start, and a dedicated session cannot chat. This was the M2 exit checkpoint ([plan](../../refactor-plan/PLAN.md), Phase 4 item 8). Teardown was complete.

## Setup

- The config was [`examples/sandbox-science.toml`](../../../examples/sandbox-science.toml) with its own state directory and one project, `lab38`, on `crypto-base`, with sessions `a` and `b`. The launcher has no option for a dedicated session, so `c` was opened with `sandbox-science-harness session open --dedicated` once `up` had made the project. There was no `[mcp_proxy]`.
- The model was the owner's gateway, alias `sandbox-orchestrator`, as `name` and as `reviewer`. The launcher has no watcher option; no job ran. The config named the key file, and nothing else touched it.
- `up` ran in a `systemd-run --user` unit. The workspace login was opened in the built-in browser through a one-shot loopback redirect that read the private file, so the address was never printed.
- The built-in browser's pane was hidden, and the session page paused ("Session views paused while this page is hidden"). The lab emulated a visible page in the tab and drove the GUI by its controls.
- The user's actions went through the GUI: chat messages, the document comment, the artifact and PDF views, the review panel, the ledger board and entry view. The isolation markers in session `a` went through the worker's API with the run service's token, as in lab 37.

## Results

| Step | Result | Evidence |
| --- | --- | --- |
| 1. Paper | Partial | ePrint's PDFs answered 403 with a Cloudflare challenge (`cf-mitigated: challenge`), from the sandbox and from babbage; its abstract pages answer 200. The policy allowed the GET, `admit_hosts` admitted the host, and there was no permission request to approve. The agent retried with a browser user agent before giving up. The lab then used ePrint 2017/518 (qDSA, Renes and Smith) from its arXiv copy. The first try saved it in the session's own `shared/` folder, so `publish` was held and `pdf_info` found nothing (bug B3). Told about `$PROJECT_SHARED`, the agent moved it there, published it (500,173 bytes) and read it with `pdf_info`, `pdf_search` and `pdf_pages`. The GUI listed it under Results, and its viewer, on the second origin, showed the paper |
| 2. Sage | Pass | In `krn-a-sage`: p and N prime, #E = 8N, the point with x = 9 of order N, the twist of order 4N' with N' prime. In `krn-a-python`: N as the paper writes it, and #E + #E' = 2p + 2. The numbers went to `results/curve25519-order.json`, and the session's kernel panel showed both kernels. The paper (p. 10, lines 3–5) says the point has order 8N; the agent did not point out the difference |
| 3. Document | Pass | `curve25519-order-check`, in session `a`'s workbook: three blocks and 13 formulas, shown with their math |
| 4. User feedback | Pass, with a gap | A GUI comment on block b2, addressed to `a`, asked which order was right. Its request stayed queued: an idle session does not start a run for it (LD-22, not built), so a chat message started one. The run got the request, re-read p. 10, made revision 2 (b2 and b3 changed, b4 added) and replied in the thread with what it changed. A first reply without a new revision was refused, "name a revision this run made" |
| 5. Automatic review | Partial | Each run's terminal review read the durable text. Of the 15 reviews: 2 passed, 4 failed ("the reviewer response exceeded its bound", the limit of 16 tool calls in one turn; one window was left unreviewed), and 9 were capped: 5 at the 64,000-token review budget, and the 4 from 04:40 at 60 reviewer calls an hour, three of them with no call at all. One finding, `warn`, `evidence_strength`, inconclusive, on the document's first revision: the review panel showed its severity, statement, remedy and anchors. The agent's `fix` and `rebut` were both refused, "invalid finding response" (bug B4); a later `rebut` without evidence was accepted, and the finding stays open |
| 6. Board update | Partial | `I-1` (issue, open) and `R-1` (result, proposed, answers `I-1`), with two `supports` items of type `exact_computation`: the JSON artifact at revision 1 with its SHA-256, and block b3 of the document at revision 2. Two tries failed first: `supports` as a link kind, and the artifact named by its path, since the agent has no way to find an artifact's ID in a later run (B6); the user gave the IDs from the GUI. Establishing `R-1` in one change with a new decision in effect was refused: `gate_not_met: R-1's computational evidence is not pinned to the artifact revision that holds the computation`, clause `computation`, because the block's evidence was typed as a computation too. No review covers `R-1`, so the review clause would not hold either. The board showed `I-1` and `R-1`; the entry view showed the statement, fields, links, the decision that affects it and the history, but neither the evidence nor the findings, which its route returns (B5). No proof was claimed, so no `ledger check` was needed |
| 7. Isolation | Pass | See below |

**Isolation.** Session `a` set `lab38_a_secret` in its Python kernel and wrote `a-marker.txt` in its workspace and `marker-a.txt` in the project's shared area. Then:

| Session | Sandbox | Python kernel | `lab38_a_secret` | Shared marker | Session `a`'s file |
| --- | --- | --- | --- | --- | --- |
| `a` | `ss-4933…` (the project's) | `krn-a-python`, pid 874 | `'a-only'` | written | written |
| `b` (chat run) | `ss-4933…` | `krn-b-python`, pid 914 | not defined | read | `../a` is there: one container |
| `c` (dedicated) | `ss-a940…` | `krn-c-python`, pid 605 | not defined | read, synced across environments | absent; only `sessions/c` |

`down` suspended both sandboxes, the dedicated one too, and `status` listed both.

## Tokens

The project's daily budget counts MAIN: 812,342 tokens (804,965 in, 7,377 out) in 11 runs. The reviewer used 262,252 in 15 reviews. The whole lab used 1,074,594, more than the 1M aimed for.

| Run | Session | Calls | Input | Output |
| --- | --- | --- | --- | --- |
| ePrint download (403) | a | 4 | 34,948 | 319 |
| arXiv download, wrong `shared/` | a | 8 | 75,374 | 495 |
| `hostname` (after #297) | c | 2 | 16,901 | 58 |
| move, publish, read the PDF | a | 6 | 61,629 | 530 |
| Sage and Python | a | 10 | 105,257 | 1,028 |
| the document | a | 3 | 29,928 | 359 |
| the comment and the finding | a | 17 | 249,187 | 2,138 |
| ledger, first try | a | 7 | 89,857 | 1,290 |
| ledger with the IDs | a | 7 | 82,288 | 819 |
| establishing `R-1` | a | 3 | 33,659 | 205 |
| isolation check | b | 3 | 25,937 | 136 |

## Bugs

- **B1, high: one alias could not serve MAIN and the reviewer** ([#295](https://github.com/regenscheid/sandbox-crypto/pull/295)). With `reviewer` equal to `name`, as the owner's decision has it, the run service exited 2 at `up`: `--reviewer-model must be a separate model alias`. The watcher had the same refusal.
- **B2, high: a dedicated session could not chat** ([#297](https://github.com/regenscheid/sandbox-crypto/pull/297)). The run service admitted every run as one of the project's environment, `open_session` refused the mismatch, and the message stayed "Queued" while the runs failed.
- **B3, medium: code and the file tools meant different `shared/`** ([#298](https://github.com/regenscheid/sandbox-crypto/pull/298)). Code starts in the session's workspace, so a relative `shared/` there is the session's own folder, which the file, PDF and publish tools cannot reach and the recorder does not record. The executor sets `$PROJECT_SHARED`, but no tool said so.
- **B4, medium: the agent could not answer a finding** ([#299](https://github.com/regenscheid/sandbox-crypto/pull/299)). `review_respond` gave its evidence and revisions as bare objects and arrays, and a refusal said only "invalid finding response".
- **B5, medium, reported: the ledger entry view shows no evidence or findings.** `GET …/ledger/R-1` returns both evidence items and `findings`, and `LedgerEntryView` renders neither. To see it, open any entry with evidence.
- **B6, medium, reported: the agent cannot find an artifact's ID after the run that published it.** Ledger evidence needs the inventory's `artifact-…` ID and revision. Only `publish`'s result names them, and a later run gets earlier tasks and answers without tool results. To see it, publish a file in one run, then ask another run to cite it in the ledger.
- **B7, medium, reported: a published file can be missing from Results.** `write_file` recorded `results/curve25519-order.json` first, so `publish` found the same content and added only a reproduction (RA-9). The GUI lists it under All files as an execution record, not under Results (RA-37). In a project that records all files, this happens to every file an agent writes and then publishes unchanged. The spec has to say which wins.
- **B8, medium, reported: reviews rarely complete with the real model.** A review of a modest run spends its 64,000-token budget in 6 or 7 calls (the estimate runs about half again above the gateway's count), 60 calls an hour ran out in about 40 minutes, and a turn that breaks the response bound (at about 670 output tokens, only the limit of 16 tool calls fits) fails the review, and its retries failed the same way. A capped or failed review does not satisfy the gate's review clause.
- **B9, low, reported: the launcher's gaps.** It cannot open a session in its own environment, has no watcher alias, and does not set up the math checker, so the document's 13 formulas showed "renderer unavailable".

## Observations, not fixed

- **ePrint needs the MCP connectors.** Its PDFs sit behind a bot challenge for non-browser clients. The owner's ePrint service (item 9) is the route for papers; this lab had no MCP proxy.
- **The agent tried a browser user agent** against the challenge before it was told not to.
- **`crypto-base` has no `file` command;** two shell calls failed on it.
- **A decision can be in effect without the change it states.** `D-1` says to establish `R-1` and is in effect, while `R-1` stayed proposed. The agent read that as the gate's answer.
- **The gate names the clause but not the evidence item** that breaks it.
- **The GUI's "Not logged in" page** says to open the URL that `sandbox-science-workspace serve` printed; with the launcher, the address is in the private file.
- **OpenShell's `sandbox exec`** showed `/sandbox/project` without `sessions/`, while the executor's calls ran in `/sandbox/project/sessions/a`. What `exec` shows is not the executor's view of the files.

## Whether the M2 exit criterion is met

Not yet. Two workbooks shared the project's container with distinct kernels, a third ran in its own, and every step of the path ran with the real model. Missing:
- the paper came from arXiv, not ePrint, which needs the MCP connectors;
- #295 and #297 are needed for the setup to run at all, and #298 and #299 for the agent to use `shared/` and answer findings;
- reviews must complete (B8) for the gate's review clause to hold;
- the entry view must show evidence and findings (B5), and the agent needs a way to cite artifacts from earlier runs (B6);
- a comment addressed to an idle session should start a run (LD-22).

## Evidence and teardown

The evidence is kept outside git, in an archive on babbage (sha256 `1e414cc74517ff7a1be415223dcbc77cdb5d6ce45b9e947c0a63c69b6f4ffa0b`, 33 entries). It holds the config, the scripts, the notes, the command outputs, the isolation records, a copy of the control store and the parts' logs. It holds no tokens and no login address.

Teardown ran `down`, which suspended both sandboxes, then `lab_down.sh`, which deleted them, stopped the lab units and removed the lab directory. There was no forward unit. The launcher's state directory and the lab's checkout were then removed. Afterwards no lab unit, port, process, OpenShell path or driver socket was left. No image was changed.
