<!-- SPDX-License-Identifier: MIT -->
# Archived design pages

These pages describe code that was removed from main on 2026-10-14. Nothing in production called it. The code, its tests and these pages are kept on git tags as reference for the rebuild. See the [refactoring plan](../../refactor-plan/PLAN.md), decision 2.

## Delegation

Tag `archive/delegation-2026-10`. Delegation will be rebuilt as a role of the one agent loop (refactoring plan, Phase 4 item 7).

- [Delegation records](DELEGATION-RECORDS.md): briefs and handbacks.
- [Child attempts](CHILD-ATTEMPTS.md): the child turn journal.
- [Delegated child controller](DELEGATED-CHILD-CONTROLLER.md): the restricted child conversation loop.
- [Delegated plan progress](DELEGATED-PLAN-PROGRESS.md): a child's progress on its own plan track.
- [Delegated run transport](DELEGATED-RUN-TRANSPORT.md): the worker's selected-run HTTP API and its client.
- [Run kernels](RUN-KERNELS.md): the worker's run/kernel bindings.
- [Run-kernel dispatch](RUN-KERNEL-DISPATCH.md): the worker's child-kernel dispatcher.

## Document sync and exports

Tag `archive/document-sync-2026-10`. To be ported to the control store's public API (B-01) when LD-49 to LD-52 are scheduled.

- [Document exports](DOCUMENT-EXPORTS.md): export-input snapshots.
- [Worker document source](WORKER-DOCUMENT-SOURCE.md): captured worker text as named patches.

The control store's publication outbox and the manual catalog delivery journal had no pages of their own. They were described in sections of `docs/LIVING-DOCUMENTS.md`; the version of that page on the tag keeps the text.
