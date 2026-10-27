# Backup and restore

What to keep, how to snapshot it, and how to put it back. It is written for the owner's personal instance on babbage ([personal instance](PERSONAL-BABBAGE.md)) and any `sandbox-science up` instance with the same layout; the [Kubernetes deployment](#on-kubernetes) will do the same job with volume snapshots.

The stores are SQLite files in write-ahead-log mode and directories of content-addressed files. Copying the state directory with `cp` or `rsync` while anything runs can tear a database (a `-wal` file holds changes the main file does not), and a copy taken a minute apart from its neighbors can disagree with them. So take a backup with [`scripts/personal_backup.sh`](../scripts/personal_backup.sh) (the work is [`scripts/backup_stores.py`](../scripts/backup_stores.py)): it copies each database through SQLite's online backup, checks it, and writes one dated directory with a manifest. A volume that holds the only copy is not a backup either ([a PVC is not a backup](KUBERNETES-BUNDLE.md#prepare-private-configuration)); keep a copy on another machine ([off the host](#off-the-host)).

## What state exists

Paths are under `~/.local/state/sandbox-science-personal/` (`$ROOT`) on the personal instance.

| State | Where | Back it up? |
| --- | --- | --- |
| **Control store**: projects, sessions, runs, documents and their history, comments, reviews, plans, artifact records, jobs, math checks. The research record; it exists nowhere else | `launcher/control/control.sqlite3` (WAL) | Yes. [Living documents](LIVING-DOCUMENTS.md) |
| **Artifact bytes** the control store points to (hash-named, written once); the optional S3 settings | `launcher/control/control.sqlite3.artifacts/blobs/ab/<sha256>`, `storage.toml` | Yes. A blob evicted to S3 is in the bucket, which has its own durability ([S3 backing](ARTIFACT-S3.md)) |
| **Worker stores**: environments, revisions, package gates, recovery marks, leases, shared-file records, claims | `launcher/worker/state/worker.sqlite3`, `environments.sqlite3`, `environment-claims.sqlite3`, `shared.sqlite3`, `documents-catalog.sqlite3` | Yes. [Worker](WORKER.md) |
| **Worker blobs**: capture bytes, lockfiles | `launcher/worker/state/artifacts/`, `lockfiles/` | Yes |
| **Event store** (hash-chained: permission decisions, alerts, provisioning records) | `openshell/middleware/state/events.db` | Yes. [Event store](EVENT-STORE.md). Checkpoints in the journal are kept by the journal |
| **Policy store**: project policies by hash, sandbox bindings | `openshell/middleware/state/policies/`, `sandboxes/`, `projects/` | Yes (small) |
| **Config**: the launcher's config and the project policy (no secrets) | `~/.config/sandbox-science/personal.toml`, `personal-policy.yaml` | Yes |
| **Package cache** | `openshell/cache/` | No: refetched on demand |
| **Worker `environments/` and `shared/`**, the launcher's `sandboxes.json`, logs | `launcher/worker/state/environments/`, `shared/` | No: rebuilt from the sandboxes, or only diagnostics |
| **Sandboxes' disks** (research files, installed packages, kernel state) | `vm/`, owned by the OpenShell VM driver | No: large, and the driver's. What the agent publishes as artifacts is in the control store |
| **OpenShell gateway state**: its database (registered providers, with credentials, and the key that encrypts them) and certificates | `openshell/state/openshell/` | **Never.** [Never copy, archive or query it](PERSONAL-BABBAGE.md#what-runs-where). The script refuses any path inside it |
| **Secrets and keys**: tokens, TLS and CA keys, model key, Cloudflare Access file | `launcher/tokens/`, `openshell/middleware/tls/`, `~/.config/sandbox-science/secrets/` | No: see [Secrets](#secrets) |

## What to back up together

One backup is a set: control store and blobs, worker stores and blobs, event store, policy store. They refer to each other (a run in the control store names environments in the worker's; the worker's permission requests are in the event store; a sandbox binding names a policy), and no transaction spans them. The set is consistent only if nothing writes while it is taken, so:

- **Stopped is the way** (`scripts/personal_down.sh`; the launcher's and worker's lock files are then free). The script checks those locks and refuses while an instance runs.
- **`--live`** takes each database through SQLite's online backup, which is a consistent snapshot of that database whatever else writes (a reader does not block writers in WAL mode). The databases are then at slightly different moments, and the manifest records `"live": true` when an instance held its locks. It is for an unattended safety copy, not for a snapshot before a migration. The middleware's unit may still run after the launcher stops (`personal_down.sh --keep-sandboxes`); the script notes it, and copies `events.db` as it is.
- **Order inside a backup**: databases first, then blobs. A blob is on disk before the row that names it, so every blob a copied row names is in the copy. (Extra blobs are harmless.) Blobs are checked against their names as they are copied, and a blob that is not its name stops the backup.

## Taking a backup

```sh
scripts/personal_down.sh                          # stop it; see "Before an update" for why
scripts/personal_backup.sh --label before-update  # prints the new directory
scripts/personal_backup.sh --verify DIR           # check one again, later or elsewhere
```

It writes `$SS_PERSONAL_BACKUPS/<UTC time>[-label]/` (default `~/.local/share/sandbox-science-personal-backups/`; directories mode 700, files mode 600), under a `.partial` name until everything is in it and `MANIFEST.json` is written, so an interrupted run never looks complete. The manifest holds each file's SHA-256 and size, each database's `user_version` and `application_id`, whether it was live, and the repository's revision. Each database copy passes `PRAGMA integrity_check` before it is accepted, and the script checks the free space first. A blob already in the previous backup is hard-linked, not copied again, so a daily backup of a store of artifacts costs the new blobs only.

The script deletes nothing: old backups pile up until you remove them. A sensible rule is to keep the last few dailies and every `before-*` backup until the update has proved itself. Hard links share a blob among backups, so removing an old backup directory is safe.

## Before an update

The control store's schema moves forward only: `MIGRATIONS` in [`control/migrations.py`](../packages/orchestrator/src/sandbox_science/orchestrator/control/migrations.py) is an ordered list, a writer migrates the store when it opens it, and a store in a newer version than the code is refused ([schema versions](LIVING-DOCUMENTS.md)). The worker's store does the same, with its own list. There is no downgrade. So the only way back from a migration that goes wrong, or from a release that wrote bad data, is the snapshot taken before it, with the revision that made it.

In [Updating to a new main](PERSONAL-BABBAGE.md#updating-to-a-new-main), between step 1 (stop it) and step 2 (`git pull`, `uv sync`):

1. `scripts/personal_backup.sh --label before-<new revision>` (the stopped instance is the consistent one).
2. Pull and `uv sync --locked`. Then `scripts/personal_backup.sh --versions` says, for each store, whether the new checkout would migrate it: exit status 0 means no, 3 means yes (the snapshot of step 1 is then the only way back), 1 means a store is newer than the checkout (a start would refuse; you pulled an older revision). The snapshot is taken first either way, because the check needs the new code and the new code must not touch the stores before the snapshot exists.
3. Start it (`scripts/personal_up.sh`). If a migration refuses a row, the store stays at its old version and the writer names the table and the check ([a store a migration refuses](LIVING-DOCUMENTS.md)); nothing is lost, and the snapshot is a second line.

`scripts/personal_up.sh` also makes this check for you. Before it starts anything (after its own config checks, before the unit), it runs `personal_backup.sh --versions`. Exit status 0 means nothing would migrate, and it goes on. Status 3 means a store would migrate: it runs `personal_backup.sh --label before-migration` (not `--live`) and starts nothing if the snapshot fails. Any other status (a store newer than the checkout, or the check could not tell) also starts nothing, since the start would refuse. Doing step 1 by hand with a better label is still worth it, since this one is named for no revision. The snapshot needs the launcher and worker stopped, so a kept unit that still runs (`personal_down.sh --keep-sandboxes`) makes a needed snapshot fail: stop everything first. To start without the check and the snapshot, pass `personal_up.sh --skip-snapshot` or set `SS_PERSONAL_SKIP_SNAPSHOT=1`; then step 1 is yours.

## Restoring

Restore when a migration or a release damaged the stores, when a disk is lost, or to move the instance to another machine.

```sh
scripts/personal_down.sh                                   # all four units must be stopped
scripts/personal_backup.sh --verify DIR                    # the backup is whole
scripts/personal_backup.sh --restore DIR                   # prints what it would replace
scripts/personal_backup.sh --restore DIR --apply           # does it
scripts/personal_backup.sh --restore DIR --with-config --apply   # and the two config files
```

1. **Check out the revision in the manifest** (`git checkout <revision>`, `uv sync --locked`, rebuild the web app if it differs): the stores in a backup are at that revision's schema versions, and an older checkout refuses a newer store. Restore prints a warning when a store is newer than the checkout.
2. **Restore refuses** while a unit runs or a lock is held, and when the backup fails its own check. With `--apply`, each file is copied beside its target, then renamed into place (mode 600); the file it replaces, and its `-wal` and `-shm`, are renamed `<name>.pre-restore-<time>`. Nothing is deleted. Content-addressed files already in place are left; a damaged one is set aside. Blobs the backup lacks (made after it) stay where they are.
3. **Start it** (`scripts/personal_up.sh`) and look: `sandbox-science status --config ~/.config/sandbox-science/personal.toml`; the projects and documents are there (`.venv/bin/sandbox-science-harness document list --state-dir $ROOT/launcher/control --project crypto`); the event store verifies (`.venv/bin/sandbox-science-events verify $ROOT/openshell/middleware/state/events.db`).
4. **Expect the gap.** Everything after the backup is gone from the restored stores: runs, documents and decisions made since, and records in the event store (a checkpoint taken later in the journal then fails `verify --checkpoint` because the store is shorter; that is the evidence of the gap, not damage). The `.pre-restore-*` files hold what was replaced, so the gap can be inspected or mined, for example by exporting documents from them (`document export --state-dir` on a copy).
5. **Sandboxes can disagree with the restored worker records.** The sandboxes live in OpenShell's state and `vm/`, which a restore does not touch. One created after the backup is unknown to the restored worker; one deleted since is still recorded. `openshell sandbox list` (after `source $ROOT/openshell/env.sh`) shows what exists; the worker's recovery marks and [recovery fence](RECOVERY-FENCE.md) keep an environment with uncertain package state out of service until an operator clears it. This path, restoring over live sandboxes, has not been exercised on babbage.
6. **A lost gateway** (its state directory gone) is not restored from anything here: `personal_up.sh` makes a new one with new certificates and registers `github-readonly` again with a placeholder; the launcher registers `crypto-research-mcp` from the Cloudflare Access file in the config. Sandboxes whose gateway is gone are gone, and the worker's records of them are for the operator to reconcile ([worker](WORKER.md)).

The helper's own checks (an integrity check of every database, hashes of every file, refusal of the gateway's directory, private modes, a real control store opened after a restore) are tested in `packages/testing/tests/test_backup_stores.py`. A restore on babbage itself is part of the first real use; it has not been run against the personal instance.

## Secrets

The script copies none. Each has a source of truth outside the state directory, or is made again:

- **The model key** (`~/.config/sandbox-science/secrets/litellm-harness.key`) and **the Cloudflare Access file**: yours, kept in your password manager, not here.
- **`launcher/tokens/`** (the worker's, run service's and workspace's tokens): made again when the directory is gone ([state directory](QUICKSTART.md#the-state-directory)). 
- **The middleware's CA and certificate** (`openshell/middleware/tls/`, ten years) and **the gateway's certificates and keys**: made once by `personal_up.sh` and trusted by the gateway and its sandboxes. Losing the middleware's means a new CA and the sandboxes trusting it again; do not archive private keys beside the data.
- **Provider credentials** in the gateway database: re-registered from their source files, as above. This is why the database is never copied.

Backups are mode 600 in a mode 700 directory and hold no key, but they hold research content: keep them where the instance's data is allowed to be.

## Off the host

A backup beside the data it protects survives a bad migration but not a lost disk. After each backup, copy the dated directory to another machine, preserving hard links and modes: `rsync -aH --chmod=D700,F600 $SS_PERSONAL_BACKUPS/ other-host:backups/sandbox-science-personal/`. Encrypt it at rest where that machine is not as private as this one. Run `--verify` there; the manifest is the check.

## On Kubernetes

The first deployment ([D92](DECISIONS.md), [first deployment](KUBERNETES.md#first-deployment-scope-and-proofs)) does not use this script. It runs Longhorn recurring snapshots with backup to Garage from the start (task B1 in the [backlog](../.agents/BACKLOG.md)); Garage also holds the artifact bytes ([S3 backing](ARTIFACT-S3.md)). The differences that matter:

- A Longhorn snapshot is crash-consistent for one volume. SQLite with `synchronous=FULL` recovers from that, but the control, worker and event stores are on different volumes and are snapshotted at different instants, so the set can disagree. Quiesce the application first for any snapshot you will restore a set from, and above all before an upgrade (scale the app and pool to zero, snapshot every volume, then roll out). Recurring snapshots are the safety net between upgrades, not a consistent set.
- The control volume stays small ([bundle storage](KUBERNETES-BUNDLE.md#prepare-private-configuration)); artifact bytes go to Garage with verified retention before local eviction, so a restored control volume finds its bytes there. Garage's own durability (versioning, lifecycle rules that must not expire referenced objects) is an operator setting.
- Workspace checkpoints (a verified archive of a research volume, to Garage) cover the research volumes. They do not restore the control, worker, policy and event stores, bindings or ownership, so a complete recovery set still needs those backed up together ([checkpoints](WORKER.md#offline-complete-workspace-checkpoints)). Automatic PVC release waits for exactly that ([Kubernetes work](KUBERNETES.md)).
- The same two rules hold: a snapshot before every upgrade that changes a schema version, and a restore at the revision that made it, since migrations only go forward.
- Two maintenance tasks change what a restore finds. Scheduled cache trimming ([`[maintenance]`](KUBERNETES-BUNDLE.md#cache-trimming-opt-in)) unlinks local copies of artifact bytes only after verifying them in Garage, so a control volume restored from before or after a pass finds the same content, locally or remotely. The offline worker replica release ([runbook](KUBERNETES-BUNDLE.md#offline-worker-replica-release)) unlinks the worker's CAS replicas of content control has retained and acknowledged: scale the pool to zero, `kubectl create -f` the rendered Job, read its report, scale back. A pool volume restored from before a release still has those replicas; one restored from after it relies on control's copies (and Garage), so restore the control volume and Garage's objects with it, never a pool volume newer than the control volume. Snapshot the pool volume while the pool is scaled to zero for the release if you want a set to return to.

None of this is built or proven on the cluster yet; this page will say what was tested when the first deployment runs a restore.
