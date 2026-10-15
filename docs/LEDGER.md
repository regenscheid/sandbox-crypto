# Project ledger

The project ledger as built, against its specification, [the ledger spec](specs/ledger.md). It records the project's research state as typed entries in the control store (`orchestrator/ledger/`).

## What is built

**Entries (LG-1, LG-2).** An entry has an ID (`entry-…`), a type, a handle unique in its project (`I-3`, `C-12`, `REF-2`: one counter per project and type), and, for claims and results, its origin (`project` or `published`). The type, handle and origin never change. Each revision holds the title, the status, the statement, the type's own fields and the outgoing links, with who made it, the reason, the operation ID, and where it came from when it was imported (LG-41). The seven types and their statuses:

| Type | Handle | Statuses | Fields |
| --- | --- | --- | --- |
| `issue` | `I-` | `open`, `resolved`, `obstructed`, `split`, `withdrawn` | none |
| `claim` | `C-` | `proposed`, `established`, `recorded`, `disputed`, `refuted`, `superseded`, `withdrawn` | `kind` (lemma, theorem, observation, counterexample, negative result, other) |
| `result` | `R-` | as a claim | `kind`, `category`, `conditions`, `measures` |
| `assumption` | `A-` | `active`, `withdrawn` | none |
| `decision` | `D-` | `proposed`, `in_effect`, `rejected`, `reversed` | none |
| `task` | `T-` | `todo`, `in_progress`, `blocked`, `done`, `dropped` | none |
| `reference` | `REF-` | `active`, `withdrawn` | `identifiers` (DOI, ePrint, arXiv, NIST, ISBN, URL), `authors`, `published`, `version`, `retrieved`, `pdf` (an artifact revision, pinned by its SHA-256); its statement is the citation text |

A new entry starts in its type's first status: `proposed` for a project claim or result, `recorded` for a published one. A reference names at least one locator (an identifier or its PDF, which must be a revision of an artifact of the project; the link records the revision's SHA-256, and a hash the change gives must be that one) and a date: published, versioned or retrieved (LG-15). An entry's fields are at most 64 KiB as UTF-8 JSON, and a result's measures are finite numbers or text.

**Statements.** A statement is Markdown with LaTeX, saved in the dollar form (LD-42). Its revisions are `text_revisions` of kind `statement` under the entry's ID, with their block map and block owners, as comment bodies and explanations keep theirs; an entry revision names the statement's text revision, and a revision that leaves the statement unchanged names the same one.

**Math checks (LG-1, LD-53 to LD-55).** Each new text revision of a statement is queued for the math check documents, comments and explanations have ([math checks](MATH-CHECKS.md)), with the project's macros, since a statement belongs to no document; a project macro change checks it again. A formula KaTeX rejects becomes a `math` finding anchored to the entry's statement at that text revision, for the session of the agent that last changed the formula's block, with the repair as its remedy; text the user wrote gets none. The agent fixes it with `ledger_change`, and the next revision's check confirms the finding fixed, or confirms it when the formula is gone. A revision that keeps the statement is not checked again.

**Links (LG-5).** A revision's links replace the revision before's when a change gives them, and are kept otherwise; a change that gives the links the entry already holds changes nothing. Each kind has the types that may hold it and the targets it may name:

| Kind | Held by | Points to |
| --- | --- | --- |
| `answers` | claim, result | issue |
| `assumes` | claim, result, decision | assumption, pinned to a revision (when none is named, its revision as the change leaves it) |
| `builds_on` | claim, result, issue | claim, result |
| `restates`, `supersedes` | claim, result | claim, result |
| `compares_to` | claim, result | claim, result, with the stated difference |
| `split_into` | issue | issue |
| `affects` | decision | any entry |
| `concerns` | issue | any entry, a review finding of the project, or a document block at a revision |

An entry the same change revises is linked as the change leaves it, so an assumption revised and pinned in one change is pinned at its new revision (LG-F5). A finding link reads with the session the finding is in now, since the workspace opens a finding in its session and a math finding can move to another. A block link keeps the document's ID even when the change names the document, since a name can later name another document. An entry's current revision also shows the links other entries' current revisions hold to it, 200 at a time in the order those entries were made, with their count.

