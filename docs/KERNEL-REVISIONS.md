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
state event's durable source acknowledgement. `Result.kernel` retains that start
pin even if an explicit restart replaced the kernel during the cell, and compares
it with the revision recorded at completion. The existing execution start revision
and overlap fields remain in `Location` and `Result`. This is package-state
observation, not proof of actual imported modules, producer identity or input
lineage. Direct arbitrary writes to package records can bypass the managed lock.

## Disclosure and scope

Ordinary shell, cell and background-job results include `running_kernels`, at most
the two supported languages in that session, as observed at completion. Cell and
restart tool summaries include the kernel revision fields. A retained ordinary
tool result names kernels that predate the current revision and explains that
loaded state is retained until an explicit restart. These summaries are visible to
the agent and in the GUI's retained tool transcript. There is no automatic live
GUI broadcast or cross-session notification transport in this slice.

`POST /v1/kernels/status` accepts only:

```json
{"context":{"project_id":"example","session_id":"workbook-a","environment_id":"example-env"}}
```

The authenticated project/environment checks are the existing worker checks. The
response names that environment and session, its current revision, and only that
session's running kernels. The typed `WorkerClient.kernel_status(context)` uses
this route. It starts no research command or kernel and changes no package gate;
opening its executor connection uses the normal managed environment lifecycle.
An empty session returns an empty list. A legacy executor without the scoped
status protocol, or an invalid/oversized kernel reply, fails visibly.

A same-incarnation worker restart preserves a previously recorded exact birth pin
and kernel globals. First observation of a kernel that was already running,
including an unseen birth replayed after reconnect, does not infer a start revision
from today's packages. It remains unknown until a new observed birth. An explicit
restart establishes a fresh known start only when its observed epoch/fingerprint
matches the worker's retained revision association. Sandbox/incarnation loss keeps
the existing lifecycle and state-loss behavior. Confirmed environment deletion
forgets only that environment's birth/epoch metadata.

## Acceptance boundary

Focused local tests use only the existing executor test backend and disposable
prefixes. They cover real Jupyter kernels retaining globals across a managed fake
install, waiting for linking, new-session and explicit-restart pins, scoped HTTP
status, durable worker restart, delayed/returning fingerprints, collision and
corruption refusal, and owned startup cancellation. Local tests are not OpenShell
runtime acceptance.

For root-owned disposable CP-F7 acceptance:

1. Start session A's Python kernel; set a synthetic global such as `cp12_value =
   1729`. Retain its execution, kernel generation/start revision, daemon and VM
   identities.
2. In the same disposable environment, use the existing permitted provisioning
   path to install a package not already present (for example `networkx` if the
   selected minimal image lacks it). Retain the exact operation, successful probe
   and changed revision. Do not weaken the package gate or reuse real credentials.
3. Query A's scoped kernel status and run `print(cp12_value)`. Its original start
   must remain older than the current revision. Start B's first Python cell and
   verify its start/current revisions match the new revision. Query an empty
   session and an unauthorized project; neither may disclose A or B.
4. Gracefully restart only the disposable worker service. Requery A and its global;
   the exact original start/generation and same daemon/VM must remain. Then
   explicitly restart A and verify its new start is current and the old global is
   gone. Preserve the old execution's immutable kernel pin.
5. Tear down all disposable resources and archive exact source/runtime identities,
   results and cleanup evidence. Root owns this lab; no author-started services,
   VMs or models are part of local validation.

Runtime acceptance, independent exact-head review, fresh CI and merge remain
pending until their evidence is recorded centrally. Automatic recovery/restoration,
uninstall/upgrade, package UI and live cross-session notification remain separate
backlog work.
