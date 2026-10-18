# Adaptive research resource policy

Status: implemented for durable token/turn check-ins, parent turn decisions and
explicit USER token renewal; the extended policy below also covers observed active time,
retained artifacts and operator-enabled root renewal.

## Purpose

Extended investigations should preserve useful work when an initial allowance is
reached. Resource check-ins give a parent enough evidence to continue, redirect,
request a report or pause a child. They are not scientific completion. Token grants,
project spending controls, tool deadlines, security policy and physical capacity
remain separate constraints.

## Initial grants and cumulative accounting

New check and task admissions default to 20,000,000 tokens and 300 model turns.
Explicit existing project settings keep their values. A delegated parent's children
still receive their tokens from its remaining allocation. Foreground input/output
and folding tokens count toward the same cumulative token grant; Resume
deducts earlier attempts rather than creating a fresh grant. Unknown model charges
remain visible and do not alone refuse local admission. The upstream LiteLLM budget
is the primary spending control; configured local observed-plus-reserved caps remain.

Foreground turn usage follows the logical work across attempts. A canonical model
intent and its archived response count once; an unknown foreground dispatch counts,
and auxiliary folds do not. Legacy attempts without dispatch intents use archived
response counts. The ordinary final report round remains separate from the loop's
research-turn allowance and still passes the existing token/cost admission gates.

The project `turn_ceiling` (default 10,000) bounds parent-approved cumulative turns.
An explicit low initial `check_turns` or `task_turns` is retained. A parent may grant
up to 300 more turns per check-in, never above that ceiling. This does not add tokens,
change a deadline or increase the parent's own allowance. Root turn allowance remains
an operator setting; the root may renew only when the explicit root policy below permits it.

## Durable check-ins and parent decisions

At 80% of a cumulative token or turn allowance, a safe point records one resource
request for that work, grant revision and attempt. Exhaustion records a separate
request, including when a large next-call estimate caused an early stop. A new grant
supersedes both older requests. The record keeps
the authoritative work and attempt, epoch, measured usage, granted total, bounded
original task and the latest archived message ID. The checkpoint reference identifies
available evidence, not a claim that every file is durably published.

Requests are stored in the existing control observation journal. The child and its
direct parent receive them as platform notices. A parent's delegation wait wakes
without making an extra model call. Requests are not repeatedly reoffered within the
same attempt. An agent's own warning never forces an extra final model call or wakes
its wait for descendants. After process loss, unresolved requests are offered again when their
current attempt is eligible; the original records remain inspectable.

The parent reads the checkpoint and supporting history, then calls `resource_decide`:

- `continue` retains the current allowance.
- `redirect` sends the reason as updated guidance while retaining the allowance.
- `report` uses the next eligible call for a final report and pauses that work; if
  the current call already returned an answer, it retains that checkpoint instead.
- `pause` stops at the next model safe point with its history and tool identities intact.
- `extend_turns` records a cumulative increase of up to 300 turns within project policy.

The decision requires the current direct-parent work, recipient attempt and session
epoch. It is idempotent under the original tool operation. Competing or stale decisions
are rejected. A child cannot decide its own request or grant itself tokens. An opted-in root may
renew its own soft allowance under the evidence requirements below. Decisions
are shown to the child at a safe point; `pause` and `report` do not cancel or replay an
in-flight worker operation. Explicit Stop remains authoritative.

A live child observes an approved turn increase before its next turn-limit check.
If it already paused at a turn limit, its parent can approve the request while still
running. Once the root pauses for unfinished children, the automatic recovery service
can queue the existing root Resume if the child has an approved remaining turn grant.
The same retry backoff, no-progress bound, token/cost checks and unresolved-tool-effect
refusals apply. Siblings and completed work are not redispatched.

## USER token approval

`TokenGrants.status` returns the cumulative grant, measured use, revision and exact
next 10,000,000-token increment, or a readable blocker with a typed ancestor work ID
when an ancestor needs approval first. The new authenticated USER
`renew_next` path permits exactly that increment. Each approval covers one increment;
another exhaustion needs another explicit approval. The legacy operator recovery API
remains compatible for previously scripted, explicitly authorized decisions.

Token approval requires a paused tree with no live lease or queued/running Resume,
current work/attempt/epoch/revision, a positive local project cap and a configured
`max_token_grant` ceiling (default 1,000,000,000). This ceiling is distinct from the initial
20,000,000-token allowance. Raising that administration ceiling is an explicit user
configuration change, within the schema ceiling of 1,000,000,000 tokens. Token
approval does not dispatch work; the existing root Resume
is the continuation path.

