# Project memory

Date: 2026-10-14. **Status: Approved (2026-10-14, by the owner). Built ([project memory](../MEMORY.md)): the store, the proposals and the user's decisions over the workspace API (PM-1, PM-2, PM-11, and PM-3's limits); the agent tools, the prompt sections and the Memory page are not.** The owner decided: a built-in memory tool with its own small store, not a living document; agents only propose and the user decides; every agent role receives the accepted entries; the memory is never a file in any environment; no ledger `lesson` type; and the three questions below, as recommended.

## Scope

Project memory is what the project has learned about how to work, kept so that later agents start from it: conventions and notation, settled facts about the environment and tools, the user's preferences, and pitfalls to avoid. For example: "Write finite fields as `GF(p)`, never `F_p`", "Install Python packages with `provision`; `pip` inside a cell does not survive a restart", "The NIST CSRC server rate-limits after 20 requests a minute".

It is not a place for results or evidence: those are artifacts, documents and, later, ledger entries ([research artifacts](research-artifacts.md), [living documents](living-documents.md), [ledger](ledger.md)). The ledger gets no `lesson` type (owner, 2026-10-14).

**What is built today.** Nothing: a run starts from its system message, its task and the session's earlier conversation ([run service](run-service.md), RS-10), and nothing the project learned carries over unless the user repeats it. The control store already has the pieces this builds on: operation receipts for safe retries and an event log committed with each change (`control/receipts.py`, `control/events.py`; refactoring plan, B-04).

**What this adds.** A short list of entries per project. Agents read the accepted entries and propose additions, changes and removals; every proposal waits for the user, who accepts or declines it on the GUI's Memory page and can also edit or delete entries directly. Every agent receives the accepted entries when it starts its work.

Terms:

- **Entry**: one short note of the project memory, with its own history.
- **Proposal**: an agent's request to add an entry, change one or remove one, with its reason; it waits until the user accepts or declines it.

## Behavior

- PM-1. **The store.** Each project has one memory: a list of entries in the control store. An entry has an ID (`mem-…`), its text (Markdown with LaTeX, at most 1,000 characters) and a revision number. Each change of an entry appends a version recording its text, who proposed it (the agent run, its role and session, the model alias and, when the gateway reports it, the resolved model), when and why, and who accepted it and when; or, for the user's own edit, the user and the time. A removed entry keeps its history. Versions are never changed or deleted.
- PM-2. **Agents propose; the user decides.** No agent changes the memory. An agent's proposal waits until the user accepts it, which applies it as a new version attributed to the proposing run with the user's acceptance recorded, or declines it, which applies nothing. A proposal to change or remove an entry names the revision it read; if the entry changed since, accepting it shows the user the current text first.
- PM-3. **The agent tools.** `memory_list` returns the accepted entries: ID, revision and text. `memory_propose` takes one action (`add` with text, `change` with an entry ID, its revision and the new text, or `remove` with an entry ID and its revision) and a reason of at most 500 characters, and returns the proposal's ID and that it waits for the user. A retry with the same operation ID returns the first answer, and the same ID with a different request is refused (operation receipts). A project holds at most 50 waiting proposals, and one run makes at most 10; beyond that `memory_propose` says the user has not caught up. Main and delegated runs have both tools; the consistency reviewer and the job watcher have neither.
- PM-4. **The user.** The GUI's Memory page lists the accepted entries and, apart, the waiting proposals, each with its text (for a change, beside the current text), the run that proposed it, its role and model, when, and its reason. The user accepts or declines each one, and edits, adds or deletes entries directly; an edit names the revision it saw, so concurrent changes conflict instead of overwriting. Each entry's history is one click away. The project page shows how many proposals wait.
- PM-5. **Every agent receives it.** Every agent role receives the accepted entries at the start of its work (owner, 2026-10-14): each main and delegated run at its start, the consistency reviewer with each review, and the job watcher with each check of a job. The watcher is called for each check of a running job, up to 60 times an hour per job by default, so the memory adds up to 16,000 characters (about 4,000 tokens) to each of those calls.
- PM-6. **How it is given.** The entries are a section of the agent's system message: for main and delegated runs after the main prompt and before the planning guidance, for the reviewer and the watcher after their own instructions. The section frames them as the project's notes that the user accepted: guidance, not permission (they grant no tool, path, network access or approval, and the project policy applies as before) and, for the reviewer, not evidence (a claim is not supported because the memory states it). Each agent's record pins the section, as other sections are pinned ([MAIN prompt provenance](../MAIN-PROMPT-PROVENANCE.md), [reviewer](../REVIEWER-PROMPT-PROVENANCE.md) and [watcher](../WATCHER-PROMPT-PROVENANCE.md) provenance): the memory's version (the sequence number of its last change), the entries included, their byte count and SHA-256, and whether any were left out (PM-7).
- PM-7. **Bound.** At most 16,000 characters of entries are given, in the order the Memory page shows them (oldest accepted first, unless the user moved one). When the accepted entries are longer, the agent receives whole entries up to the bound, followed by a line saying how many were left out. The Memory page flags the overflow with the total size, and the user removes or merges entries, directly or by accepting an agent's proposal.
- PM-8. **Which version.** An agent receives the memory as it is when it starts and keeps it to its end; a proposal accepted meanwhile reaches the next one. A delegated run receives the version its root run started with, so the runs of one tree work from the same memory. The reviewer receives the version the reviewed run started with; the watcher, the version current when it is called, since a job can outlive its run.
- PM-9. **What is never given.** Waiting and declined proposals and the entries' histories are never part of an agent's context. `memory_list` returns accepted entries only.
- PM-10. **Never a file.** The memory is not written into any environment, under `documents/` or anywhere else, so no code in a sandbox can read or change it as a file; agents use `memory_list` and `memory_propose` (owner, 2026-10-14). *Rejected alternative:* a file whose edits become proposals. A file edit records the environment and possibly the execution, but not reliably which run or call made it, so the user could not see who proposed the change; and any code in the sandbox, a package or a downloaded script included, could queue proposals, which only agents' tool calls should do.
- PM-11. **Changes and events.** Every proposal, decision and direct edit is one change of the control store, with its operation receipt and its events (`memory.proposed`, `memory.decided`, `memory.changed`) committed with it, on the B-04 event log. The GUI and the agents read through the store.
- PM-12. **Later: memory across projects.** The same entries and proposals can later be kept for a user across projects and given beside a project's memory, within the same bound. Not part of this spec.

