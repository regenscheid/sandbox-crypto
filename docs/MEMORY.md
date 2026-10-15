# Project memory

The project memory as built, against its specification, [project memory](specs/project-memory.md). It keeps what a project has learned about how to work, as short entries in the control store (`orchestrator/memory.py`), which agents propose and the user decides.

## What is built

**Entries (PM-1).** A project's memory is a list of entries, each with an ID unique in its project (`mem-1`, `mem-2`: one counter per project), its text and a revision. Text is Markdown with LaTeX, 1 to 1,000 characters after surrounding white space is taken off; control characters other than tabs and line breaks are refused, and so are the bidirectional overrides and isolates (U+202A to U+202E, U+2066 to U+2069), which would show the user other text than an agent receives. A memory holds at most 100 entries; to add more, the user removes or merges some. Every change of an entry appends a version, written once (triggers refuse updates and deletions): its revision, what changed (`added`, `edited`, `removed` or `moved`), the text, the entry's place, who applied it and when, and the proposal it came from, if any, whose record says who proposed it (the run, its role and session, the model alias and the model that answered), when and why. A removed entry keeps its history. The memory's **version** counts the changes of its accepted entries; each change raises it by one, and a decline leaves it.

**Order (PM-7).** The entries are in the Memory page's order: oldest accepted first, unless the user moved one. A move swaps an entry with the one before or after it, and appends a version to both. Every version keeps the entry's place, so the memory at an earlier version can be rebuilt in its order then, for an agent that must work from the same memory as the run it belongs to (PM-8).

**Proposals (PM-2, PM-3).** An agent run proposes one action with a reason of 1 to 500 characters: `add` with text, `change` with an entry, the revision it read and the new text, or `remove` with an entry and the revision it read. A proposal against an entry that changed since, or was removed, is refused, naming the current revision. A proposal waits (`prop-1`, `prop-2`: one counter per project) until the user accepts or declines it; nothing an agent does changes an entry. A project holds at most 50 waiting proposals and a run makes at most 10, waiting or decided; beyond them a proposal is refused, saying the user has not caught up. A project keeps at most 10,000 proposals and 50,000 changes, its history included.

**Decisions (PM-2, PM-4).** Only the user changes the memory: accepting a proposal applies it as a new version, attributed to the proposing run with the user's acceptance recorded; declining applies nothing. Accepting a change or a removal names the entry's revision the user saw beside the proposal: when the entry changed after the proposal was made, the user has seen the current text, and the accepted change becomes the next revision with both authors in its history (PM-F6). The user also adds, edits, removes and moves entries directly, naming the revision seen; a stale revision is a conflict that names the current one, and an edit that keeps the text changes nothing and is refused.

**Changes and events (PM-11).** Every proposal, decision and direct change is one change of the control store, with its operation receipt (scope `memory`; an agent's receipts are its run's, the user's are the user's) and its events on the event log (subject `memory`): `memory.proposed` and `memory.decided` under the proposal, with the proposing session, and `memory.changed` under the entry. A retry with the same operation ID returns the first result, and the same ID with another request is refused. The tables check their values when they are written (control store version 22).

**The workspace (PM-4).** `GET /api/v0/projects/{project}/memory` answers the entries in order, the version, their characters, how many of them an agent would receive within 16,000 characters (PM-7) and how many proposals wait; `GET …/memory/proposals` the waiting proposals, oldest first, each with the entry it would change as the entry is now; `GET …/memory/{entry}/history` an entry's versions, newest first, 50 at a time before `?before=`; `POST …/memory` the user's change, under the workspace's same-origin guard ([workspace](WORKSPACE.md)).

## Not built

- The agent tools `memory_list` and `memory_propose` (PM-3).
- The memory section of each agent's system message and its pin in the agent's record (PM-5 to PM-9).
- The GUI's Memory page and the count of waiting proposals on the project page (PM-4).
- Memory across projects (PM-12), which is not part of the spec.
