# Project ledger

The project ledger as built, against its specification, [the ledger spec](specs/ledger.md). It records the project's research state as typed entries in the control store (`orchestrator/ledger/`).

## What is built

**Entries (LG-1, LG-2).** An entry has an ID (`entry-…`), a type, a handle unique in its project (`I-3`, `C-12`, `REF-2`: one counter per project and type), and, for claims and results, its origin (`project` or `published`). The type, handle and origin never change. Each revision holds the title, the status, the statement and the type's own fields, with who made it, the reason, the operation ID, and where it came from when it was imported (LG-41). The seven types and their statuses:

| Type | Handle | Statuses | Fields |
| --- | --- | --- | --- |
| `issue` | `I-` | `open`, `resolved`, `obstructed`, `split`, `withdrawn` | none |
| `claim` | `C-` | `proposed`, `established`, `recorded`, `disputed`, `refuted`, `superseded`, `withdrawn` | `kind` (lemma, theorem, observation, counterexample, negative result, other) |
| `result` | `R-` | as a claim | `kind`, `category`, `conditions`, `measures` |
| `assumption` | `A-` | `active`, `withdrawn` | none |
| `decision` | `D-` | `proposed`, `in_effect`, `rejected`, `reversed` | none |
| `task` | `T-` | `todo`, `in_progress`, `blocked`, `done`, `dropped` | none |
| `reference` | `REF-` | `active`, `withdrawn` | `identifiers` (DOI, ePrint, arXiv, NIST, ISBN, URL), `authors`, `published`, `version`, `retrieved`, `pdf` (an artifact revision); its statement is the citation text |

A new entry starts in its type's first status: `proposed` for a project claim or result, `recorded` for a published one. A reference names at least one locator (an identifier or its PDF, which must be a revision of an artifact of the project) and a date: published, versioned or retrieved (LG-15). An entry's fields are at most 64 KiB as UTF-8 JSON, and a result's measures are finite numbers or text.

**Statements.** A statement is Markdown with LaTeX, saved in the dollar form (LD-42). Its revisions are `text_revisions` of kind `statement` under the entry's ID, with their block map and block owners, as comment bodies and explanations keep theirs; an entry revision names the statement's text revision, and a revision that leaves the statement unchanged names the same one.

**Mutations (LG-30, LG-31).** A change names an operation ID, a reason, and up to 32 entry changes, each creating an entry (its type, title, statement and fields) or revising one (its ID or handle and the revision it expects). All of it commits, with its events and its receipt, or none of it does. A stale expected revision is a conflict that names the current one; nothing is rebased. The same operation ID with the same change returns the first result (`replayed`), and with another change is refused; a change is compared by a digest of its JSON without the fields it leaves unset, so a field a later version adds does not turn a retry into a conflict.

**Events (LG-33).** Each new revision records `ledger.entry_created` or `ledger.entry_revised` (with the status before and the parts changed) in the control store's event log, under the entry.

**Who may change what (LG-13).** The user may make any change; the main agent may create and revise any entry; a delegated agent may create entries, in their first status, but revise none. Other agent roles, the platform, imports and code in the sandbox change nothing; refusals say `not_permitted_for_role`. An agent's session must belong to the project, and an entry of another project is not found.

## Not built yet

- Links between entries, and to findings and document blocks (LG-5), with the statuses that come with them (`superseded`, `split`).
- Statuses a decision sets (`established`, `obstructed`, `refuted`), a decision's own `rejected` and `reversed`, decisions' status changes, evidence links and the gate (LG-4, LG-7, LG-8 to LG-17); published claims and results, which cite their reference through evidence (LG-15).
- `dominates` links and the frontier (LG-18 to LG-24); flags (LG-27 to LG-29); work records (LG-6).
- Math checks of statements (LG-1, LD-53).
- The agent's tools, results files (LG-41), the workspace's routes and views, the pack's import and the export (LG-34 to LG-39).

## Tables

| Table | Holds |
| --- | --- |
| `ledger_entries` | Each entry's ID, project, type, handle number, origin, current revision, and its position in the order entries were made |
| `ledger_revisions` | Every revision: title, status, statement's text revision, fields, author, reason, operation ID, provenance and time; written once |
