# Job sandboxes

Status: Draft design (2026-10-22); not built. It designs D93, a confirmed owner
requirement: a big [background job](../BACKGROUND-JOBS.md) (D83) may run in a new
sandbox of its own, sized and placed by trusted profiles, inside the same cluster and
control environment. It extends [tools](tools.md#background-jobs) as the first "batch
experiment" mode ([architecture](../ARCHITECTURE.md#worker-lifetimes-and-api)).
Requirements are numbered JS-n; owner questions are at the end.

## Context

Today a job runs in the session's environment under the policy's job limits
([policy](../POLICY.md#background-job-resources)), at that environment's size, pinned at
creation by one Kubernetes profile per base image ([placement](../KUBERNETES.md#placement)).
A 256 GiB, three-day computation needs a session environment that size for its whole
life, or manual work over ssh, Slurm or Modal. The worker already has what a second
sandbox needs: records before create and IDs before Ready, identity-checked deletion,
stop and start ([environments](../WORKER.md#environments)); profile placement; the
isolation probe ([tiers](../WORKER.md#isolation-tiers)); `shared/` sync
([D79](../WORKER.md#shared-data-across-environments)); a lockfile per package revision;
the sandbox daemon; ranged reads ([capture](../WORKER.md#bounded-artifact-capture)).

## Non-goals

No Kubernetes Job objects or other controller beside OpenShell, which owns sandbox pods
([placement](../KUBERNETES.md#placement)). No remote backends (ssh, Slurm, Modal),
multi-node or MPI jobs, GPUs, EKS, Karpenter or spot automation: future work the
profiles should carry. No mounting of one sandbox's disk in another. No automatic replay.

## What a job sandbox is

- JS-1. **A new environment kind.** A logical environment of kind `job`, created by the
  worker through OpenShell for exactly one job execution, under the project and the
  session environment's stored policy revision. Its ID is `<project>-job-<12 hex>`; its
  sandbox name follows the usual `ss-` rule. It runs that job only: no kernels, calls,
  provisioning requests or other jobs, and no agent tool can name it (`not_found`). (new)
- JS-2. **Same identity as other jobs.** `Execution.context` keeps the session's
  environment ID, so harness, workspace and recovery ownership checks are unchanged.
  `JobDetails.sandbox` names the job environment, profile and phase; `Location` reports
  the job sandbox's ID, isolation tier and evidence, and environment revision.
- JS-3. **Image.** The job sandbox starts from the image the session environment
  recorded at its creation (D46), so job and session share a base. A job profile whose
  pinned architecture differs from the session environment's is refused (`unsupported`).
- JS-4. **Lifetime.** Phases: `creating` (pod being placed), `preparing` (packages,
  inputs), `running`, `collecting`, then `released`, or `retained` until a deadline and
  then released. The execution stays `queued` until `running` and ends only after
  collection (JS-15), so no completion notice precedes the files it names.

## Asking for one

- JS-5. **`start_job` gains `profile` and `inputs`** (and `resume_from`, JS-21).
  `profile` names a job profile allowed for this project; omitted, the job runs in the
  session environment as today. `inputs` lists up to 64 session-workspace files or
  directories to copy in. The harness builds the `profile` enum and descriptions ("16
  cores, 256 GiB, up to 7 days") from the worker's capabilities. Agents never supply
  CPU, memory, node selectors, runtime classes, volumes or pod JSON. (new)
- JS-6. **Trusted minimum tier.** The harness adds `min_isolation_tier` from its own
  `--min-isolation-tier`, never from tool arguments, so the worker can refuse a weaker
  job sandbox before the command runs.
- JS-7. **Idempotency.** A retry with the same operation ID returns the same execution,
  never a second sandbox: the job names its sandbox record, written before creation.

## Profiles, policy and placement

- JS-8. **Job profiles are worker configuration**, in
  `[openshell.environments.job_profiles.NAME]`: a `KubernetesProfile` (node selector
  pinning `kubernetes.io/arch`, runtime class, CPU, memory and ephemeral-storage requests
  and limits) plus `description`, `wall_time_max` (≤ 7 days), `schedule_timeout`
  (default 30 minutes, ≤ 24 hours), `max_concurrent`, `stage_max_bytes` and
  `collect_max_bytes`. A pool declaring tier 2 or 3 refuses a job profile without a
  runtime class; today all name `kata` and amd64
  ([isolation](../KUBERNETES.md#sandbox-isolation)).
- JS-9. **Policy grants them.** New policy resources: `job_profiles` (names a project may
  use) and `job_sandboxes` (concurrent job sandboxes per project, counting `creating` and
  `preparing`; default 0, so the feature is off until granted), clamped by the pool
  ceiling. The environment `cpu`/`memory` caps size session environments and do not
  narrow a job profile; the profile list is the grant. The time limit is the least of
  the request's, the policy's `job_wall_time_max` and the profile's.
- JS-10. **Refusal before anything is created**, with a reason: an unknown or ungranted
  profile (`policy_denied`, listing granted ones); a full project, profile or pool
  (`max_job_sandboxes`) count (`policy_denied`, listing active job sandboxes); the
  session's existing job limit; an architecture mismatch (JS-3); or a pool whose
  declared tier is below the request's minimum (`unavailable`).
- JS-11. **The sandbox itself is probed.** A tier in effect below the request's minimum
  or the session environment's ends the job `failed` (`isolation_insufficient`) and
  releases the sandbox before the command runs; runc on a Talos node is tier 1 (D43).

## Packages, inputs and policy

- JS-12. **Packages.** Increment 1 has the image's packages only (revision 0); the start
  result and manifest name the packages the session added and say they are absent.
  Increment 2 replays the session's current lockfile (Conda files by channel, name and
  SHA-256, then pip pins) through the package cache under the same policy; unless the
  job environment's lockfile then matches, the job ends `failed` (`environment_mismatch`).
  Copying a live prefix (offline checkpoints need a stopped sandbox) and mounting one
  ([a shared mutable prefix](../PROJECTS-AND-SESSIONS.md#provisioning-and-persistence))
  are rejected. Files outside the prefix travel only as inputs. (new)
- JS-13. **Inputs.** The worker copies each input from the session environment to the
  same relative path in the job sandbox's session workspace, by ranged reads and file
  writes, following no symlink, within `stage_max_bytes` (default 2 GiB) and the existing
  100 MiB per write. `shared/` arrives by the ordinary sync before the job starts. Skill
  snapshots named by the job's `bindings` are staged again. The manifest records each
  input's path, size and SHA-256.
- JS-14. **Same policy and middleware.** The job environment is compiled from the session
  environment's policy revision with the same providers, bound in the middleware to its
  own sandbox ID, seeded with the session environment's admitted hosts, admits others on
  demand (D69) and is revised with the project. Installs inside the job go through the
  wrappers and policy and become revisions of the job environment only; the manifest
  records its start and end lockfile digests. Nothing returns to the session environment.

## Results

- JS-15. **Collection.** When the command ends, the worker copies into the session
  workspace the whole `.jobs/<ID>/stdout.log` and `stderr.log` (the existing paths), and
  each file in the job attempt's `written_files` under its session workspace to
  `.jobs/<ID>/files/<relative path>`, within `collect_max_bytes` (default 4 GiB).
  `$JOB_SCRATCH_DIR` (`.jobs/<ID>/scratch/`) is never collected; `shared/` returns by
  the ordinary sync. Collection has its own 30-minute deadline, outside the time limit.
  The harness's recorder records collected files as artifact revisions at the completion
  notice, so S3/Garage retention applies.
- JS-16. **Immutable result manifest.** With the terminal state, in one transaction, the
  worker stores a `JobResultManifest`: job, attempt, profile and driver-config digest,
  image, sandbox ID, tier and evidence, timings, outcome, exit code or signal, peak
  memory if measured, lockfile digests, inputs, collected files (path, size, SHA-256),
  and files not collected with the reason. It is never rewritten; `job_status` returns
  it with its digest, and `.jobs/<ID>/manifest.json` is an untrusted copy.
- JS-17. **Partial output.** Failure, timeout, cancellation and loss collect what can be
  reached. A cut-short collection (over 1,000 files, the byte cap or the per-write
  limit, or unreadable files) marks the manifest `collection_incomplete` and retains the
  sandbox.
- JS-18. **Reading results:** `job_status` (phase, measurements, live head and tail),
  then `read_file` and `list_files` on `.jobs/<ID>/`. No new tool.

## Long runs

- JS-19. **What survives.** The job sandbox's daemon keeps the job across turns, run
  ends, orchestrator and control restarts, GUI closing, gateway restarts and worker
  restarts ([daemon](../WORKER.md#sandbox-daemon-and-recovery)). A worker restart before
  the command was dispatched ends the job `failed` (`worker_restarted_before_start`) and
  releases its sandbox; nothing ran.
- JS-20. **What does not.** Pod deletion or eviction (node drain, Talos upgrade, node
  failure), Kata VM failure, sandbox deletion and, later, spot interruption end the
  daemon. The job is `lost` (`sandbox_lost`), and its command is never resubmitted by
  the worker, the harness or Resume ([continuity](research-continuity.md#recovery),
  RC-9). An out-of-memory kill inside the VM is an ordinary failure with its signal.
  Pod replacement keeps the workspace claim's data, not process memory.
- JS-21. **Checkpoint convention** (increment 3). A job may write checkpoints under
  `$JOB_CHECKPOINT_DIR` (`.jobs/<ID>/checkpoints/`), each a directory `ckpt-<n>` with a
  `COMPLETE` file written last. Every 30 minutes and at collection, the worker copies the
  newest complete one into the session workspace's `.jobs/<ID>/checkpoints/`, keeping
  two within `collect_max_bytes`; one only on the pod's disk is lost with it. A new job
  started by the agent or user with `resume_from: <job ID>` gets that job's latest copy
  at `$JOB_RESUME_DIR`. Jobs longer than seven days need this.
- JS-22. **Disk.** The workspace claim's size is a gateway default, not in the driver
  configuration, so profiles size ephemeral storage only; per-profile claims need
  OpenShell support (qualification item).

## Capacity and visibility

- JS-23. **Waiting for room.** Full worker counts are refused at once (JS-10). When no
  node has room, the pod stays pending and the job `queued` in `creating` up to the
  profile's `schedule_timeout`; then it ends `failed` (`capacity_unavailable`), the
  sandbox is deleted by identity, and nothing retries. Whether OpenShell reports the
  scheduler's reason is a qualification item; until then the GUI says "not placed yet".
- JS-24. **The GUI** session page shows each job's profile, phase, reserved CPU and
  memory, tier and retention deadline, with "release now" beside "cancel". A worker CLI
  command lists job environment records and any `ss-` sandbox no record names.

## Cleanup and cost

- JS-25. **Release.** After a successful, complete collection the worker deletes the
  sandbox by identity at once. After failure, timeout, cancellation, loss or incomplete
  collection it stops the sandbox (disk kept, CPU and memory freed) and retains it for
  `job_retention` (pool setting, default 3 days), at most 2 per project (a third releases
  the oldest early). Closing a session or deleting its environment cancels, collects and
  releases without retention. Release deletes all that was not collected, as the tool
  description says.
- JS-26. **Ownership and orphans** reuse the environment rules: record before create, ID
  before Ready, delete only the recorded ID with confirmed absence, keep the record when
  creation or deletion is uncertain. At startup and hourly, a live job reattaches, an
  unfinished collection restarts, an overdue retention releases, and a record whose
  execution is gone is reported and kept. A sandbox without a record is never deleted.

## Contracts, configuration and code

- JS-27. **Contracts.** `JobRequest`: `profile`, `inputs`, `resume_from`,
  `min_isolation_tier`. `JobDetails`: `profile`, `sandbox` (environment ID, phase,
  reserved resources, retained until, collection status, manifest digest). New
  `JobResultManifest`. `EnvironmentInfo`: `kind`, `job_id`. Capabilities: a
  `job_sandboxes` feature listing profiles allowed to the client's projects. A worker
  route releases a retained sandbox for the workspace server's logged-in user.
  `Resources`: `job_profiles`, `job_sandboxes`. No executor protocol change until step 4.
- JS-28. **Reuse** in `worker/`: `environments.py` (create, records, `_wait_ready`,
  `_cleanup_creation`, `_delete`, stop), `kubernetes.py`, `dispatcher.py`, `daemon.py`,
  `journal.py`, `isolation.py`, `shared_sync.py`, `admission.py`, `files.py`, and
  `provisioning.py`'s `lockfile` with `provision_stages.py`; `orchestrator/jobs.py`, the
  artifact recorder, `workspace/jobs.py`.
- JS-29. **New code:** `worker/job_sandboxes.py` (admission, phases, reconciliation,
  retention), `worker/job_transfer.py` (staging, collection, checkpoints),
  `worker/job_manifest.py`, settings, the CLI command, schema changes and GUI columns.
  The renderer (`launcher/kubernetes_config.py`, `kubernetes_bundle.py`) passes job
  profiles through the worker TOML and checks `kata` and amd64; no new network flow.
- JS-30. **Tests.** Unit, with the worker's fake OpenShell (`Fake` in
  `test_environments.py`): each refusal, record-before-create, ID-before-Ready, schedule
  timeout, uncertain create or delete, tier refusal, transfer bounds, manifest written
  once, retry idempotency, worker restart in every phase, loss without resubmission,
  retention. Process: real worker, harness and workspace server over the fake CLI with
  local daemons, through `start_job`, `wait_jobs`, a worker restart and `read_file`.
  Live, disposable, on Kata (amd64): admission with the profile's resources, tier 3, a
  job above the session's memory, worker restart mid-job, pod deletion giving `lost` and
  no rerun, an unplaceable profile giving `capacity_unavailable`, denied egress still
  denied, nothing left after release. Fixtures are not live evidence.

## Delivery

1. **Smallest useful increment:** profiles and policy grant; `start_job` with `profile`
   and `inputs`; image packages only (JS-12); collection, manifest, retention and
   release; tier checks; schedule timeout; GUI phase; Kata acceptance.
2. Lockfile replay of the session's packages.
3. Checkpoints, a periodic log mirror, `resume_from`.
4. Files over 100 MiB both ways; the agent inspecting retained sandboxes. Later: EKS
   node pools as profiles, spot interruption, remote backends.

## Open questions for the owner

1. **Which sizes should we offer first?** Each size is a named choice the agent picks,
   such as "large: 16 cores, 256 GiB memory, up to 7 days", and must fit one real node
   with room to spare. Recommendation: two sizes, medium and large, set from your
   biggest amd64 node; tell us its cores and memory.
2. **How many big jobs may run at once?** If two agents each start a 256 GiB job and
   only one node can hold one, the second must wait or fail. Recommendation: one per
   project and two in the whole cluster at first.
3. **When the cluster has no room, how long should a job wait?** Example: it shows
   "waiting for room" for 30 minutes, then fails with a clear message, and the agent can
   try later or pick a smaller size. Recommendation: 30 minutes; a size may allow 24 hours.
4. **Should node upgrades wait for running big jobs?** Example: a three-day sieve runs
   on node A; a Talos upgrade restarts node A at hour 50; the job is marked lost and only
   its saved checkpoints remain. Recommendation: no automatic blocking for now; check
   the job list before upgrading, and upgrade nodes without big jobs first.
5. **How long should a failed job's sandbox be kept?** Example: a job crashes at hour
   20; its disk is kept, with no CPU or memory reserved, so you (later also the agent)
   can get files that were not copied back. Recommendation: 3 days, 2 per project.
6. **Is 4 GiB the right copy-back limit?** A finished job keeps its logs, the files it
   wrote (up to the limit), shared files and checkpoints; the rest is deleted with its
   sandbox. Example: a 50 GiB temporary database goes in the scratch folder and is not
   kept. Recommendation: 4 GiB per job.
7. **May the first version start jobs with the base image's packages only?** Example:
   if the session installed `g6k` after it started, the big job lacks it unless its
   script installs it (still under your policy), and the result says so.
   Recommendation: yes; copying the session's packages comes next.
