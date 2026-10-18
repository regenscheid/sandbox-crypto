# Adaptive research resource policy

Status: implemented for durable token/turn check-ins, parent turn decisions and
explicit USER token renewal. Time and storage renewal remain separate follow-ups.

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
an operator setting; the root cannot self-approve a resource request.

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
are rejected. A child cannot decide its own request or grant itself tokens. Decisions
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

## Boundaries and remaining qualification

No adaptive time or storage allowance is implemented here. Explicit deadlines,
transport timeouts, worker leases, security policy and actual disk/RAM capacity are
unchanged. Long admitted tools retain their original operation identity. The root
cannot autonomously renew its own turn ceiling. Decisions express a parent's judgment
of scientific progress; the platform does not infer novelty or productivity from
usage alone. Real-model scientific quality and prolonged renewable-grant operation
still need isolated live acceptance evidence beyond deterministic regression tests.
