# Child environments

Status: Decided by the owner (2026-10-26): all six questions as recommended. First
increment built 2026-10-26, fake CLI only; not qualified on a cluster: the worker's kind,
admission, preparation, hand-back, retention, release and restart rules
([worker](../WORKER.md#child-environments)), the delegation's setting, placement, records
and outcome, the run in its own environment, and a Resume's choice of environment. Open:
the Compute tab's children's kernels (CE-8), the workspace's environment views (CE-11's
GUI part), the prompts' guidance (CE-13), inputs from control's retained content
(CE-7), files over 100 MiB in a hand-back (as JS's delivery step 4: CE-F1's 1.5 GiB
file is left out and the environment retained), and read-only `inputs/` and
`work/<id>/`: the worker's file calls set no file mode, and a sandbox's agent code runs
as the user who owns its files and could change one back, so it needs per-run mounts
(DA-20's open question 1). Reviewed 2026-10-26; the review's fixes are built. It designs
backlog item B7, raised by the owner on 2026-10-24: for some projects, each subagent's
research environment runs in a sandbox of its own, possibly on another node, for compute
and memory; the parent, above all the main agent, owns the durable records, and a
child's disk is working memory while the child runs or waits for more work, released
when the parent closes the child. Requirements are numbered CE-n; the owner's questions
are at the end, each with a recommendation. It extends [plans and
delegation](plans-and-delegation.md) (PD-14, "a delegation creates no container"),
[delegated agents](delegated-agents.md) (DA-19, DA-20) and [job
sandboxes](job-sandboxes.md) (D93), whose mechanisms it reuses.

## Context

A session has one environment: the project's default, or a dedicated one made with
`--dedicated` ([projects and sessions](../PROJECTS-AND-SESSIONS.md)). Every run of the
session, the main agent's and each delegated child's, executes in that one sandbox:
`run_child` copies the parent's context, environment included (`harness.py`), and each
child gets kernels of its own inside it, keyed by session and run (PD-37, `KernelInfo.run`).
So a session's children share one node's CPU, memory and disk, and a child's files are
the parent's by path: `read_file` takes no run, and DA-19's `work/<delegation_id>/`
write area is not built.

The worker already has what a second sandbox needs, and the job sandbox uses it: an
environment of another kind created from the session environment's image, policy
revision, providers and admitted hosts, placed by a trusted profile, recorded before
creation, released or retained at the end, with outputs copied back through the worker
under bounds (JS-1 to JS-26). A job sandbox carries one job and runs no agent, no kernels
and no tool calls. A child environment is the same shape carrying one delegated run.

Two costs frame the design. Every new sandbox seeds its workspace from the image: with
the slim research images the copy took 69 s on the cluster's single-replica class, 111 s
before, and the gateway's fixed 5-minute grace ends a slower one ([images](../IMAGES.md),
[Kubernetes](../KUBERNETES.md#placement)). Since B11 ("option C", built 2026-10-26) the
research images keep their Python environment in the image and seed only a small layer,
about 1,700 entries in crypto-base, which a local copy writes in seconds
([the environment outside the workspace](../IMAGES.md#the-environment-outside-the-workspace-b11)).
And artifact
content leaves a sandbox only through the recorder's capture, bounded per file and per
call ([artifacts](../ARTIFACTS.md)); larger files keep a hash and a size, and their bytes
stay where the file is.

## Non-goals

No shared disk between sandboxes (as JS-non-goals). No change to what a delegated run may
do: its tools, fence, connectors and authority are DA's and PD's. No multi-node child, no
GPU placement, no remote backend. No automatic choice of a profile by the model: the
platform places a child by the project's settings. No change to checks' evidence rules.

## What a child environment is

- CE-1. **A new environment kind.** A logical environment of kind `child`, created by the
  worker through OpenShell for exactly one delegated run, under the project and the
  session environment's stored policy revision. Its ID is `<project>-child-<12 hex>`;
  `Environments.create` refuses that shape as it refuses job IDs. It carries the
  delegation's ID and the session's. It is made from the session environment's image,
  policy revision, providers and admitted hosts, as a job sandbox is (JS-1, JS-9), with
  the lockfile of the session environment's current package revision replayed into it
  before the run starts, so the child computes with the same packages the parent does.
  A child environment runs the agent's kernels and tool calls, which a job sandbox does
  not: the worker's executor, kernels, file tools and recorder work in it exactly as in a
  session environment, scoped to the one run.
- CE-2. **Who gets one.** A project setting, `child_environments`, is `off` (today's
  behaviour: every child shares the session's environment), `tasks` (every delegated
  task gets a child environment; checks stay in the session's) or `all`. A task brief
  may ask for `environment: shared` under `tasks` or `all`, for a small task not worth a
  sandbox, and the platform honours it; a brief cannot ask for `own` under `off`, and
  `delegate` says so in a notice rather than refusing (the task runs shared). A check is
  never placed in its own environment unless the setting is `all`: its reading of the
  entry's evidence is the same wherever it runs, and it rarely computes.
- CE-3. **Placement.** A child environment is placed by a trusted child profile, one of
  the project's `child_profiles`, each a `KubernetesProfile` as session and job profiles
  are: image, node selector pinning the architecture, runtime class, CPU, memory and
  ephemeral-storage requests and limits, and its isolation tier (per-profile tiers,
  [D43](../DECISIONS.md)). The project names a default child profile; a brief may name
  another of the project's by name (`profile`), never a size or a node. A profile whose
  tier is below the project's `min_isolation_tier` cannot be a child profile. The
  policy's CPU and memory caps narrow the profile as they narrow a session's.
- CE-4. **Lifetime.** Creating, ready, running or waiting, collecting, then released or
  retained. The environment is created when the delegation is admitted and the run
  starts once it is ready; `delegate` returns the delegation ID at once, as today, and
  the parent's notice says the child is waiting for its environment. A child whose
  environment is not ready within the profile's `schedule_timeout` (JS-14's bound, 1800 s
  unless set, at most 24 hours) ends `failed` with the reason `environment_unavailable`,
  the environment is released, and the delegation's outcome says so; the parent may
  delegate again. The environment stays while the run works and while it waits for
  more work from its parent (`message_child`, PD-44), since the child's working state is
  on its disk. It is released when the parent closes the child: the delegation ends
  (returned, cancelled, a limit, a failure) and its hand-back (CE-6) is complete; or the
  session closes (JS-25's rule). An incomplete hand-back retains the stopped environment
  for the pool's `child_retention` (3 days unless set, at most 2 per project), as a job
  sandbox is retained, so an operator can recover what it holds.

  *Amended 2026-10-26 (as built):* "waits for more work from its parent" means a child
  whose run is still running and waits in `wait_agent_message` for a `message_child`.
  A delegation that returned (`partial` included) has ended, and a message to an ended
  delegation goes back to the root as a delivery failure (PD-44); so an ended child's
  environment is handed back and released (or retained) at once, and more work for it is
  a new delegation. An interrupted run that is never resumed keeps its environment until
  the session closes, or until it has been idle past `child_idle_max` (the project's
  setting, off by default) or else `child_orphan_idle` (24 hours) from its end, when the
  session's next main run releases it; one admitted for a delegation that was never
  written (the orchestrator stopped in between) is released past `child_orphan_grace`
  (15 minutes). A pool without child profiles, or a policy whose `child_environments_max`
  is 0, gives no environments: tasks run in the session's, with a notice.
- CE-5. **Working memory, durable records.** Nothing on a child environment's disk
  survives its release. The durable records are the ones the platform already keeps,
  owned by control and visible to the parent: the child's archive (its messages, tool
  calls and results), the files its calls recorded (the recorder's inventory, with their
  content when retained), the artifacts it published, its ledger entries and work
  records, and its result (`sandbox-task-result-3`) with its deliverables accounting
  (PD-45). The child's prompt says so (DA-15's family): what it wants to keep, it records
  or publishes; a result file or a deliverable it names must be a recorded file.
- CE-6. **The hand-back.** At the delegation's end, before release, the worker collects
  into the parent's environment, at `work/<delegation_id>/` (DA-19's path, made real
  here as the hand-back target, read-only to the parent's agent as any recorded input is
  writable only by its author), each of: the child's `result_files` (RA-38), every file
  its deliverables of kind `artifact` or `file` name (PD-45), and every recorded file
  whose content the recorder could not retain, under the job collection's bounds (JS-16:
  1,000 files, 100 MiB per write, a 30-minute deadline), with a manifest written once.
  Files already retained in control are not copied twice: the manifest names their
  artifact revisions. A collection past its bounds is incomplete: the delegation's
  outcome lists what was left behind, the environment is retained (CE-4), and the
  parent's outcome notice says so. The parent reads handed-back files with its ordinary
  file tools, at that path; the recorder records them as the parent's files, so they
  are artifacts of the session like any other.
- CE-7. **Inputs.** The brief's `inputs` (file grants, DA-17) are copied into the child
  environment at `inputs/` before the run starts, read-only, under the job sandbox's
  staging bound (JS-10's `stage_max_bytes`, 2 GiB), from the parent's environment or
  from control's retained content; an input past the bound refuses the delegation with
  `inputs_too_large` naming the file. `references` (PD-11, B8) are archive records and
  reach the child as today, materialized into its state block. `shared/` (D79) is
  synchronized into a child environment as into any environment.
- CE-8. **Kernels and compute.** A child's kernels live in its environment. The worker's
  kernel listing and observation are per environment, so the Compute tab asks for the
  session's and for each live child environment's, and shows each kernel under its
  agent with its current cell (B10, item 5). A child environment that is being created
  shows as such.
- CE-9. **Limits.** Per project, in the policy: `child_environments_max`, the child
  environments alive at once (default 2; 0 is `off` whatever `child_environments`
  says). Per pool, in the worker's settings: `max_child_environments` (default 2) and
  `child_retention`. Per profile: `max_concurrent`, `wall_time_max` (at most 7 days; a
  child past it ends `limit_reached`, its environment collected and released). The
  delegation `cap` (PD-38) is unchanged and counts shared and own-environment children
  alike; a `delegate` beyond `child_environments_max` under `tasks` or `all` is refused
  with `limit_reached` naming the limit, not queued and not silently run shared, since
  the parent chose a placement. Child environments do not count against
  `max_environments` (as job sandboxes do not).
- CE-10. **Policy and tiers.** A child environment runs under the session environment's
  policy revision; a later policy change reaches it as it reaches the session's
  (re-pinned at the next run, not mid-run). Its isolation tier is its profile's, lowered
  only by the probe (D43); the capability record of the child's run names its
  environment and tier, and the ledger's evidence rules read the tier from the run's
  record as they do today, so a `fail`-tier child's computation is treated as its tier
  says.
- CE-11. **Records.** The worker's environments table gains the kind `child` with the
  delegation and session IDs (control store and worker store steps). The delegation
  record names its `environment_id`; the outcome names it with the environment's end
  (released, retained with the reason, failed). The run tree and the Agents panel show
  each child's environment and state; the workspace's environment views list child
  environments under their session with their profile, node and age. The events of the
  environment's life (created, ready, collecting, released, retained) are the worker's
  events, as job sandboxes' are.
- CE-12. **Recovery.** After a worker restart, a child environment is reattached by its
  record as session environments are (JS-19, JS-20). On a tree Resume (D100), a child
  whose environment still exists continues in it; one whose environment is gone
  restarts in a new one with its inputs copied again, its archive and recorded artifacts
  intact, and its outcome says it restarted. A child whose run ended while its
  hand-back was interrupted resumes the collection without duplicates (JS-26).
- CE-13. **Cost and the model.** The `delegate` tool's description says that a task in
  its own environment starts after its environment is ready (about one to five minutes
  today) and that a small task should ask for `environment: shared`. The MAIN prompt
  says: delegate into an own environment for work that is heavy in compute, memory or
  disk, or that must not disturb the session's environment; keep short tasks shared.
  The project's cost view counts child environments' seeding as the worker reports it.
  Many short children in own environments are a cost the owner has named; B11 (option C,
  the Python environment out of the workspace) cuts each seed to seconds, this design's
  natural prerequisite for `all`.

  *Amended 2026-10-26 by the owner, replacing the as-built reading above (PD-46):* a
  child's environment stays while the child runs, waits, or has returned and awaits its
  parent's review. It is handed back and released when the parent accepts the
  delegation or cancels it, when the parent's run ends without accepting it, at the
  session's close, or at `review_max`. A message to a returned delegation reopens it in
  the same environment.

## Fixtures

**CE-F1 A task in its own environment** (CE-1 to CE-6, `scripted`):
- *Given* a project with `child_environments: tasks`, one child profile and a session
  whose environment is READY.
- *When* the main agent delegates a task with two deliverables of kind `file`, the child
  writes both, publishes one, records a 1.5 GiB file the recorder cannot retain, and
  returns `complete`.
- *Then* the delegation's record names a `<project>-child-…` environment created from
  the session environment's image and policy revision; the child's calls ran there; at
  the end `work/<delegation_id>/` in the session's environment holds the unpublished
  deliverable and the 1.5 GiB file, the manifest names the published one's artifact
  revision, the environment is released, and the outcome names it as released.

**CE-F2 Shared by choice, refused by limit** (CE-2, CE-9):
- *Given* `child_environments: tasks`, `child_environments_max: 1`, one child running in
  its own environment.
- *When* the parent delegates a second task with `environment: shared`, and a third
  without it.
- *Then* the second runs in the session's environment; the third is refused with
  `limit_reached` naming `child_environments_max`, and nothing is queued.

**CE-F3 Waiting keeps the environment; closing releases it** (CE-4):
- *Given* a child that returned `partial` and asked its parent a question.
- *When* the parent answers with `message_child` after 20 minutes, the child continues
  and returns `complete`, and the parent's run ends.
- *Then* the environment stayed through the wait, and was released after the second
  hand-back; the session's close finds nothing to release.

*Amended 2026-10-26 (as built):* a child that returned `partial` has ended, and takes no
`message_child` (PD-44). As built, the fixture is: a child sends its partial finding with
`message_parent` and waits in `wait_agent_message`; while it waits, its environment
stays; the parent answers, the child continues and returns `complete`; the environment
is released after its one hand-back, and the session's close finds nothing to release.

*Amended 2026-10-26 by the owner, replacing the as-built reading above (PD-46):* the
fixture as first written holds: the parent's `message_child` reopens the returned
delegation in the same environment, the child returns `complete`, the parent accepts it,
and the environment is released after that acceptance's hand-back.

**CE-F4 Not ready in time** (CE-4):
- *Given* a profile with `schedule_timeout: 60`.
- *When* the gateway cannot place the sandbox within 60 s.
- *Then* the delegation ends `failed` with `environment_unavailable`, the environment is
  released, the parent's notice says so, and a new `delegate` is admitted.

## Open questions for the owner

1. **Default.** `child_environments: off` by default, so existing projects do not
   change; the owner turns on `tasks` per project. *Recommended:* off, until option C
   lands and seeding is cheap.
2. **Checks.** Keep checks in the session's environment unless `all`. *Recommended:* yes;
   a check that must compute heavily can be a task whose result the check reads.
3. **Hand-back target.** Copy into the parent's `work/<delegation_id>/`, or record into
   control only. *Recommended:* both as CE-6 says: retained artifacts are already in
   control; the copy exists for what the recorder could not retain and for the declared
   results, so the parent's next step can read them by path at once.
4. **Idle release.** Release an environment whose child has been waiting longer than a
   bound, and recreate it on the next message, or keep it until the parent closes the
   child. *Recommended:* keep it (the owner's framing: working memory "while they're
   running or waiting"), with a per-project `child_idle_max` the operator may set, off by
   default; a waiting child's cost is shown in the Agents panel.
5. **Counting.** Child environments outside `max_environments`, under their own caps, as
   job sandboxes are. *Recommended:* yes, so a worker's session capacity is not eaten by
   children, and the child caps are the one place an operator sizes them.
6. **Order of building.** Option C first (seconds to seed), then CE; or CE first with
   today's 69 to 290 s seeds. *Recommended:* CE's worker and records first, since they
   are independent of the image layout and can be qualified with `tasks` on one project;
   option C in parallel, as its own increment.

### Owner answers (2026-10-26)

All six as recommended: child environments are off by default, a project turns on `tasks`
or `all`; checks stay in the session's environment unless `all`; the hand-back goes both to
control and into the parent's `work/<delegation_id>/`; a waiting child keeps its environment,
with an optional `child_idle_max` off by default; child environments are counted apart from
`max_environments`, under their own caps; the worker and records are built first, and the
Python environment leaves the copied workspace (option C) as its own increment in parallel.

