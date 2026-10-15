# Project memory

Date: 2026-10-14. **Status: Draft; awaiting the owner.** The owner chose the approach (option (a), 2026-10-14): one project memory document per project, a living document whose agent changes wait as suggestions, its current text given to each run, and no ledger `lesson` type. The choices still open are listed under [open questions](#open-questions), each with a recommendation.

## Scope

Project memory is what the project has learned about how to work, kept so that later runs start from it: conventions and notation, settled facts about the environment and tools, the user's preferences, and pitfalls to avoid. For example: "Write finite fields as `GF(p)`, never `F_p`", "Install Python packages with `provision`; `pip` inside a cell does not survive a restart", "The NIST CSRC server rate-limits after 20 requests a minute".

It is not a place for results or evidence: those are artifacts, documents and, later, ledger entries ([research artifacts](research-artifacts.md), [living documents](living-documents.md), [ledger](ledger.md)). It is not a ledger entry type either; the ledger gets no `lesson` type (owner, 2026-10-14).

**What is built today.** Living documents, with revisions, comments and an optional switch that makes agent patches wait as suggestions for the user to accept or decline (LD-20). The GUI accepts suggestions; declining one is a harness command (`document decide … decline`). No document is given to a run: a run starts from its system message, its task and the session's earlier conversation ([run service](run-service.md), RS-10).

**What this adds.** One memory document per project, which agents can change only by suggestion, and whose accepted text every main and delegated run receives at its start.

## Behavior

- PM-1. **The document.** Each project has one project memory: a project-scope living document named `memory` and titled "Project memory", made with the project, and for an existing project when it is first opened after this is built. It cannot be deleted, renamed or moved to a workbook. Everything else about it is an ordinary living document: Markdown with LaTeX, blocks, revisions, comments (LD-1 to LD-21).
- PM-2. **Who changes it.** The user edits it directly, as any document. An agent cannot change it: its suggestion switch is always on and cannot be turned off, so every agent patch on it waits as a suggestion (LD-20) until the user accepts or declines it, one by one. An accepted suggestion becomes a revision attributed to the agent run that proposed it, with the user's acceptance recorded; a declined one writes nothing.
- PM-3. **How an agent proposes.** With the existing `document_patch` tool, naming the memory's ID and the revision it read (`document_read`), as for any document. The tool's result says the patch is waiting for the user. Agents propose memory entries when they learn something later runs should know, and say why in the patch's reason; what kinds of change they may propose is open question 3.
- PM-4. **Who receives it.** Each main run and each delegated run receives the memory's text at its start (open question 2). The consistency reviewer and the job watcher do not: the reviewer judges claims against evidence, and memory is not evidence.
- PM-5. **How it is given.** The text is a section of the run's system message, after the main prompt and before the planning guidance, framed as the project's notes that the user accepted. It is guidance, not permission: it grants no tool, path, network access or approval, and the project policy applies as before. The run record pins the section like the others ([MAIN prompt provenance](../MAIN-PROMPT-PROVENANCE.md)): the document's ID and revision, the included text's byte count and SHA-256, and whether it was cut (PM-6).
- PM-6. **Bound.** At most 16,000 characters of memory (about 4,000 tokens) are given. A longer memory is given whole blocks from its start up to the bound, followed by a line saying how many blocks were left out and that the memory is over its bound. The GUI shows the memory as over its bound, with its size. Nothing is cut from the document itself; the user, or an agent's suggestion that the user accepts, shortens it.
- PM-7. **Which revision.** A run receives the revision current when it starts, and keeps it to its end; a suggestion accepted during a run reaches the next run. A delegated run receives the revision its root run started with, so the runs of one tree work from the same memory.
- PM-8. **What is never given.** Pending and declined suggestions, comments, earlier revisions and the document's history are never part of a run's context. An agent reads them only with the document tools, as data (LD-23).
- PM-9. **In the GUI.** The project page links to the memory and shows how many suggestions wait. The memory opens as a document, with each waiting suggestion's text, the run that proposed it and its reason, and the user accepts or declines each one there. Declining in the GUI is built with this.
- PM-10. **No file in environments.** Unlike other documents (LD-3, LD-49), the memory is not written into any environment's `documents/` area, so no code in a sandbox can edit it as a file, and no direct edit (LD-50) can reach it. Runs receive its accepted text in their context (PM-5), and agents read it with `document_read`. A file a call writes at the memory's path, `documents/memory.md`, is an ordinary file of that environment, not the memory. *Rejected alternative:* syncing the file and turning edits of it into suggestions. A direct edit records the environment and possibly the execution, but not reliably which run or call made it (LD-10), so the user could not see who proposed the change; and any code in the sandbox, including a package or a downloaded script, could then queue suggestions for the user to review, which only agents' tool calls should do.

## A worked example

1. Project `ff` is made; its memory is empty apart from its title.
2. A run spends three calls finding that `pip install` inside a Sage cell is lost when the kernel restarts. It proposes, with `document_patch` on the memory, a block: "Install Python packages with `provision`, not `pip` inside a cell (lost on restart)", with the reason "learned in run r-12 after a kernel restart".
3. The run ends. The user opens the project page, sees "1 suggestion waiting", reads it and accepts it. The memory is now at revision 2, attributed to run r-12 and accepted by the user.
4. A later run proposes "Always use 128-bit floats". The user declines it; nothing is written.
5. The next main run's system message includes the memory at revision 2, and its record pins it. A run it delegates gets the same revision 2, even if the user accepts another suggestion meanwhile.
6. Months later the memory is 20,000 characters. Runs receive its first 16,000 characters of whole blocks and a line saying 6 blocks were left out; the GUI shows the memory as over its bound. An agent proposes merging old entries; the user accepts, and the next run receives all of it again.

## Contracts used

- Living documents' `Mutation`s, patches, suggestions and their events (`document.suggestion_created`, `document.suggestion_decided`), unchanged.
- `PromptPins`: one more section, `memory`, versioned by the document's ID and revision.

## Acceptance fixtures

**PM-F1 Agents only suggest** (`scripted`; PM-2, PM-3)
- *When* an agent patches the memory, and a request tries to turn its suggestion switch off or delete it.
- *Then* the patch waits as a suggestion and the document is unchanged; the switch and the deletion are refused.

**PM-F5 No file to edit** (`worker`; PM-10)
- *Given* a project whose documents are synced into its environment (LD-49).
- *When* a call lists `documents/` and then writes `documents/memory.md`.
- *Then* the listing has no memory file; the written file changes no document and creates no suggestion; the memory is unchanged.

**PM-F2 Accepted text reaches the next run** (`scripted`; PM-5, PM-7, PM-8)
- *Given* a memory at revision 2 with one suggestion waiting.
- *When* a main run starts, the user accepts the suggestion during it, and the run delegates a child.
- *Then* the scripted model's first request holds revision 2's text and not the suggestion; the child's holds revision 2 too; the run record pins revision 2; the next main run holds revision 3.

**PM-F3 Bound** (`scripted`; PM-6)
- *Given* a memory of 20,000 characters in 40 blocks.
- *When* a run starts.
- *Then* its system message holds whole blocks up to 16,000 characters and a line naming the number left out, and the record's pin says it was cut.

**PM-F4 Roles** (`scripted`; PM-4)
- *When* a main run's answer is reviewed and a job's end is watched.
- *Then* the reviewer's and the watcher's requests hold no memory text.

## Open questions

1. **The bound.** *Recommended:* 16,000 characters, cut at whole blocks with a line saying how many were left out (PM-6). A larger bound costs every run more context; a smaller one fills sooner.
2. **Which roles receive it.** *Recommended:* main and delegated runs, not the consistency reviewer or the job watcher (PM-4). Giving it to the reviewer would let unverified notes count as evidence.
3. **What agents may propose.** The owner asked for additions. *Recommended:* any change, as a suggestion (adding, correcting or removing entries), since the user decides each one and stale entries otherwise stay until the user edits them. The alternative refuses agent patches on the memory that change or remove existing blocks.
4. **Locking.** *Recommended:* the memory's suggestion switch is always on, and the memory cannot be deleted, renamed or moved (PM-1, PM-2), so no run can switch off the user's review.
5. **Declining in the GUI.** *Recommended:* built with this (PM-9), since today the GUI can only accept suggestions.

## Sources

The owner's decision of 2026-10-14 (option (a)), [living documents](living-documents.md) (LD-20, LD-22, LD-23), [MAIN prompt provenance](../MAIN-PROMPT-PROVENANCE.md), [plans and delegation](plans-and-delegation.md) (PD-32, PD-34), [threat model](../THREAT-MODEL.md).
