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

**Mutations (LG-30, LG-31).** A change names an operation ID, a reason, and up to 32 entry changes, each creating an entry (its type, title, statement, fields and links) or revising one (its ID or handle and the revision it expects). Entries the same change creates are named `new:<key>`. All of it commits, with its events and its receipt, or none of it does. A stale expected revision is a conflict that names the current one; nothing is rebased. An operation ID names one change in its project, whoever sends it: the same operation ID with the same change returns the first result (`replayed`), and with another change is refused; a change is compared by a digest of its JSON without the fields it leaves unset, so a field a later version adds does not turn a retry into a conflict. An entry is `superseded` exactly while a current revision supersedes it, so a supersedes link and its target becoming `superseded` commit together, and so do the link's removal and the target's leaving `superseded`; an entry that is `superseded` supersedes nothing new, though one that supersedes another may later be superseded. An issue is `split` exactly while it holds `split_into` links, so it names the issues it was split into in the same change. Neither `supersedes` nor `split_into` links ever lead back to their holder.

**Events (LG-33).** Each new revision records `ledger.entry_created` or `ledger.entry_revised` (with the status before and the parts changed) in the control store's event log, under the entry.

**Who may change what (LG-13).** The user may make any change; the main agent may create and revise any entry; a delegated agent may create entries, in their first status, but revise none. Other agent roles, the platform, imports and code in the sandbox change nothing; refusals say `not_permitted_for_role`. An agent's session must belong to the project, and an entry of another project is not found.

**The agent's tools.** A run reads with `ledger_list` (by type or current status, a page at a time), `ledger_read` (an entry at a revision, with its links and the links to it) and `ledger_history`, and changes with `ledger_change`, a mutation whose operation ID defaults to the tool call's. The reads are `read` tools; `ledger_change` and `ledger_import` are effects, which the plan fence gates. Every change is the run's: its session, role (`main`, or `delegated` for a delegated run), model, alias and tool call; the arguments name no author, and each tool refuses arguments it does not take. Every answer fits the tool's output budget (20,000 characters): a long list or history comes a page at a time, and a long entry in parts, its statement, links and incoming links each going on where `more` says. Entry text and results files are untrusted data, and the system prompt says so.

**Results files (LG-41).** Code in the sandbox has no route to the ledger. It writes a results file, JSON Lines with one mutation per line (its operation ID, reason and changes), at most 256 lines and 1 MiB, and the agent imports it with `ledger_import`, naming its path in the workspace and the SHA-256 it read; the import is refused if the file is no longer what the agent read, or if the bytes the worker sent do not match the digest it sent with them. Lines end at a newline only; a line repeats no key and holds no NaN or infinity. Every line is checked as a mutation before anything is recorded; the mutations are then applied in one change, all or none, with the agent's authority, and a refusal names its line. Each revision records the file's path, the SHA-256 of the bytes applied, the session's environment and the line; a line imported before replays its first result, so importing a file again, from any session of the project, records nothing. The answer counts the lines recorded and replayed and lists the first 20, with the handles each made.

**The workspace's reads.** Three read-only routes answer typed models (`LedgerListing`, `LedgerEntry`, `LedgerHistory` in `api-types.ts`) for the GUI's ledger and board views: a page of entries, an entry at a revision, and an entry's history ([workspace](WORKSPACE.md)).

## Not built yet

- Statuses a decision sets (`established`, `obstructed`, `refuted`), a decision's own `rejected` and `reversed`, decisions' status changes, evidence links and the gate (LG-4, LG-7, LG-8 to LG-17); published claims and results, which cite their reference through evidence (LG-15).
- `dominates` links and the frontier (LG-18 to LG-24); flags (LG-27 to LG-29); work records (LG-6).
- The GUI's ledger, board, frontier, issue-list and history views, the pack's import and the export (LG-34 to LG-39); naming the execution that wrote a results file.

## Tables

| Table | Holds |
| --- | --- |
| `ledger_entries` | Each entry's ID, project, type, handle number, origin, current revision, and its position in the order entries were made |
| `ledger_revisions` | Every revision: title, status, statement's text revision, fields, author, reason, operation ID, provenance and time; written once |
| `ledger_links` | Each revision's outgoing links, in order: kind, target (an entry, a finding or a document block), pinned revision, block and note; written once |
