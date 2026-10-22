# Ledger ownership

The public `Ledger` facade remains the entry point for reads, category definitions,
mutations and fold acceptance. This refactor changes internal ownership, without
changing stored schemas, operation fingerprints, result shapes or authorization.

## Boundaries

- `Ledger` alone opens control-store transactions. An `apply` batch has one write
  transaction, including replay lookup, every mutation and its `within` callback.
- Queries accept the caller's SQLite connection and return values. They never
  acquire a transaction or hold a store/facade reference.
- `MutationInput` carries the active connection, project, normalized mutation,
  author, authorized role, resolved block IDs and import provenance. Preparation
  resolves entry identities, expected revisions, links and evidence into a
  `PreparedMutation`, which explicitly names its entries and new-entry keys.
  Provenance resolution follows the structural rules, preserving refusal order.
- Domain rules evaluate the prepared mutation against the transaction's current
  state and return an `EvaluatedMutation` with the entries that passed the gate.
  They retain the existing evidence, flags, gate, frontier and provenance
  implementations. Decision session membership reads the project's `sessions`
  rows on the supplied connection, without opening another project-store reader.
- Commit writes revisions, statements, links, evidence, flags, work, frontier
  history, events and the replay receipt on the supplied connection. Post-write
  validations remain inside this transaction, so a refusal rolls all of them back.

Preparation for a later mutation in a batch sees the earlier mutation's writes.
A replay skips preparation and commit but still participates in the batch's
`within` callback. The callback runs once, after all results, including an empty
batch; its exception rolls back the whole batch and its own writes. No collaborator
opens another transaction, commits, rolls back or calls the public facade.

Document block mappings are resolved before the write transaction as today:
immutable document revisions can be read from their own storage. The resolved
block IDs are explicit mutation inputs, never ambient mutable ledger state.

## Verification

Public characterization tests cover callback visibility, rollback of revisions,
flags, work, frontier history, events and receipts, retry after rollback, replay
without duplicate writes, several mutations sharing the transaction, and rejection
of a missing session or a session belonging to another project. Existing
ledger suites retain authority, provenance, evidence, cycles and concurrent-writer
coverage. Fold-proposal acceptance retains its existing public tests. Read-only
fixture writers must remain alive while immutable WAL readers use their sidecars;
production's WAL guard remains unchanged.

There are no open behavior decisions and no migration or runtime dependency.