Nested work may be renewed when its ancestor allocation has room. Ancestor checks
include observed subtree use, including completed descendants, and outstanding sibling
allocations inside the same transaction. Only the immediate parent's commitment
increases; higher ancestors retain the existing allocation and are checked without
counting that increase twice.
An insufficient ancestor must receive its own explicit approval first; a nested
approval cannot silently enlarge it or allocate the same tokens to siblings. All
previous grant and request records remain available. The resource list projects the
later USER grant revision on the original token warning rather than inventing a
parent decision.

The USER resource list is bounded to 100 items per page, newest first, with a compound
time/ID continuation cursor. It distinguishes superseded attempts, current work state,
parent decisions and token requests resolved by a USER grant.

## Time, retained artifacts and root policy

The existing project delegation settings carry these operator controls (also exposed
by the supported `project delegation` CLI): `research_seconds` and `storage_bytes`
are optional initial soft allowances, disabled when omitted; `time_ceiling_seconds`
defaults to 604800 and `storage_ceiling_bytes` to 68719476736. A direct parent can
approve up to 3600 more seconds (`extend_time`) or 1073741824 more retained bytes
(`extend_storage`), bounded by those ceilings. Existing explicit settings are retained.

Time means observed active-attempt elapsed seconds, including model, tool and child
wait time. Durable safe-point samples accumulate across attempts; paused/offline time
is excluded. Unobserved time immediately before a process loss is not invented.
This allowance never changes execution deadlines, transport timeouts, worker leases,
Stop propagation or security policy. Reaching it pauses at the next safe point; an
in-flight operation keeps its original identity and is allowed to settle.

Storage means logical retained artifact revision bytes attributed to this work across
its attempts, including retained prior revisions. It does not measure unpublished
research-VM files, scratch disk, RSS or physical free space. Metadata-only artifact
records do not count as retained content. Existing project/global artifact retention
bytes, metadata, revision and operation quotas produce nonrenewable capacity notices;
these remain hard boundaries and require an operator to change capacity or retention.
No parent decision changes them, and no filesystem scan is run on the host.

`root_resource_renewal` defaults to false. Enabling it authorizes the root to use the
same `resource_decide` tool on its own turn/time/artifact request, within the existing
ceilings and increments. Each decision needs a checkpoint, a progress-based reason,
and a newer persisted evidence counter than its prior self-renewal of that resource.
The counter includes published artifact revisions, plan progress records and completed
children; model calls and resource-control calls alone are not progress. This bounds
no-progress renewal without claiming that a counter proves scientific novelty.
Root renewal never extends tokens. Explicit Stop or a lost ownership fence rejects it.

An authenticated USER can explicitly extend a paused root or child soft allowance in
the recovery UI. The decision is audited as an operator decision and requires the
same current work, attempt, epoch and allowance. The decision itself does not dispatch
work. An approved paused root or child turn/time/storage extension is eligible for
the existing bounded automatic root Resume path when all
other unfinished effects and budgets permit it. Hard capacity refusals are never
turned into automatic retries.

## Boundaries and qualification

Token approval remains exactly one explicit USER increment of 10,000,000 tokens.
Unknown model prices remain diagnostic under the upstream LiteLLM budget. Physical
worker/disk limits and security controls are not soft allowances. No resource decision
replays an uncertain operation. Real-model research quality and prolonged operation
need isolated runtime evidence in addition to deterministic regression coverage.

### Operator controls

Read or update the current project without changing existing research records:

```sh
sandbox-science-harness project delegation --state-dir STATE --project PROJECT
sandbox-science-harness project delegation --state-dir STATE --project PROJECT \
  --research-seconds 14400 --time-ceiling-seconds 604800 \
  --storage-bytes 1073741824 --storage-ceiling-bytes 8589934592 \
  --turn-ceiling 10000 --root-resource-renewal
```

Use `--no-root-resource-renewal`, `--no-research-time-limit`, or
`--no-storage-limit` to disable the corresponding optional policy. Initial soft
allowances are pinned per work item when first observed; changing an initial
value affects new work, while extension ceilings are checked against current
operator policy. Turning off an allowance disables its safe-point pause and
warnings; it does not discard the previous measured usage or audit decisions.
Hard retention settings and the worker's storage remain separate controls.

The first safe-point evidence counter prevents old results from previous work in
the same session from qualifying the root for a fresh allowance. Each subsequent
root extension requires another evidence increase for that resource. Parent
extensions remain explicit progress-based decisions. Neither path changes the
20,000,000-token default or the USER-only exact 10,000,000-token increments.
