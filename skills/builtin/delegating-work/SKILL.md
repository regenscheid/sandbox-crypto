---
name: delegating-work
description: Starting delegated runs, either a check of one ledger entry that can count as its independent check or a task with its own brief, track or issue, and reading their outcomes. Use it before calling delegate, or when a delegated result comes back.
---

# Delegating work

A main run is offered `delegate`, `wait_delegations`, `delegation_status` and `cancel_delegation` once the project's delegation setting allows it. A delegated run starts with a fresh context: it sees none of your conversation, only its brief and the project memory, and it has kernels of its own. It works in the same session, so it reads and writes the session's files. It cannot ask the user anything: when it needs an answer, it ends blocked with the question among its open questions.

## Checks and tasks

A brief is a check or a task.

A check examines one ledger entry at one revision:

```json
{"check": {"entry": "R-4", "revision": 5},
 "note": "The proof's script is scripts/verify.sage; its output is results/verify.json."}
```

The platform writes the check's task, and lists the entry's evidence as its inputs. Your note, one line of at most 500 characters, reaches the checker marked as the requester's note, not as evidence. Use it to say where things are, never which verdict you expect. A check reads the ledger, the documents and the memory, and computes; it changes nothing.

A task is yours to write: what to do and what to return, the context it needs, and the inputs it should read (workspace paths, documents, ledger entries):

```json
{"task": "Run scripts/bias.py for 2^20 samples and report the mean bias with its interval.",
 "context": "The cipher variant is the one in document notes, block b12.",
 "inputs": ["scripts/bias.py", "notes"], "issue": "I-2"}
```

A task can also name:

- a track of the session's active plan: the child reads its steps with `track_read` and records progress on them with `track_progress`; a track has one running task at a time;
- an issue: the task's outcome, the lines of work it dropped and the obstructions it claims are recorded there as work;
- delegate: true, where the project allows two levels, so it may start tasks of its own once more.

A task works with a delegated agent's authority. It can create ledger entries in their first status and add evidence and work records, but it changes no status. A task never counts as an entry's independent check.

## When a check counts

The gate counts a delegation as an entry's independent check only when all of these hold:

- it was a check, so the platform wrote its task, and you added at most a note;
- it ran in a fresh context, in a run that added none of the entry's proofs;
- it returned confirmed and complete, having examined everything the entry rests on;
- it read the entry whole at that revision with `ledger_read`, and each proof's target whole: an entry with `ledger_read`, a document block with `document_read`, an artifact's whole file in one `read_file` call;
- no plan fence refused its calls;
- no other check of the same material returned refuted, or inconclusive from a complete check, or is still running.

A proof file too long to show in one read, about 20,000 characters, cannot be read whole, so its check cannot count: keep proof files compact. Once the delegation has ended, a main run of the session links it as evidence on the entry:

```json
{"role": "independent_check", "type": "proof", "delegation": "<delegation ID>", "checked": 5,
 "scope": "Re-derived the bound and recomputed Table 2"}
```

## One verdict per material

A check that comes back refuted or inconclusive is information, not an obstacle. Running checks again until one says confirmed does not work: while the entry keeps the material a check disputed, no delegation's check counts, and only the user's own check does. Read the refutation and the coverage, then fix the entry or its proof, which makes new material to check, or take the disagreement to the user.

## Reading outcomes

`delegate` returns a delegation ID at once, and the child runs while you go on. Its end arrives as a delegation notice, or as the result of `wait_delegations`, which keeps the turn open until all (or the first) of the named delegations end, or a user message arrives. `delegation_status` reads a running delegation's limits, or an ended one's whole outcome. `cancel_delegation` stops one, keeping its record.

An outcome separates what the platform saw from what the child said:

- how it ended: returned, failed, cancelled, limit_reached or interrupted;
- its result: a status (complete, partial or blocked) and a summary; for a check, the verdict and coverage; for a task, its deviations, open questions, dismissals and obstructions;
- the files its calls recorded;
- for a task, its result files: the files it named as its results, each pinned to the revision its own calls made. Cite those pinned revisions rather than the paths, which can change later.

The result is the child's claim. Weigh it like any other evidence, and check what matters.

## Limits

- You cannot give your final answer while a child runs, and the end of your run cancels its children.
- The project's cap bounds how many delegated runs the session runs at once, across the whole tree.
- Checks and tasks have turn and token limits from the project's setting, and a child never gets more turns than its parent has left.
- There are at most two levels. A task allowed to delegate starts tasks, never checks, and on no track; its own children delegate nothing.
- A delegated run has no connector, review, plan or delegate tools (unless allowed), and no `ask_user`. Search the literature in the main run, and pass what a task needs as files.
