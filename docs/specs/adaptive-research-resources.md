# Adaptive research resource policy

Status: implemented locally (A8, [D102](../DECISIONS.md)): soft caps on tokens, turns,
active time and cost are check-ins, loop heuristics ask for an early check-in, the
user token approvals of D87 are removed, and the web app shows the usage ([usage
view](#usage-view)). Retained-artifact allowances and hard retention
capacity keep their earlier behavior. Runtime qualification with a real model is pending.

## Purpose

Extended investigations should not be paused because a number was reached, and stuck
work should not run on unnoticed. A soft cap catches stuck work without pausing
productive work: at a cap the work checks in, and whoever owns it decides. Spending is
enforced by LiteLLM's budget for the key, user or team, and by a configured local
project limit only if an operator sets one ([run service](run-service.md), RS-6a); no
soft cap, check-in or decision here refuses a model call or Resume. Tool deadlines,
security policy and physical capacity remain separate, hard constraints.

## Soft caps

Every work with durable records (a root or a delegated child) has four soft caps, each
cumulative across its attempts and each a project delegation setting:

| Cap | Default | Step | Ceiling |
| --- | --- | --- | --- |
| Tokens (input plus output, folds included) | a check's or task's `check_tokens`/`task_tokens`, 20,000,000; a root's `root_tokens`, 100,000,000 | 20,000,000 | `max_token_grant` (1,000,000,000) |
| Turns (foreground model calls; an unknown dispatch counts, folds do not) | a child's `check_turns`/`task_turns`, 300; a root's run `max_turns`, 2,000 | a child's 300; a root's, a block as large as its limit, accepted by the user | a child's `turn_ceiling` (10,000); a root has none |
| Active time (observed seconds, waiting for a decision excluded) | `research_seconds`, 14,400 | 3,600 | `time_ceiling_seconds` (604,800) |
| Cost (known journaled cost plus explicit reservations; unpriced calls add nothing) | `soft_cost`, 25.00 in the gateway's currency | 25.00 | none |

A root's initial caps are pinned when first observed, so later default changes do not
move them; a child's come from its immutable blueprint. `research_seconds` and
`soft_cost` may be turned off (`--no-research-time-limit`, `--no-soft-cost`). A child's
only turn limit is `turn_ceiling`, cumulative across attempts. A root's turn limit is its
run's `max_turns` (2,000 by default; `serve --max-turns N` and `run --max-turns N` mean
"ask at N", not "stop at N"), a soft cap like the others, with one difference: at it the
root does not extend itself but pauses and asks the user ([the root](#check-ins)). The
runaway guard ([run service](run-service.md), RS-6f) still stops a run that repeats itself,
and the model gateway's budget still bounds spending; no further hard turn limit applies
to a root. Only a run without durable records (`run` without a state directory) has none
to pause on, so it keeps `max_turns` as a hard stop with its report round.

Resume deducts nothing and grants nothing: the caps follow the work, and usage records
(the model-call journal, fold calls and reservations) are kept as before.

## Check-ins

Before each model call the resource hook reads the caps and recorded use in one
transaction. At a cap, or when a loop heuristic tripped, the work records one check-in
(a `resource_request` observation, identified by resource, cap revision and attempt),
with a bounded state summary: the work's brief, up to 12 things it produced (artifact
revisions its attempts published, children that completed), its last eight tool calls
(tool and whether it failed), the head of its latest answer, the loop rule if any, and
its recorded tokens, turns, active seconds, cost and evidence counter.

**A child** then waits at that safe point without model calls. Its check-in reaches its
direct parent as a notice, wakes the parent's delegation or message wait, and makes a
parent that is closing take another turn. The parent answers with `resource_decide`:

- `extend` gives one bounded step of the cap, or lets a loop check-in go on;
- `redirect` sends the reason as the child's corrected instruction, with one step;
- `stop` makes the child's next call its report round (`resource_report`); an answer it
  already gave and held for another turn stands as its report instead.

`continue` is refused for a reached cap, which would not move it. The older actions
(`extend_turns`, `extend_time`, `extend_storage`, `extend_tokens`, `extend_cost`,
`report`, `pause`) are kept for earlier records and callers. After `max_idle_extensions`
(3) extensions in a row without new evidence, `extend` is refused: the parent must
stop or redirect, and a redirect starts the count again. Evidence is the work's counter:
a child's published artifact revisions and completed children; a root's, the session's
artifact revisions, plan progress records and completed descendants. Model calls and
resource decisions are not progress.

A parent that does not answer within 30 minutes gets the root's rule applied for it: one
automatic step while the child has evidence or extensions left, otherwise a stop. A
parent that needs a decision it cannot make escalates with `message_parent`, or, at the
root, asks the user.

**The root** decides its own check-ins. While it records new evidence, or has idle
extensions left, the platform extends it by one step, tells the root model to compare its
progress with the session plan (continue only with work that advances it, revise the
plan, or finish with a report), and tells the user with a notice in the session's chat:
a notice, not a question. After `max_idle_extensions` extensions in a row without new
evidence, the root pauses (`resource_pause`) and the user is asked: the resource panel
offers one step (`extend`, an audited USER decision against the exact paused attempt and
allowance), and Resume continues. The opt-in `root_resource_renewal` still lets the root
model decide its own check-in with `resource_decide` after saving a checkpoint and new
evidence.

**The root's turn limit** is the user's to accept (the owner, 2026-10-24). Reaching it
records a check-in (resource `turns`, rule `turns`, the root's use, latest answer and
evidence counter, with a `step` equal to the configured limit) and pauses the run without
further model calls (`turn_limit`); the session's chat shows a notice and a card. The user
answers on the card:

- **Continue** accepts one more block of `step` turns (2,000 by default), recorded as an
  audited USER `extend` against the exact paused attempt and allowance, and the approved
  tree resumes automatically as for any approved turn extension. No ceiling bounds the
  total; the card asks again at the end of each block, whatever the evidence.
- **Stop** ends the research as an explicit USER Stop: the tree is `stopped`
  (`user_stop`) with a recorded cancel, so Resume and automatic recovery leave it alone.

An explicit Resume of a root at its limit is the same acceptance of one block, recorded as
the user's decision on the new attempt's check-in. The check-in is a durable record: the
card survives a restart and shows as historical once a newer attempt exists.

Decisions are idempotent under the tool operation. They require the current direct
parent, recipient attempt and session epoch; stale or competing decisions are refused, a
child cannot decide its own check-in, and explicit Stop wins. If a decision commits but its
tool response is lost, Resume restores that response from the original receipt without
deciding again; automatic recovery admits the pending tool only with that receipt. A USER
approval receipt cannot substitute for an agent's tool decision.

## Loop heuristics

Cheap counters over what the run already records ask for an early check-in, which is a
loop check-in (resource `loop`) with its rule:

| Rule | Trips at |
| --- | --- |
| `same_call` | 8 calls in an attempt of one tool with the same arguments, other than the tools that watch something change (`wait_jobs`, `wait_delegations`, `delegation_status` and the like) |
| `same_error` | 6 failed calls in an attempt with the same error record |
| `no_progress` | 30 model calls in a row with no new evidence counter, nor a new distinct successful call |
| `same_output` | 4 answers in a row with the same text: a child whose output stopped changing |

A loop check-in changes no cap; its answers are those of any check-in, and the counters
start again after it. The runaway guard still stops a run that repeats itself 25 times in
a row (RS-6f).

## Accounting

Spend and tokens per project, session, run and role, the reviewer's included, are
recorded and read through the usage view ([run service](run-service.md), RS-6g; [below](#usage-view)). The
reviewer's calls are accounted under their own role; a review has no token budget by
default (`--review-token-budget` is an operator's optional cap), and no reviewer cost
stops anything but the gateway's budget. Optional project cost notices record when the
project's recorded cost first crosses each threshold; they never stop.

## Time, retained artifacts and capacity

Active time is observed active-attempt elapsed seconds, including model, tool and child
wait time, excluding time spent waiting for a check-in decision. Durable safe-point
samples accumulate across attempts; paused or offline time is excluded, and unobserved
time before a process loss is not invented. The allowance never changes execution
deadlines, transport timeouts, worker leases, Stop propagation or security policy.

Retained artifact bytes (`storage_bytes`, off by default) are logical retained revision
bytes attributed to the work across its attempts. Reaching them pauses the work at the
next safe point (`resource_storage`); a parent or the user may extend by 1 GiB within
`storage_ceiling_bytes`, and an approved extension qualifies for automatic root Resume.
Project and global artifact retention bytes, metadata, revision and operation quotas
produce nonrenewable capacity notices and stop the work (`resource_retention`); they
remain hard boundaries that only an operator changes. No filesystem scan is run on the
host.

## Operator controls

```sh
sandbox-science-harness project delegation --state-dir STATE --project PROJECT
sandbox-science-harness project delegation --state-dir STATE --project PROJECT \
  --root-tokens 100000000 --task-tokens 20000000 --soft-cost 25 \
  --research-seconds 14400 --max-idle-extensions 3 --turn-ceiling 10000
sandbox-science-harness project budget --state-dir STATE --project PROJECT \
  --notify-at 50,100,250
```

`--no-soft-cost`, `--no-research-time-limit` and `--no-storage-limit` turn the optional
caps off; `--no-notify` removes the notices; `project budget --daily-cost-limit AMOUNT`
sets the optional local limit, which the model-call journal alone enforces.

## Usage view

The workspace shows the accounting and the check-ins in one read-only view
([workspace](../WORKSPACE.md#views)), over records control already keeps; it decides,
extends and stops nothing.

- A session's Usage tab reads `GET …/sessions/{session}/usage/view`: the session's spend
  and tokens by role and by model; by agent, one entry for each work across its attempts
  with the reviewer's calls under the run they reviewed, beside the work's four soft caps
  and what it has used against each; the latest check-ins; and counts of the session's
  check-ins by resource, loop rule, who answered and the answer's action.
- The project's Usage page reads `GET …/usage/view?window=24h|7d|all`: the window's use
  beside the project's whole-life use, its sessions by spend with their check-ins and loop
  detections in the window, the optional local cost limit and the notices.
- The soft caps are read without sampling or pinning anything: a child's token and turn
  caps are its blueprint's, the others the allowance pinned when the work first reached a
  safe point (else the settings' value), and each is raised by the recorded extensions. A
  root's turn limit is unknown until its first safe point pins it. A work's token use is
  its own calls and folds, as its cap counts it; its cost use also counts the calls made
  for it, such as the reviewer's.
- A cost the gateway did not price is shown as unknown, or as unpriced calls beside a
  known part, never as zero.

## Not built

The loop heuristics and soft caps have been exercised with scripted models only; the
check-ins have not been qualified with a real model.
