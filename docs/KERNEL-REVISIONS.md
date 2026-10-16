<!-- SPDX-License-Identifier: MIT -->
# Kernel start revisions

The worker records the package revision at a kernel's observed start and compares
it with the environment's current recorded revision (Conda provisioning
[CP-12 and CP-F7](specs/conda-provisioning.md)). A package change leaves existing
kernel state intact. A kernel started at revision 0 can therefore report start
revision 0, current revision 1 and `revision_status: "predates"` while retaining its
globals. A new kernel started after that change reports revision 1.

## What is recorded

`KernelInfo` adds `start_revision`, `current_revision` and `revision_status`.
Both revision numbers are nullable. The comparison is `current` when the known
numbers match, `predates` when the start is older, and `unknown` when either is
unknown. An incoherent start ahead of the current revision is reported as unknown.
These fields are informational and never authorize execution, package installation
or recovery.

The executor takes the existing managed package lock shared, observes the package
fingerprint, and holds that lock through process startup and Jupyter readiness.
Managed package commands cannot link packages in that interval. The existing CP-13
launch gate still encloses the work. Actual startup and its cleanup remain owned
through repeated cancellation, and the lock stays held until that work completes.
An ignored cell interrupt stops and reaps the kernel; replacement waits for the
next admitted cell, so recovery admission cannot be bypassed by an eager restart.

Each successful birth gets a fresh opaque identity and retains its fingerprint,
package epoch, launching execution and local generation. The executor increments
its package epoch on each reported package change. The worker associates bounded
hello/revision observations with its own revision numbers; equal fingerprints
from an earlier epoch cannot be mistaken for today's revision after an A→B→A
change. Missing, delayed or inconsistent associations remain unknown.

The worker keeps one bounded birth record per environment/session/language and
one package-epoch association per environment. A birth pin binds the trusted
project, environment and session, language, exact executor incarnation, generation
and birth identity. A known managed daemon also retains its recorded immutable VM
binding. Guest metadata cannot choose another project or session. Bounded SQL
prefixes and typed scalar projections prevent corrupted large metadata from being
allocated in Python. Corrupt birth history stays unknown rather than being
silently reconstructed.

`Execution.kernel` records the cell's kernel at its observed start, before the
state event's durable source acknowledgement, in one transaction with generation
mapping, the execution record and state event. Status and running-kernel summaries
only read these pins, so seeing a new kernel before its owning start event cannot
freeze a provisional unknown or consume its state-loss notice. `Result.kernel` retains that start
pin even if an explicit restart replaced the kernel during the cell, and compares
it with the revision recorded at completion. `Execution.kernel_state_lost` retains
the start observation's loss indication so generation mapping, terminal replay or
worker restart cannot erase it before `Result.state_lost`. The existing execution start revision
and overlap fields remain in `Location` and `Result`. This is package-state
observation, not proof of actual imported modules, producer identity or input
lineage. Direct arbitrary writes to package records can bypass the managed lock.

## Disclosure and scope

