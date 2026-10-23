<!-- SPDX-License-Identifier: MIT -->
# OpenShell Lab41: what merged since lab 40, with the real model

Run on babbage on 2026-10-15 (UTC), from 17:16 to 18:41, against the OpenShell 0.1.3 lab that `.agents/labs/lab_up.sh` brings up, with the images `sandbox-science/minimal:02f85b3`, `sandbox-science/crypto-base:02f85b3` and `sandbox-science/mcp-proxy:e5e55c86` (the proxy's recipe has not changed since lab 40; nothing was built). Everything ran from main `326f4801`. The lab checked what merged since [lab 40](babbage-openshell-lab-40-2026-10-15.md): lab 40's fixes and the config script (#394, #397, #400 to #402, #404), general task delegation with tracks, issues, result files and depth 2 (#395, #396, #399, #405, #410), the reviewer's claimed-action check and its GUI (#406, #407, #409, #411), cost by role (#403, #408) and history keeping the user's words (#390, #393). Every role used `sandbox-orchestrator`, the weaker model the owner keeps for labs. Teardown was complete.

## Setup

- The config was written by `.agents/labs/launcher_config.py` with `--project lab41`, `--mcp-proxy` and `--provider-env-file`, naming the owner's Cloudflare Access file by its path alone. It had the `delegated` alias and the tool index's `index` and `about` without editing (lab 40's B7 holds). The script has no option for the profile or the sessions, so the project was edited to `crypto-base` and the dedicated `scratch` session dropped (B9, [#412](https://github.com/regenscheid/sandbox-crypto/pull/412)). The gateway `https://gateway.example.invalid/v1 [redacted in repository]` and the owner's key file came from the example; no daily cost limit.
- `up` ran in a `systemd-run --user` unit, with nvm's Node on its `PATH` for the math checker. It registered `crypto-research-mcp` from the owner's file.
- `project delegation --depth 2`: "main runs delegate, and the tasks they allow delegate once more"; at most 10 at once; a check 150,000 tokens and 12 turns, a task 200,000 and 20; no models to choose, no fallback.
- The GUI was the built-in browser pane, logged in through a one-shot loopback redirect. The pane was hidden, so the lab emulated a visible page, as labs 38 to 40 did.

## Results

| Step | Result | Evidence |
| --- | --- | --- |
| 1. A plan with tracks, depth 2 | Pass | Plan v1 with tracks `orders` and `source`, activated. `delegate` of a task on track `orders` with `delegate: true` (limits 20 turns, 200,000 tokens); the lead read its track (`track_read`: its two steps only), delegated the twist's order as a task without a track or `delegate`, computed the curve's order, and recorded both steps with `track_progress`. Details below |
| 2. A task on an issue, with a result file | Pass | `I-1` opened; `delegate` with `issue: I-1`. The task wrote and published `results/ell.json`, declared it, and its outcome pinned it (artifact r1, SHA-256, 733 bytes, content kept). The platform recorded the outcome on I-1 as a work record (`limit: tools`, since the task said `partial`), citing the delegation and the artifact revision with its path. The parent recorded `C-1`, `answers` I-1, with `exact_computation` evidence pinned to that revision. No dismissals or obstructions were declared |
| 3. A claimed action that never happened | Not exercised by the model; the check fails on history | The model made no false claim: it made both asked-for fixes (r2), answered the nudge "You also changed the title…" with "Not done", and made the asked-for one-digit typo with Python (r3). The actions table works: run 4's review shows its refused and its applied `document_patch`, with the blocks r2 changed. But from run 9 on, reviews raised four `missing_action` or `contradicting_value` fails on true statements of earlier runs, carried in each run's earlier conversation (B1, [#414](https://github.com/regenscheid/sandbox-crypto/pull/414)) |
| 4. Connector search | Pass, with a gap | `mcp_tools` queries list and load nothing: "search ePrint archive paper metadata full text" listed six e-print tools, `loaded: []`; a load by names took three. Lab 40's queries ("ledger change", "ledger_change ledger_frontier") find nothing. No `shell` or `admit_hosts` before the first connector call. Run 1 had no connector tools at all: its listing failed once upstream (B4) |
| 5. Cost by role | Pass | The project page: "$0.26 … The main agent: $0.11 … Delegated runs: $0.02 … The reviewer: $0.13 … The job watcher: $0.000044". Every reviewer and watcher call kept its cost |
| 6. History | Pass | Run 8's prompt (its record's `messages`) holds every earlier message of the user verbatim, the comment as "(A comment addressed to you on the document doc-…:)" and its body, and run 7, started for the comment alone, shows it in place of the stand-in task. Run 8's second answer quoted both word for word |

**Step 1: the run tree** (GUI, while running and after): "Main run run-ca26…", under it "Delegated run run-0d8d…: … limits max_turns 20, max_input_chars 400000, tokens 200000", "Delegation dlg-8736…, a task", and under that "Delegated run run-2bff…: … limits max_turns 18, max_input_chars 400000, tokens 182612": three levels. The grandchild's limits were what the lead had left. Its tokens: "4 model calls (sandbox-orchestrator), 32,747 input and 1,197 output tokens, $0.0039" in the session's run view. The grandchild had 42 tools, without `track_*` or the delegate tools; the lead had 48, with them. The plan showed "2 of 3 steps completed", both by a progress record whose author is the delegated run with track `orders` and the plan v1 grant. Handbacks went to the direct parent: the grandchild's outcome was acknowledged by the lead's run and the lead's by the main run. The model never tried to give the grandchild a track, so the refusal ("this run's tasks take no track") was not exercised. The tree shows a task as a check (B6, [#416](https://github.com/regenscheid/sandbox-crypto/pull/416)).

**Step 3: the actions table** (review of run 4, read whole): "Run run-6be6… | 1 document_patch, document curve25519-orders, refused, nothing | 2 document_patch, document curve25519-orders, ok, document … r2", then "r2 since r1: b2, b4". The reviewer raised `artifact_disagreement` on the deliberate typo (the document's ℓ against run 1's answer), the agent "fixed" it, and a confirmation review confirmed the fix.

## Tokens and cost

| Run | What it did | Calls | Input | Output | Cost |
| --- | --- | --- | --- | --- | --- |
| 1 | plan, lead on track `orders`; no connectors (B4) | 19 | 342,142 | 1,485 | $0.0311 |
| 1's lead | track, sub-task, curve order | 7 | 66,492 | 799 | $0.0070 |
| lead's sub-task | twist order | 4 | 32,747 | 1,197 | $0.0039 |
| 2 | I-1, task on it, C-1 | 8 | 140,195 | 1,113 | $0.0097 |
| 2's task | `results/ell.json` | 10 | 95,570 | 2,051 | $0.0071 |
| 3 | paper tools, the document, plan finished | 18 | 371,188 | 1,041 | $0.0201 |
| 4 | the two fixes | 7 | 121,833 | 602 | $0.0044 |
| 5 | the nudge | 5 | 86,403 | 119 | $0.0039 |
| 6 | the one-digit typo | 7 | 127,600 | 371 | $0.0046 |
| 7 | the comment; the typo undone after a finding | 10 | 194,814 | 935 | $0.0080 |
| 8 | quote the comment; stopped by the plan (B2) | 2 | 30,865 | 277 | $0.0009 |
| 9 | a job, not waited; false findings retracted (B1) | 8 | 155,495 | 465 | $0.0065 |
| 10 | a waited job, the watcher; false findings (B1) | 18 | 423,442 | 1,391 | $0.0171 |

MAIN used 2,001,776 tokens (1,993,977 in, 7,799 out, $0.1064) in 10 runs, at the 2 million aimed for; runs 9 and 10, answering false findings, took 579,402. Delegated runs used 198,856 ($0.0180), the reviewer 1,329,164 in 101 calls and 16 reviews ($0.1321: 2 passed, 2 inconclusive, 12 with findings), the watcher 211 in one call ($0.0000435). The whole lab: 3,530,007 tokens, $0.2565.

## Model errors, and whether the platform caught them

| Error | Caught |
| --- | --- |
| The sub-task's first answer added fields (`exact_sage_code`, `exact_sage_output`) | Yes: the one correction; the second answer validated |
| The task copied the file's SHA-256 with a digit dropped, into a ledger call; the parent copied the same typo into C-1's evidence | Yes: both refused by the schema; the parent retried without the hash and the platform pinned the right one. The typo stays in the task's summary and in I-1's work-record note |
| The task wrote to the ledger unasked: `{"code": …}` to `ledger_change`, a `produced` work record naming nothing | Yes: each refused |
| The task called itself `partial` with an obstacle | Yes: refused ("only a blocked task names its obstacle"), corrected |
| The task reported `partial` for a complete computation, because its unasked ledger writes failed | No: I-1's record reads "Ran out of the tools" |
| Run 1 left the source step `in_progress` | No (run 3 marked it blocked) |
| Run 3 never tried the paper-search tools; a long query found none | No |
| Run 3 marked the delegated runs' correct orders "unverified here" after the reviewer's warning (B5) | No |
| Run 7 undid the user's deliberate typo after a `fail` finding, and wrote that `results/ell.json` "concerns a separate subgroup-order task" | No: the confirmation review confirmed the "fix" |
| Run 8 said it did not have the user's words, which its prompt held | Only by chance: the plan's veto discarded the answer (B2) |
| Runs 9 and 10 retracted true statements under false findings | No |
| The reviewer read 16 or 50 characters of every item | Partly: both reviews ended inconclusive, not passed (B3, [#415](https://github.com/regenscheid/sandbox-crypto/pull/415)) |
| A reviewer said run 10's waited job showed no completion, though the wait's result (succeeded, "done") was in its window | No: `fail`, unaddressed |

## Bugs

- **B1, high: the claimed-action check flags true statements of earlier runs** ([#414](https://github.com/regenscheid/sandbox-crypto/pull/414)). The review controller observed every message of a run's prompt, the earlier conversation included, as the run's own transcript. Each earlier answer came into every later window again, while the window's actions list only its own calls. Run 9's review raised `missing_action` on "Updated curve25519-orders…" (run 4's answer, whose patch made r2) and `contradicting_value` on "pinned to `results/ell.json` (artifact revision 1)" (run 2's); run 10's raised two more. The agent retracted them. The fix observes from the first message after the earlier conversation.
- **B2, medium, reported: a finished plan still vetoes later answers.** Every new run starts with `reread_required` while the session has any plan, finished ones included. Run 8 answered without `plan_read` (the user had said "without tools"); the hook retried with no new notice, since the plan notice had not changed, and then stopped the run with no answer as `user_message`. The GUI says "The run ended its wait for a message." This is the documented reread fence; a finished plan holding it, and the silent retry, look unintended.
- **B3, medium: `review_read`'s `start` and `count` are undescribed** ([#415](https://github.com/regenscheid/sandbox-crypto/pull/415)). Two reviews passed 50 and 16, the manifest's page size and the calls a turn runs, read that many characters of each item, and ended inconclusive.
- **B4, medium, reported: one failed listing leaves a run without connectors.** Run 1's listing got `upstream_error` on `initialize` (1.6 s) at 17:19:55, between two of the operator's listings that worked. The run had no `mcp_tools`, nothing told the model, and it admitted `cr.yp.to` and tried `curl`. The next run's listing worked. The listing could be retried once, or `mcp_tools` offered with the server's error.
- **B5, medium, reported: a run's review does not see its delegated runs' work.** The main run's answer gave orders its children computed; its review held only their returned summaries, warned that "the snapshot evidence does not include a successful Sage computation", and the agent then marked the correct orders unverified in its document. Lab 40's B3 brought a delegation's outcome in only when ledger evidence pins it.
- **B6, low: the run tree shows a task as a check** ([#416](https://github.com/regenscheid/sandbox-crypto/pull/416)): "Verdict: ; status complete", "did not read the entry at its revision", and no context, track, issue, `delegate`, dismissals, obstructions or result files.
- **B7, low, reported: a waited job's end never reaches the review.** The execution item stays `queued`: `wait_jobs` is a read, which the actions leave out, and the wait does not record the execution's end. The waited job's completion was then judged unsupported (one reviewer error, two rebuttals citing the start).
- **B8, low, reported: the comment form's text.** "An idle session keeps it for its next manually started run", but a work request starts a run (RS-6b); the comment started run 7 at once.
- **B9, low: the config script sets no profile or sessions** ([#412](https://github.com/regenscheid/sandbox-crypto/pull/412)): it adds `--profile` and `--session`.

## Observations, not fixed

- **The copied-number check stayed silent on the typo**: the window held document r3 but not `results/ell.json`, which run 2 made and the document names only in its text. The reviewer caught the typo by comparing with run 1's answer instead.
- **Plan progress reads "Updated by sandbox-orchestrator"** whether the main run or the lead recorded it; the record says which.
- **The reviewer now costs more than MAIN** ($0.13 against $0.11), most of it on the false findings of runs 9 and 10.
- **A kernel check failed once** with "kernel observation capacity is busy", while three runs shared the sandbox; no notice was lost that the lab saw.
- **`mcp_tools` matching is strict for long queries**: "Find conference paper bibliographic record / DOI by title, author Bernstein…" listed nothing, though the paper-search tools fit.
- **The paper is not on ePrint** (PKC 2006), so the source step could not succeed with the e-print tools alone.

## Whether what merged works

Mostly. Depth 2 works as built: three levels, the lead's track and its progress on the plan, the grandchild without a track or delegation, limits that shrink down the tree, and handbacks to the direct parent. A task on an issue leaves its work record there, its declared file is pinned with the right hash even when the model mistypes it, and the parent can cite the pin. `mcp_tools` lists without loading, lab 40's stray calls are gone, every role's cost is counted, and history keeps the user's messages and comments verbatim. The claimed-action check is the exception: its table works, but with history in every prompt it raises false `fail` findings on true statements, which the model then retracts. #414 should merge before the check is relied on; #415 and #416 are small.

## Evidence and teardown

The evidence is kept outside git, in an archive on babbage (sha256 `8684ba4f2f7cd2d7a782a501fbe57974d2b2827c53a7aa7882caa2a3262f7e19`, 53 entries): the config as written and as run, the lab's policy, the helper scripts, the notes, the run transcripts, run 8's prompt, the token report, the proxy's records, the GUI's texts, the operator's connector checks, a copy of the control store (no signed URLs were in it) and the parts' logs. It holds no tokens, no login address, nothing from the owner's secrets directory and nothing of the gateway's database.

Teardown ran `down`, which suspended the research sandbox and deleted the proxy's; the launcher unit then stopped. The research sandbox was deleted, and `lab_down.sh` stopped the lab units and removed the lab directory (14 GB), with the gateway's database, its key and the registered provider in it. The launcher's state and config directories were removed. Afterwards no lab unit, port, process, OpenShell path or driver socket was left, and no image was added.