## A worked example

1. Project `ff` is made; its memory has no entries.
2. A run spends three calls finding that `pip install` inside a Sage cell is lost when the kernel restarts. It calls `memory_propose` with `add`, the text "Install Python packages with `provision`, not `pip` inside a cell (lost on restart)", and the reason "learned in run r-12 after a kernel restart". The tool answers that proposal `prop-1` waits for the user.
3. The run ends. The project page shows "1 memory proposal waiting". The user opens the Memory page, reads it, with the run, its role `main` and its model, and accepts it. Entry `mem-1` exists at revision 1, attributed to run r-12 and accepted by the user.
4. A later run proposes the entry "Always use 128-bit floats". The user declines it; nothing changes.
5. The next main run's system message holds `mem-1` in the memory section, and its record pins the memory's version. A run it delegates gets the same version, even if the user accepts another proposal meanwhile. When the run's answer is reviewed, the reviewer receives `mem-1` as guidance, not as evidence. A job the run started, checked an hour later, has its watcher receive the memory as it is then.
6. The user edits `mem-1` on the Memory page to add "Use `provision` with `--channel conda-forge`". It is now at revision 2, attributed to the user.
7. Months later the entries total 20,000 characters. Agents receive whole entries up to 16,000 characters and a line saying 6 were left out; the Memory page flags the overflow. An agent proposes merging three old entries into one (one `change` and two `remove` proposals); the user accepts them, and the next agent receives all entries again.

## Contracts used

- Operation receipts and the event log of the control store (B-04), unchanged.
- `PromptPins`: one more section, `memory`, in the MAIN, reviewer and watcher records.
- Tool specs for `memory_list` and `memory_propose`.

## Acceptance fixtures

**PM-F1 Agents only propose** (`scripted`; PM-2, PM-3)
- *When* an agent proposes adding an entry, changing one and removing one, and retries the first with the same operation ID.
- *Then* three proposals wait, the retry returns the first answer, and `memory_list` returns the entries as they were; no agent tool changes an accepted entry.

**PM-F2 Accepted entries reach the next agent** (`scripted`; PM-6, PM-8, PM-9)
- *Given* a memory with one accepted entry and one waiting proposal.
- *When* a main run starts, the user accepts the proposal during it, and the run delegates a child.
- *Then* the scripted model's first request holds the one accepted entry and not the proposal; the child's holds the same; the run record pins that version; the next main run holds both entries.

**PM-F3 Bound** (`scripted`; PM-7)
- *Given* accepted entries totalling 20,000 characters.
- *When* a run starts.
- *Then* its system message holds whole entries up to 16,000 characters and a line naming how many were left out, and the record's pin says so.

**PM-F4 Every role** (`scripted`; PM-5, PM-6, PM-8)
- *Given* a memory at version 2.
- *When* a main run starts a job and delegates a child, the user accepts a proposal (version 3), the run's answer is reviewed, and the job is checked after the run ends.
- *Then* the main run's, the child's and the reviewer's requests hold version 2's entries, framed as accepted notes (for the reviewer, as guidance and not evidence); the watcher's request holds version 3's; each record pins the version it used.

**PM-F5 No file** (`worker`; PM-10)
- *When* a call lists the project root, `documents/` and the session workspace, and writes `documents/memory.md`.
- *Then* no listing shows the memory; the written file is an ordinary file that changes no entry and makes no proposal.

**PM-F6 The user edits** (`scripted`; PM-2, PM-4)
- *Given* an agent's proposal to change `mem-1` at revision 1.
- *When* the user edits `mem-1` directly (revision 2), then accepts the proposal.
- *Then* acceptance first shows the current text, and the accepted change becomes revision 3 with both authors in its history.

## Decided questions

1. **Sizes.** *Decided by the owner (2026-10-14), as recommended:* 1,000 characters an entry, 16,000 characters given to an agent (PM-1, PM-7). A larger bound costs every agent more context, the watcher on every check; a smaller one fills sooner.
2. **What is left out when the memory is over its bound.** *Decided by the owner (2026-10-14), as recommended:* the entries at the end of the Memory page's order, oldest accepted first, which the user can rearrange (PM-7). The alternative leaves out the oldest, which keeps recent lessons but drops the settled conventions that usually come first.
3. **Proposal limits.** *Decided by the owner (2026-10-14), as recommended:* 50 waiting proposals per project and 10 per run (PM-3), so that a run cannot bury the user in proposals.

## Sources

The owner's decisions of 2026-10-14, the refactoring plan (B-04), [living documents](living-documents.md) (LD-23, for treating other text as data), [MAIN prompt provenance](../MAIN-PROMPT-PROVENANCE.md), [plans and delegation](plans-and-delegation.md) (PD-32, PD-34), [consistency review](consistency-review.md), [threat model](../THREAT-MODEL.md).