Ordinary shell, cell and background-job results include `running_kernels`, the session's own kernels (one per language) and those of its
delegated runs ([worker](WORKER.md#behavior-worth-knowing)), as observed at completion. Cell and
restart tool summaries include the kernel revision fields. A retained ordinary
tool result names kernels that predate the current revision and explains that
loaded state is retained until an explicit restart. These summaries are visible to
the agent and in the GUI's retained tool transcript. The workspace also polls a
session's running kernels through the observation route below
([workspace kernels](WORKSPACE-KERNELS.md)), and stateful MAIN runs are told about
kernels that predate a package change made from another session
([kernel check](#kernel-check-before-each-turn)).

`POST /v1/kernels/status` accepts only:

```json
{"context":{"project_id":"example","session_id":"workbook-a","environment_id":"example-env"}}
```

The authenticated project/environment checks are the existing worker checks. The
response names that environment and session, its current revision, and only that
session's running kernels. The typed `WorkerClient.kernel_status(context)` uses
this route and refuses mismatched response scopes or incoherent kernel lists.
It starts no research command or kernel and changes no package gate;
opening its executor connection uses the normal managed environment lifecycle.
An empty session returns an empty list. A legacy executor without the scoped
status protocol, or an invalid/oversized kernel reply, fails visibly.

### Existing-connection observations

`POST /v1/kernels/observe` accepts the same `KernelStatusRequest` and returns
`KernelObservation`: the same bounded environment/session/revision/kernel fields,
plus an aware `observed_at` timestamp assigned by the worker when it receives and
validates the reply. This timestamp is a host receipt time, not an atomic snapshot
of kernel or package state. The response is an observation; subsequent lifecycle
or package changes can make it stale.

Unlike explicit `/status`, observation requires an already connected executor. It
never opens, wakes, reconnects or recycles an environment, creates a kernel,
refreshes ordinary activity time, or increments the environment's busy counter.
It reads existing birth/generation pins without persisting a provisional birth or
consuming a state-loss notice. A readable CP-13 `needs_recovery` package hold stays
readable and unchanged.

Missing, closed, detaching, recovering, quarantined or recycle-fenced connections
are `unavailable`, rather than an empty kernel list. A busy lifecycle lock or active
session close also refuses observation. A healthy connected session with no
running kernels returns an empty list. The worker checks the exact environment
object, channel and incarnation again after the reply, including empty replies;
idle release and connection replacement can win while the query is pending. A
matching session close invalidates its active observer token even if close finishes
before the old reply arrives; a later fresh observation can read the empty session.
These tokens exist only for the at most eight active calls, with no retained session
epoch history.

Observer admission is immediate, with no queue: at most eight calls globally and
one per environment. The worker waits at most two seconds for the whole existing
channel RPC, including its transport write lock, then releases this observer's
admission on success, failure or cancellation. It holds no lifecycle lock during
the RPC, so repeated polling cannot hold up idle release. It neither stops the
executor nor cancels research work when an observation times out. Legacy executors
without the scoped kernel protocol fail visibly as `unsupported`.

`WorkerClient.observe_kernels(context)` uses only this route, a five-second total
HTTP deadline, an 8 KiB response/error limit and no redirects. It checks exact
response environment/session scope and the existing unique-language/current-revision
coherence rules. It never falls back to explicit `/status` when observation is
unavailable. The workspace's kernel panel and the harness's kernel check are its
callers; nothing restarts a kernel automatically.

A same-incarnation worker restart preserves a previously recorded exact birth pin
and kernel globals. First observation of a kernel that was already running,
including an unseen birth replayed after reconnect, does not infer a start revision
from today's packages. It remains unknown until a new observed birth. An explicit
restart establishes a fresh known start only when its observed epoch/fingerprint
matches the worker's retained revision association. Sandbox/incarnation loss keeps
the existing lifecycle and state-loss behavior. Confirmed environment deletion
forgets only that environment's birth/epoch metadata.

## Kernel check before each turn

A MAIN run with a state directory checks its session's kernels before each model
call (`KernelCheck` in
[kernel_check.py](../packages/orchestrator/src/sandbox_science/orchestrator/kernel_check.py),
a turn hook). It reads the existing-connection observation above, which never starts
or wakes a kernel. For each kernel whose status is `predates`, it adds one user
message, framed as platform data (`sandbox-kernels-1`), naming the kernel, its
generation and its start and current revisions. Each kernel and current revision is
told once per run; a later package change is told again. The notice IDs are in the
run record's `delivered["kernels"]`, and nothing is acknowledged: the check keeps no
state between runs, so the next run is told again while the kernel still predates
the packages. A failed or unavailable observation, or a kernel whose start revision
is unknown, tells nothing, and the run goes on. Every execution result carries the
same fact in `kernel_revision_notice`.

A main run's view of its session also names, before each earlier run's part that ran
cells, the kernels its cells used and whether each runs now, from the same observation
taken as the run starts (`sandbox-kernel-lines-1`; [context
management](specs/context-management.md), CM-29c).

Older run records may hold a `kernel_notice_delivery` receipt from the durable
inbox this check replaced; it is kept as read.

## Tests and labs

Local tests use the executor test backend and disposable prefixes. They cover real
Jupyter kernels keeping globals across a managed fake install, waiting for linking,
new-session and explicit-restart pins, scoped HTTP status, durable worker restart,
delayed or returning fingerprints, collision and corruption refusal, owned startup
cancellation, and, for observation, the read-only SQL state, an active package hold,
a paused kernel birth, the write-lock timeout, connection and lifecycle races,
cancellation and admission bounds, and HTTP scope, body, deadline and redirect
refusals.

On OpenShell, [Lab25](../.agents/docs/agent-reports/babbage-openshell-lab-25-2026-10-12.md)
(`.agents/labs/kernel_revision.py`) checked that a kernel keeps its globals and start
revision across a real package install, a new kernel starts at the new revision, and
both survive a same-VM worker restart until an explicit kernel restart.
[Lab26](../.agents/docs/agent-reports/babbage-openshell-lab-26-2026-10-12.md)
(`.agents/labs/kernel_observation.py`) checked that observation neither starts nor
wakes an environment, that an idle stop completes during continuous observation, and
that kernels and state-loss disclosure behave as described after startup recovery and
ordinary resume. Worker startup recovery reconnects remembered daemon endpoints
before serving the API.

Not built: automatic restoration of packages after a failed install, and uninstall.
