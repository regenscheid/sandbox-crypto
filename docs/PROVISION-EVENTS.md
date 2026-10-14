<!-- SPDX-License-Identifier: MIT -->
# Provisioning observations in the event store

The worker records three observational kinds of provisioning event in the policy middleware's [event store](EVENT-STORE.md#record-kinds) (the store its `event_store` setting names): `provision.requested`, `provision.awaiting_permission` and `provision.finished`. They implement part of [CP-16](specs/conda-provisioning.md). They add no route, setting, GUI, grant, install command, recovery decision or operator authority. The other CP-16 kinds (`environment.revision_created`, `environment.unmanaged_change`, `probe.finished`) are not written.

The source is `worker`. Access to the event store's file is the writer boundary; the label does not authenticate anyone. The observation writer (`worker.permissions`'s `observation`, with `worker.provision_events`) accepts only these three kinds and a fixed payload schema, which includes the marker `"authority": "observation-only"`. It cannot append approvals or denials, and the permission logic ignores these kinds. Permission requests and their withdrawals are unchanged.

## Payload

Each payload is at most 4 KiB of compact JSON, with exactly these fields:

| Field | Contents |
| --- | --- |
| `format`, `version` | `sandbox-provision-observation`, 1 |
| `event_id` | SHA-256 over a fixed format domain, the kind, the provision ID and the saved sequence number |
| `provision_id`, `request_operation_id` | The operation's identities |
| `context` | `project_id`, `session_id`, `environment_id` |
| `operation_seq` | The sequence number of the saved operation record |
| `state`, `stage`, `outcome`, `reason`, `error_kind` | As saved; the stage of an unfinished operation is derived from its state. Unknown values stay `null` |
| `permission_request` | The permission request's sequence number, or `null` |
| `revision_planned`, `revision_before`, `revision_after` | Environment revisions, or `null` |
| `execution_count`, `file_count`, `probe_count` | At most 128, 2,000 and 64 |
| `submitted_at`, `started_at`, `ended_at` | The source record's times |
| `trigger` | `transition`, or `startup_classification` (only for `provision.finished`) |
| `authority` | `observation-only` |

Purpose, package specs, notes, free error text, next steps, paths, URLs and diagnostic output are left out, and not hashed. Every field is checked for its exact type and bound before the payload is encoded, and the payload is copied. Nothing here says whether an unknown effect happened.

## When they are written

The `Provisioner` emits each event after the record it describes is saved:
- `provision.requested` in the operation's own run, before its turn;
- `provision.awaiting_permission` after the permission request and the state are saved;
- `provision.finished` after the terminal record is saved. When a worker starts and classifies an unfinished operation, the finished event says `startup_classification`; a missing request or history is not filled in.

Replaying the exact submission, or a second start after an operation finished, writes nothing again. A permission wait's deadline is fixed before the observation is written, so writing it does not extend the wait.

## Coverage

The worker's records and the event store are separate databases, so these events are a partial observation, not an atomic outbox or an exactly-once history. A crash, a refused projection, a full writer, an absent store setting or an unavailable store can leave no event. An append can commit before its acknowledgment is lost; its outcome then stays unknown, and it is never retried. A missing event never proves that an operation or permission request did not happen: the operation record is authoritative. Failures log fixed warnings without exception text and do not change an operation's outcome.

## Limits

- At most eight appends are in flight; beyond that an event is dropped as unavailable. Their threads stay owned through repeated cancellation and shutdown, before the shared `Permissions` writer may close; the observer never closes it.
- A terminal operation gives up its environment's queue turn before it waits for its final event. Its task stays registered for environment forget and shutdown until the append drains, and the retention timer starts afterwards. Final events of different operations can therefore arrive in another order than their transitions.
- The store's five-second lock timeout is not a deadline for fsync or the thread: waits and cleanup can take longer while admitted work drains. The per-record and in-flight bounds set no disk quota for the store.

Checked with real installs on OpenShell in [Lab35](../.agents/docs/agent-reports/babbage-openshell-lab-35-2026-10-13.md), where no operation waited for permission.