**Evidence (LG-8 to LG-11).** A claim, result or decision holds evidence links. Each has a role (`supports`, `contradicts`, `derivation`, or `independent_check` with the revision of the entry it checked), a type (`proof`, `exact_computation`, `finite_test`, `numerical_experiment`, `source_claim`, `synthetic`; `unclassified` is for imports), a scope note saying what it covers, and one pinned target, checked to exist in the project: an entry revision (its revision as the change leaves it by default), an artifact revision with its SHA-256, whose bytes the control store keeps as it captured them (a tombstone or a revision that keeps only the sandbox's hash pins no content, and neither is a reference's PDF), a document block or math span at a revision (by the document's ID, even when the change names it), or a review. The target is the platform's record; the role, type and scope are the author's assertion. A change adds links (`evidence`) and removes them by their IDs (`evidence_removed`); a revision keeps the links of the one before, each with its ID and the revision that added it, whose author added it. An entry revision's scope notes are at most 32 KiB in all. A claim or result with no evidence links shows as asserted only (`asserted_only`, in lists too). Execution targets, PDF anchors and delegation outcomes are refused until they are built.

**Published claims and results (LG-15).** A published claim or result starts `recorded` and cites its reference through a `supports` link of type `source_claim` to a reference entry, which may be one the same change records; a change without one, or one that removes the last, is refused.

**Decisions (LG-4, LG-30).** An entry becomes `established`, `obstructed` or `refuted`, and a decision `reversed`, only in a change that puts in effect a decision that `affects` it: a new decision recorded `in_effect` (by the user or the main agent; a delegated agent's decision is `proposed`), or a proposed one moved to `in_effect`. Only a project claim or result is `established`. While an entry is `established`, `refuted` or `obstructed`, its statement, fields, `assumes` links and evidence change only with its status, or in a change that puts in effect a decision that affects it, which for the main agent's establishing passes the gate again. A proposed decision may be `rejected`; a decision in effect is reversed by a new decision, which states its own changes, and nothing returns to a status by itself; a rejected or reversed decision stays so. Other status changes need no decision.

**The gate (LG-17).** The user's decision establishes a project result as it is. The main agent's needs the gate to hold (the reviews below decide its review and finding clauses), and a refusal (`policy_denied`, `gate_not_met: …`, with the entry and the clause in its details) names the first clause that does not:

| Clause | Holds when | Pending, and why |
| --- | --- | --- |
| `evidence` | a `supports` or `derivation` link whose type is neither `synthetic` nor `unclassified`, to anything but a review or an entry that is `refuted`, `withdrawn` or `superseded` | No record says which of a target's inputs were synthetic (RA-17), so the link's type alone is read |
| `independent_check` | when evidence that counts is a `proof`, an `independent_check` the user added themselves at the revision they checked, of a revision whose statement, fields, `assumes` links and evidence that bears on it (`supports`, `derivation`, `contradicts`) the change keeps; the user records one with the workspace's check or `sandbox-science-harness ledger check` | Checks by delegations and verification reviews are not built, so only the user's count |
| `computation` | computational evidence (`exact_computation`, `finite_test`, `numerical_experiment`) pinned to an artifact revision | Execution targets are not built, so the producing execution is not pinned |
| `review` | a completed checkpoint or terminal review (`passed` or `findings`) whose coverage was complete held, whole, in its snapshot a revision of the entry whose statement, fields, `assumes` links and evidence that can count the change keeps; an entry shortened to fit one review item is not held whole | None |
| `finding` | no `fail` finding anchored to the entry is unresolved: open, claimed fixed or unaddressed, or superseded until a completed review that started after the supersession has covered the entry as the change leaves it (a confirmed or withdrawn one is resolved) | None |
| `flags` | the entry has no open flag | Flags are not built, so this clause holds |

The main agent's decision to make an issue `obstructed` needs an assessment that accepts the claimed obstruction (LG-7), which work records will carry; until they are built it is refused (clause `assessment`), and only the user's decision makes an issue obstructed. A project setting that reserves decisions to the user is not built; by default it reserves none.

**Reviews (CR-2, CR-9, CR-10).** The session's consistency reviews read the ledger: each revision an agent of the session makes is an observation of kind `ledger`, the entry as it reads at that revision (statement, fields, links, evidence, author and reason), which the reviewer reads as it reads other evidence. The revision's new title and statement count toward the 2,000-character trigger, and a new claim or result is a structural block; the user's revisions are not reviewed. The reviewer reads the ledger's events in pages of 200 at each safe point, and drains them before a terminal review; a review's coverage is complete only when it has read them all. A finding anchors to an entry revision, and to one of its fields (`title`, `statement`, `status`, `fields`, `links` or `evidence`) when it concerns one (LG-25, CR-17); an entry's read lists the findings anchored to it, math findings on its statement among them, each with its own status, which a change to the entry never resolves. When the next review is dispatched, an anchor counts as replaced only when a later revision changed what a decision rests on (the statement, fields, `assumes` links and evidence that can count), and, when it names a field, that field; a new title, status or other links replace nothing, and an open finding whose anchors are all replaced becomes `superseded` (CR-28 as amended).

**Mutations (LG-30, LG-31).** A change names an operation ID, a reason, and up to 32 entry changes, each creating an entry (its type, title, statement, fields and links) or revising one (its ID or handle and the revision it expects). Entries the same change creates are named `new:<key>`. All of it commits, with its events and its receipt, or none of it does. A stale expected revision is a conflict that names the current one; nothing is rebased. An operation ID names one change in its project, whoever sends it: the same operation ID with the same change returns the first result (`replayed`), and with another change is refused; a change is compared by a digest of its JSON without the fields it leaves unset, so a field a later version adds does not turn a retry into a conflict. An entry is `superseded` exactly while a current revision supersedes it, so a supersedes link and its target becoming `superseded` commit together, and so do the link's removal and the target's leaving `superseded`; an entry that is `superseded` supersedes nothing new, though one that supersedes another may later be superseded. An issue is `split` exactly while it holds `split_into` links, so it names the issues it was split into in the same change. Neither `supersedes` nor `split_into` links ever lead back to their holder.

**Events (LG-33).** Each new revision records `ledger.entry_created` or `ledger.entry_revised` (with the status before and the parts changed) in the control store's event log, under the entry.

**Who may change what (LG-13).** The user may make any change; the main agent may create and revise any entry; a delegated agent may create entries, in their first status, and add evidence to entries, but change nothing else. Other agent roles, the platform, imports and code in the sandbox change nothing; refusals say `not_permitted_for_role`. An agent's session must belong to the project, and an entry of another project is not found.

**The agent's tools.** A run reads with `ledger_list` (by type or current status, a page at a time), `ledger_read` (an entry at a revision, with its links and the links to it) and `ledger_history`, and changes with `ledger_change`, a mutation whose operation ID defaults to the tool call's. The reads are `read` tools; `ledger_change` and `ledger_import` are effects, which the plan fence gates. Every change is the run's: its session, role (`main`, or `delegated` for a delegated run), model, alias and tool call; the arguments name no author, and each tool refuses arguments it does not take. Every answer fits the tool's output budget (20,000 characters): a long list or history comes a page at a time, and a long entry in parts, its statement, links, evidence and incoming links each going on where `more` says, with notes and scopes shortened. Entry text and results files are untrusted data, and the system prompt says so.

**Results files (LG-41).** Code in the sandbox has no route to the ledger. It writes a results file, JSON Lines with one mutation per line (its operation ID, reason and changes), at most 256 lines and 1 MiB, and the agent imports it with `ledger_import`, naming its path in the workspace and the SHA-256 it read; the import is refused if the file is no longer what the agent read, or if the bytes the worker sent do not match the digest it sent with them. Lines end at a newline only; a line repeats no key and holds no NaN or infinity. Every line is checked as a mutation before anything is recorded; the mutations are then applied in one change, all or none, with the agent's authority, and a refusal names its line. Each revision records the file's path, the SHA-256 of the bytes applied, the session's environment and the line; a line imported before replays its first result, so importing a file again, from any session of the project, records nothing. The answer counts the lines recorded and replayed and lists the first 20, with the handles each made.

**The workspace's reads.** Three read-only routes answer typed models (`LedgerListing`, `LedgerEntry`, `LedgerHistory` in `api-types.ts`) for the GUI's ledger and board views: a page of entries, an entry at a revision, and an entry's history ([workspace](WORKSPACE.md)). The views' own tables of the types' statuses, the link kinds and the field names are tested against `web/workspace/src/lib/ledger.fixtures.json`, which `scripts/generate_ledger_fixture.py` writes from `ledger/models.py`; a workspace test fails when it is stale.

## Not built yet

- The gate's pending clauses (see the table above); the project setting that reserves decisions to the user (LG-13); evidence pinned to executions, PDF anchors and delegation outcomes (LG-8).
- `dominates` links and the frontier (LG-18 to LG-24); flags (LG-27 to LG-29); work records (LG-6).
- The GUI's frontier and project-wide history views, its indicators beside a status (open findings and flags), moving cards; the pack's import and the export (LG-34 to LG-39); naming the execution that wrote a results file. The board, the issue list and the entry view with its history are built ([workspace](WORKSPACE.md#views)).

## Tables

| Table | Holds |
| --- | --- |
| `ledger_entries` | Each entry's ID, project, type, handle number, origin, current revision, and its position in the order entries were made |
| `ledger_revisions` | Every revision: title, status, statement's text revision, fields, author, reason, operation ID, provenance and time; written once |
| `ledger_links` | Each revision's outgoing links, in order: kind, target (an entry, a finding or a document block), pinned revision, block and note; written once |
| `review_finding_entries` | The ledger entries each review finding is anchored to, kept by triggers as findings are written and their anchors change |
| `ledger_evidence` | Each revision's evidence links, in order: ID, role, type, target (an entry, an artifact, a document block or a review), pinned revision, block, math span, an artifact's SHA-256, the revision an independent check examined, scope note and the revision that added it; written once |
