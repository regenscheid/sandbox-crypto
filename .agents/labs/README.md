# babbage labs

Lab32's `provisioning_revision_packages.py` reads exact recorded package/license
inventories. Root-owned acceptance on signed `6d4c448`/helper `c85126d4` passed
worker/workspace/native paging and separately qualified stopped-VM reads. The old
helper failed on legitimate executor-endpoint retirement; its idle-read never ran.
External reviewed `5c830a3a`/reader `0f2bfc5e` proved 30 paired reads with truthful
empty current endpoints and unchanged SQL between original-VM Stopped proofs.
All owned runtime/private state is removed and independently audited. Corrected
helper `5c830a3a` was adopted only after teardown, with scoped checks and eleven
retained-row AST replays; no second corrected-helper runtime is claimed. Root
has recorded the [Lab32 report and exact archive](../docs/agent-reports/babbage-openshell-lab-32-2026-10-13.md); final independent delivery review and fresh CI remain.

Scripts for running the platform against a real OpenShell gateway on babbage, as labs 9 to 20 did. Each lab's report is in [`../docs/agent-reports/`](../docs/agent-reports/). These scripts are agents' tooling, not product code: they assume babbage's setup and are kept here so that the next lab does not start from nothing.

## Prerequisites

- OpenShell 0.1.3 unpacked at `~/.local/opt/openshell-0.1.3/bin` (gateway, driver and CLI).
- The images in the local Docker store, which OpenShell's VM driver reads directly: `sandbox-science/minimal:02f85b3`, `sandbox-science/crypto-base:02f85b3`, `sandbox-science/mcp-proxy:d27e41a` and `sandbox-science/orchestrator:174e72e` (built from `images/`; see [images](../../docs/IMAGES.md)). Only create, tag or remove `sandbox-science/*` images: the Docker store is shared with the user's own projects, and is never pruned.
- `uv sync --locked` in the repository, whose `.venv` the scripts run (`R`, by default the repository these scripts are in).

## Running a lab

All state goes under `~/.local/state/openshell-lab`, and every service runs as a transient `systemd-run --user` unit named `openshell-lab-*`. Nothing persistent is installed.

1. `bash lab_up.sh`: the gateway (VM driver, mTLS), the policy middleware and the package cache, on loopback ports 17670, 50051 and 8090. It fails if the lab directory exists or a port is busy.
2. `source ~/.local/state/openshell-lab/env.sh` in each shell that runs `openshell`. It points `XDG_CONFIG_HOME` and the other XDG directories at the lab, so **`gh` does not work in that shell**: run `gh` in another.
3. The example policy names the provider `github-readonly`; register it with a synthetic token before creating environments:

   ```bash
   openshell provider profile import -f examples/openshell/profiles/github-token.yaml
   GITHUB_TOKEN="test-secret-lab-github-$(openssl rand -hex 8)" openshell provider create --name github-readonly --type github-token --credential GITHUB_TOKEN
   ```
4. `bash worker_up.sh`: the worker on 127.0.0.1:8100 with `[openshell.environments]`, a client limited to `$PROJECT` (default `lab17`, token in `$LAB/worker/client-a.token`) and the example policy granted to that project (digest in `$LAB/policy.digest`).
5. Checks, through the worker's API (they use project `lab17`, environments `lab17-a` and `lab17-c`):
   - `python -I checks.py $LAB/worker/client-a.token $(cat $LAB/policy.digest) envs sync mmap packages cancel idioms orphans revisions`: any subset of the parts, in order. `envs` creates the two environments first.
   - `python -I budget.py $LAB/worker/client-a.token header|budget`: the cache's per-sandbox header and budgets (restart the cache with `sandbox_rate = 0.5` and `sandbox_burst = 2` in `$LAB/cache.toml` for `budget`, then restore it).
   - `bash forward.sh`: the MCP proxy in its own sandbox, with its forward run as the example unit runs it, across a stop and start.
6. To test a branch before it merges, restart the worker unit from that branch's worktree (`<worktree>/.venv/bin/sandbox-science-worker serve --settings worker.toml`, working directory `$LAB/worker`). Do not switch branches in the main checkout while a lab runs: the cache and the middleware run from its editable install.
7. `bash lab_down.sh` deletes every sandbox, stops every lab unit (the forward unit included, which restarts forever otherwise), removes the lab directory and checks that no process, port, OpenShell path or driver socket is left. Labs are always torn down completely.

## Provisioning (lab 18)

Start a fresh lab as above, register the synthetic provider, and run the worker with
`PROJECT=lab18 bash worker_up.sh`. The worker offers both `minimal` and `crypto-base`.
From the repository root, run:

```bash
.venv/bin/python -I .agents/labs/provision.py envs basics pip approvals pins probes cancel restart
```

This creates separate disposable environments and checks the real dry runs, pinned
installs, lockfiles, capability probes, independent histories, channel and AGPL refusals,
and an escalated install denied then approved. It deliberately answers the lab's permission
requests through the operator CLI, fetches a file outside an active grant, and checks that
the grant ends with its operation. The catalog checks add `python-flint` to `crypto-base`
before running all nine probes. The last two parts observe new `conda-meta` records through
OpenShell before cancelling an install or restarting the lab worker; both must leave a
visible `needs_recovery` status. Run them only against this disposable lab worker.

The script writes API results under `$LAB/evidence/`, without the client token. Keep the
observations needed for the report before `lab_down.sh` removes that directory. Parts may
be selected separately; `verify` resumes checks after the two initial capability installs.
The full sequence expects fresh environments. Tear the lab down as above after finishing.

## Background jobs (lab 19)

Start a fresh lab with `PROJECT=lab19 bash worker_up.sh`. Set `suspend_after = 3`
in the disposable worker settings and restart its unit before the idle check.
Run `.venv/bin/python -I .agents/labs/jobs.py envs basics output limits close delete idle restart`
from the branch under test. The last part deliberately restarts the lab worker.
For harness/browser checks, run `.agents/labs/job_harness.py start` with that
venv, serve `web/workspace/dist` with the workspace CLI pointing to `$LAB/orch`
and the lab worker, inspect the `lab19/gui` session and cancel its job in the GUI,
then run `job_harness.py notices` in a fresh process. Evidence is in `$LAB/evidence`;
copy the token-free records before teardown. The watcher is scripted with shortened
intervals; this does not test a real model's judgment.

## Executor recovery (lab 20)

Start a fresh disposable lab and worker with `PROJECT=lab20 bash worker_up.sh`.
Keep daemon transport enabled. Run from the isolated branch under test:

```bash
.venv/bin/python -I .agents/labs/recovery.py envs running offline normal refused cancelled kernel noisy timeout daemon_loss sandbox_loss idle_daemon_loss
```

The core checks deliberately stop/restart only the worker. The loss checks kill
the unique sandbox daemon after checking its PID/start identity, delete a sandbox,
and check an idle endpoint with no unfinished executions. They must only target
this disposable lab. A same-incarnation reconnect must fill any legacy endpoint's
original sandbox name and immutable ID before testing daemon loss; inspect
`executor_endpoints` in the disposable worker database and confirm no pending
`executor_recycles` intent. Killing before that backfill correctly refuses
automatic replacement of an unidentified legacy binding.

During a managed VM fence the script retries only definite fenced HTTP 503
refusals under the same operation ID, for at most 60 seconds. Other failures
propagate. Evidence distinguishes boot/start identity from numeric PID reuse and
checks preserved workspace/Python-prefix files, lost old kernel state, and an
unaffected second environment. Copy `$LAB/evidence/recovery.json` before teardown;
retain failed probes separately, as [lab 20's report](../docs/agent-reports/babbage-openshell-lab-20-2026-10-12.md) does.

## Reports

Write the report as `.agents/docs/agent-reports/babbage-openshell-lab-N-<date>.md`, in the style of the earlier ones (what ran where, results tables, findings, cleanup), add a line to `.agents/docs/README.md`, and add the file to `.agents/docs/INVENTORY.json` with its size and SHA-256 (rebuild the README's entry too). Cite the lab where docs state what it showed. Tokens are synthetic and never written to the report or the logs kept.

## Failed-run CLI and browser acceptance

Run `uv run python .agents/labs/failed_run.py` from the source worktree. It creates a marked `failed-run-browser-*` temporary directory and drives the actual CLI against synthetic loopback HTTP peers: a completed file read followed by a hostile model error. No research code, real model or VM runs. It verifies exit 1, exact partial transcript retention, absent provider-error text and synthetic credentials, and owned peer/child cleanup. Serve the printed state directory with the fresh production workspace build; verify Failed run, partial transcript, retained tool content and no Answer heading. Close the tab, stop the transient server and remove only the marked fixture directory.

[Acceptance on 2026-10-12](../docs/agent-reports/failed-run-records-2026-10-12.md) records signed source, exact CLI/browser evidence, final gates and complete teardown.

## Original-LaTeX clipboard acceptance

Run `uv run python .agents/labs/math_copy.py --static web/workspace/dist --info /tmp/math-copy-browser-info.json --cleanup /tmp/math-copy-browser-cleanup.json` on the frozen production build. The helper creates marked disposable project/document state and serves the actual workspace plus a synthetic loopback repair peer. Its random login URL is written only to the private info file. No real model, worker, VM or research execution is used.

Open that login URL, then the fixture document. Use real keyboard Copy and paste into the document editor's unsaved source field to verify inline/display, mixed prose, table cells, partial selections, heuristic/model repair, Unicode fallback and skipped formulas. Compare against the saved source, including delimiters and body whitespace, with Markdown quote/list structure excluded from the formula itself. Ordinary editor/code copying must keep its normal behavior. Do not save pasted acceptance text. Close the tab and send SIGTERM to the owned helper; it closes both listeners, removes private info/marked state and writes a public cleanup record. Remove only the owned public cleanup file after archiving the result.

## Document editing browser acceptance

This control-store slice needs no OpenShell VM. Run
`uv run python .agents/labs/document_editing.py create FRESH_STATE_DIRECTORY`,
then serve that directory with the production workspace build on loopback.
In the browser, keep a block or source draft open while running the helper's
`agent` mode against the same disposable directory; verify conflict retention
and deliberate resolution, retained-patch preview, historical restore and
workbook promotion. The `record` mode exports token-free revisions and events.
Close the tab, stop the transient server and remove the fixture directory.
The [dated report](../docs/agent-reports/document-editing-2026-10-12.md) records
the interactions and limits.

## Document comments browser acceptance

`uv run python -I .agents/labs/document_comments.py create` prints a fresh marked
synthetic state directory and document route. Serve that directory with the
production workspace build on loopback. Post a formula comment addressed to a
session, then run `respond STATE` for a scripted document-only main run. Keep an
unsaved reply while `history STATE THREAD_ID` appends 55 entries; verify retained
base revision, pagination and explicit adoption of the current thread revision.
`record STATE` exports research records without authentication state. Close the
tab, stop the transient server and remove the marked directory. The
[report](../docs/agent-reports/document-comments-2026-10-12.md) records acceptance.

## Consistency review browser acceptance

`uv run python -I .agents/labs/consistency_review.py create` creates a fresh marked
synthetic state and prints its IDs. The `serve --state STATE --static web/workspace/dist`
mode serves a production build on loopback, with only a fixed synthetic login token.
An optional `--delay-review ID` adds two-second evidence/audit responses to exercise
selection races. Use `fix --state STATE` to produce the exact corrected revision,
claim that revision in the GUI, then `confirm --state STATE` for a real constrained
ScriptedModel runner. Confirmation requires its checked-item record, exact source/fix
bytes and pins; it asserts the unrelated author retry is unchanged. `reopen` simulates
a concurrent USER change, and `record` exports token-free research state and audit.
Close the tab, stop the transient server and remove only the marked synthetic state.
The [report](../docs/agent-reports/consistency-review-2026-10-12.md) records the initial
coverage issue, correction, browser interactions and limits.


## Lab21: sticky recovery admission

The source helper is [`recovery_gate.py`](recovery_gate.py); the reviewed-tree setup,
positive/negative checks, explicit offline variant and teardown boundary are in
[`RECOVERY-FENCE.md`](../../docs/RECOVERY-FENCE.md#disposable-acceptance-recipe).
Root coordinates the run only after exact-source review. The helper's marker and
lab21 client scope are mandatory; it never runs against real project data. Keep
both source checkouts unchanged while lab worker/middleware/cache processes use them.

The [Lab21 report](../docs/agent-reports/babbage-openshell-lab-21-2026-10-12.md) records both runs.
The predecessor cancel/restart run used signed `ef77f6d`; the final fresh graceful
worker-offline and confirmed delete/reuse run used signed `e320236`. Root verified
both labs completely removed, with the final independent teardown at 12:13:54 UTC.
Real managed VMs proved preserved kernel globals/boot identity, queued-start
refusal and missing markers, recovery files/history/lockfile access and a healthy
peer. The final explicit rebuild used a new immutable sandbox/boot and admitted
fresh work. Legacy unknown-binding/replacement and abrupt-death cases remain
adversarial local evidence, distinct from the real graceful-stop run.

## Lab22: bounded artifact capture

The [Lab22 report](../docs/agent-reports/babbage-openshell-lab-22-2026-10-12.md) records
the successful run on signed `b535856` and Python 3.14.8: exact binary/empty retention,
twelve prompt oversized/symlink/FIFO refusals, repeated healthy publication and ordinary
read/execution access. Independent inspection found zero capture leases/helpers; complete
teardown was verified at 12:36:45 UTC. [Retained evidence](../docs/agent-reports/babbage-openshell-lab-22-evidence-2026-10-12.json)
keeps 16 exact text records. The following recipe remains reusable for a new disposable run.

Root coordinates the disposable run after exact source review. Start `lab_up.sh`
and `PROJECT=lab22 worker_up.sh` from the frozen reviewed tree. Stop only its
transient worker, add top-level `max_artifact_size = 1048576` to that disposable
`worker/worker.toml`, then restart that same lab worker. The default production
setting is unchanged. Run:

```bash
.venv/bin/python -I .agents/labs/artifact_capture.py init prepare verify
```

The mandatory marker, lab22-scoped synthetic client and exact 1 MiB setting guard
the helper. It checks exact binary/empty content hashes and retained bytes,
regular/sparse oversize and symlink/FIFO refusal within ten seconds, repeated
failure/success admission, healthy executions and ordinary file reads. It starts
no services and changes only its fresh managed sandbox/session. Record the actual
guest Python version; current runtime images use 3.14 while supported CI is 3.12/3.13.
Archive `evidence/artifact-capture.json`, worker logs and explicit teardown evidence,
then use `lab_down.sh` to remove all Lab22 sandboxes/services/state. Lost-bridge,
blocked host-thread and corrupt-state tests remain local hostile probes, not claims
about induced OpenShell failures. No arbitrary stuck filesystem-I/O bound is asserted.

## Lab24: worker-local output observations

Root-owned [Lab24 acceptance](../docs/agent-reports/babbage-openshell-lab-24-2026-10-12.md)
passed on signed `35863776` / helper `bcddecd3`, with nineteen terminal markers,
eighteen bounded reports, partial outputs, raw path/root identity and same-attempt/
kernel continuity through graceful worker restart. PDF was unavailable. The
[archive](../docs/agent-reports/babbage-openshell-lab-24-evidence-2026-10-12.json)
retains source, gate, API, independent inspection and complete teardown evidence;
cleanup was independently verified at 14:59:02.325873 UTC.

For a separately coordinated rerun, root owns setup, exact signed-source freeze,
execution, evidence archiving and full teardown; authors must not start a lab themselves.
Use `lab_up.sh` and `PROJECT=lab24 worker_up.sh` from the frozen reviewed checkout,
then run:

```bash
.venv/bin/python -I .agents/labs/output_observations.py init prepare verify checkpoint running
```

Root then gracefully stops/restarts only that disposable transient worker, retaining
its state and VM. Do not run another worker against that database. Once ready, run:

```bash
.venv/bin/python -I .agents/labs/output_observations.py resume restart_verify
```

The mandatory project/environment marker and lab-scoped settings guard every request
and read. The helper starts/stops no service. It exercises successful/failed/cancelled
shell, cell and job paths, timeout, raw invalid-UTF8/root-prefix collisions,
`not_seen_after`, explicit missing/truncated gaps and healthy execution. If the actual
profile advertises PDF, it renders a fixed synthetic PDF inside the sandbox and checks
its output observation; otherwise evidence explicitly records PDF as unavailable.
Only the marked operator fixture opens `worker/state/worker.sqlite3` read-only and
uses the internal scoped reader; no public feed is added. It checks original immutable
VM/attempt pins, same-attempt restart without command replay, exact retained prefix,
scoped stream identity and unknown producer/input fields. Every exercised outcome
requires its report except a request refused before start; known partial file paths
and metadata are required for failed, cancelled and timed-out work. A synthetic
Python global must remain usable after cancellation and after the worker restart.
The helper retains the truncation report, then removes only its known 300 fixture
files before checkpoint/recovery so the recovered path fits below the scan cap.

Archive `evidence/output-observations.json`, logs, source/helper hashes and explicit
teardown evidence, then remove every Lab24 VM/service/state with `lab_down.sh`.
Damaged derived SQL, quota/eviction, invalid sidecars and transport uncertainty remain
local hostile proof unless separately induced and retained in the real lab. This
acceptance cannot prove every file mutation, writer closure, producer/input lineage,
attempt-end bytes, automatic inventory or bounded legacy directory-walk I/O.

## Lab23: external backend and research-profile conformance

The [Lab23 report](../docs/agent-reports/babbage-openshell-lab-23-2026-10-12.md) and
[exact evidence](../docs/agent-reports/babbage-openshell-lab-23-evidence-2026-10-12.json)
record root's unchanged conformance suite run on frozen main `73c07aa`. All 23
external cases passed on each linux/amd64 profile, minimal and crypto-base; the
23 local variants per run were intentionally skipped. Both actual Python 3.14.8
guests retained effective/declared tier 3. Root verified full teardown and both
owned fixture removals at 12:46:22 UTC. The suite tested Python, not Sage.

For a new explicitly coordinated disposable run, create separate worker-managed
environments for project `conformance`, one per profile, then run the existing
[`SS_CONFORMANCE_*` recipe](../../docs/WORKER.md#conformance-suite) against each.
Keep source/tests frozen, retain pytest/JUnit/capability/state records, and remove
both VMs, services, lab state and only the owned fixture roots afterward. No new
helper or service setup is introduced here. Lab23 validates old main separately
from capture Lab22 and adds no capture fault/ownership claim or platform parity.

## Disposable comment/reply math browser fixture

See [comment math](../../docs/COMMENT-MATH.md#disposable-acceptance-fixture). `.agents/labs/comment_math.py create` requires explicit absolute Node/launcher/bundle paths and creates marked temporary synthetic state only. `main-fix`, `concurrent-user-edit` and `audit` require its marker before opening a writer. Actual local KaTeX runs; no model/worker/service is contacted. This is a browser fixture, not an OpenShell lab; coordinate any browser/server separately.

Root-owned [fresh acceptance](../docs/agent-reports/comment-math-2026-10-12.md)
passed on signed `77ecc171` / helper `972b459b`, after the predecessor run exposed
and prompted correction of dependency-only reflag accounting. The strengthened
fixture requires unchanged opening/reply budgets zero and two failed body edits
at two. Source checks passed 9,631 Python tests (43 opt-in skips, two existing
warnings), 441 web tests and 24 post-build math checks, plus all required checks
and both builds. Independent body/reply drafts, stale CAS/adoption, provenance,
accepted/removal/unavailable evidence and session isolation passed in the browser.
The tab, transient server and marked state were removed at 13:50:58.272097 UTC.


`response_math.py` creates marked disposable synthetic MAIN fix/rebut explanation
sources with actual operator-configured local KaTeX. It exercises six child states,
exact original/current attribution, stale reviewer body pins, independent scientific
confirmation and session isolation. Root owns the native browser/server/teardown;
this helper starts no model or worker and grants no USER explanation editing.

The [finding-prose browser report](../docs/agent-reports/finding-prose-math-2026-10-12.md)
records root-owned acceptance on signed `f7bb0a3`, helper SHA
`7bc82ebf57e01b7905327dabdd2cc558896b94374ea97db06c4f8ec08a92d94b`.
The local renderer confirmed only child math findings; scientific confirmation
used a fresh pinned reviewer fixture and retained an inconclusive overall verdict.
Complete disposable teardown was independently verified at 15:37:06.734290 UTC.


## Lab25: kernel start revisions

Root-owned [Lab25 acceptance](../docs/agent-reports/babbage-openshell-lab-25-2026-10-12.md) passed on signed `629e27d`, using [`kernel_revision.py`](kernel_revision.py), SHA `132aee5c`. A real networkx install changed revision 0→1; A retained its start/global, B started current, graceful worker restart preserved the same VM/kernel, and explicit kernel restart removed the old global. Full teardown was independently verified. This is sequential CP-F7 kernel-revision continuation, not a new concurrency/overlap rerun.

For a separately coordinated fresh run, root freezes the reviewed source and starts `lab_up.sh` followed by `PROJECT=lab25 worker_up.sh`. Supply only visibly synthetic provider credentials through private lab state. Run `.venv/bin/python -I .agents/labs/kernel_revision.py init before`. Gracefully stop/restart only the owned worker, keep its state/VM, wait for its API listener, then run the same helper with `after`. Retain endpoint incarnation/immutable VM bindings before and after. Copy only token-free evidence before `lab_down.sh`; verify all owned units, VM, ports, state and driver sockets removed. The helper starts no services and accepts only its marked lab25 scope.


## Project macro browser fixture (2026-10-12)

The root-owned [`project_macros_browser.py`](project_macros_browser.py) helper serves an existing production GUI from a marked temporary state directory, seeds synthetic projects/documents/comments and emits a private 0600 login file. It starts no VM, worker or model. Optional mechanical acceptance requires the exact Node binary, built `check-math.mjs` launcher and `mathcheck.mjs` bundle paths and SHA-256 values. Root must independently freeze/check source and built bytes; the source label is descriptive, not an internal Git guard. Use only its owned loopback listener, keep private URLs/tokens out of exported records, and SIGTERM the helper after acceptance. Verify its exact PID/listener, marked root and private file are removed. The [acceptance report](../docs/agent-reports/project-macros-2026-10-12.md) records both fresh runs, the corrected launcher/bundle invocation and complete cleanup.


## Lab26: passive kernel observations

Root-owned [Lab26](../docs/agent-reports/babbage-openshell-lab-26-2026-10-12.md) passed on signed `a6bb469`, helper SHA `33abd17b`. For a separately coordinated fresh run, freeze reviewed source, start the owned lab and `PROJECT=lab26 worker_up.sh`, then run `.venv/bin/python -I .agents/labs/kernel_observation.py init` and `active`. Gracefully restart only the worker without idle suspension, wait for its listener, and run `restarted`; startup recovery already reconnects remembered endpoints. Set `suspend_after=3` only in the owned settings, restart/wait and immediately run `reconnected`, which includes warmup and idle polling in one process. Independently confirm actual gateway `Stopped` phase during its stopped interval. Retain only safe projections/API records, use `lab_down.sh`, and verify complete VM/unit/port/state/socket removal. The helper starts no services and accepts only its marked lab26 scope.


## Lab28: passive original kernel lineage

[Lab28](../docs/agent-reports/babbage-openshell-lab-28-2026-10-12.md) passed on signed `1deb9b7`, helper [`kernel_lineage.py`](kernel_lineage.py) SHA `4d90664b`. For a separately coordinated fresh run, freeze a reviewed clean source and use that exact head with every stage: `.venv/bin/python -I .agents/labs/kernel_lineage.py init --head <exact-head>`, then `before`, `restarted`, `idle`, and `resume` with the same argument. Root starts the owned lab with the documented imported provider profile and obviously synthetic provider, then `PROJECT=lab28 worker_up.sh`. Gracefully restart only its worker between `before` and `restarted`; set owned idle suspension to three seconds and restart/wait before `idle`. During the stopped interval independently retain the exact original gateway VM name/immutable ID/Stopped projection tied to the helper run/source/SHA, without raw credential annotations, before `resume`. A initiates the package change; B is fresh afterward. Missing/legacy/corrupt proof remains local-fixture coverage, and no MAIN notice delivery is tested. Copy token-free evidence before `lab_down.sh`; independently verify complete owned units/VM/listeners/state/XDG/socket removal.

## Synthetic session-plan browser fixture

`session_plans_browser.py` serves the frozen production workspace on loopback over
an owned marked temporary control store. It starts no worker, model or VM. Root
uses `serve --info /tmp/OWNED-EVIDENCE/private.json --cleanup
/tmp/OWNED-EVIDENCE/cleanup.json`, then fixed `inspect`, `in-progress`, `revise`
and `finish --fixture /tmp/session-plans-browser-...` actions. The evidence
directory must be current-user owned; the exclusive private info file holds the
synthetic login URL and must never be archived. Public startup pins source,
helper, assets and synthetic scopes. SIGTERM drains the app, removes only owned
state/private info and records cleanup. See [acceptance](../docs/agent-reports/session-planning-2026-10-12.md).

## Lab29: explicit MAIN kernel notice delivery

[`kernel_main.py`](kernel_main.py) is the owned, source-pinned component/adapter helper for [Lab29](../docs/agent-reports/babbage-openshell-lab-29-2026-10-13.md). Its fixed stages are `init`, `before`, `flows`, `restarted`, `reopen`, `idle-resume` and `inspect`; each is invoked as `uv run python .agents/labs/kernel_main.py <stage> --head <exact-signed-clean-runtime-head>`. A new run requires independent source/helper clearance and the standard fresh marked lab/provider/profile/worker setup. The canonical control store creates the environment and sessions; no arbitrary project/VM/command selector is accepted.

The root operator owns graceful worker restarts and the separate bounded safe gateway projection. `idle-resume` waits for that exact dated stopped-VM proof, rechecks bounded SQL, then admits one fixed harmless resume cell. Scripted models retain actual call inputs; production stores/controller/loop and CLI `_keep` are exercised, not a full interactive CLI or external model. Snapshot SQLite connections are explicitly closed. Cancellation after admitted save drains actual work and can leave a saved unACKed audit.

Export only the helper's bounded public evidence and exact durable run files, with byte hashes and live worker-credential literal exclusion. Never export or hash provider raw registration, worker token/config, binding key/check/config pins or raw gateway annotations. Independently verify the all-workspace inventory is the exact owned VM before the standard scoped teardown; remove the new private registration only by its exact metadata identity. Root and independent cleanup must prove owned units, original PID/start identities, ports, sockets and marked state are gone. Historical or unrelated private files are outside the run. Preserve failed and corrected records separately; the report defines what actual runtime, focused source coverage and reused gates establish.

## Lab30: read-only provisioning workspace

[`provisioning_workspace.py`](provisioning_workspace.py) is a fixed, source-pinned root acceptance helper. Stages are `init`, `serve`, `initial`, `install`, `permission-wait`, `permission-finish`, `idle-inspect` and `inspect`, each with `--head <exact-signed-clean-head>`. Root owns the fresh lab setup, normal and escalation policy digests, synthetic provider, operator decisions, worker lifecycle, sanitized gateway proof, native browser and complete teardown. Canonical ProjectStore IDs select shared/dedicated environments; the helper has no arbitrary target or operator decision command.

The helper uses production worker/workspace components and bounded typed GET parity/pages. It submits fixed GP and python-flint/queued-GP fixture requests; the root operator answers exact permissions separately. Worker-offline503 and live-worker stopped-VM durable records are distinct. The stopped proof must name the two original managed VMs, exact run/head and a fresh UTC time after terminal provisioning. Public evidence excludes private login/token/config/provider output and raw gateway annotations; private login cleanup owns the exact inode even when write, fsync or descriptor wrapping fails. Actual counts and license/probe flags must be observed, not assumed from guard tests. No full CP-26, recovery, scientific or new operator authority claim follows from this helper.

Lab30 first real attempt at `f964a050` is explicitly partial: GP installation and retained records passed, but the post-install ready overview failed on SQLite empty-BLOB slicing. All owned runtime/private state was removed and independently checked before source changes. Corrected helper acceptance now requires paired worker/GUI ready overview, empty queue and exact current revision immediately after install and after the permission queue drains. Preserve original failed evidence; use a new clean source freeze and fresh lab after worker correction/review/gates.

## Opt-in planning/progress evaluation

[`planning_progress_evaluation.py`](planning_progress_evaluation.py) defaults to a
standard-library description with no production imports or external effects.
Its separately selected active modes measure a fixed synthetic plan-tool task
through production `run_task` and canonical progress reads; all non-plan calls
are refused before dispatch. Root's positive and missing-progress scripted
controls completed on 2026-10-13, with the expected pass and progress refusal;
both fixtures were removed and independently checked. See the
[calibration report](../docs/agent-reports/planning-evaluation-2026-10-13.md).
Root owns any finite-budget model pilot with explicit private configuration;
none has run. These controls do not establish model compliance or actual
worker/progress ordering. See
[the bounded recipe and limits](../../docs/PLANNING-EVALUATION.md).

## Lab33: internal child-kernel dispatcher

[`run_kernel_dispatch.py`](run_kernel_dispatch.py) defaults to a description with
no production import or runtime effect. Active `prepare`, `run` and read-only
`inspect` require `--head <exact-signed-clean-runtime-head> --admit`, a fixed
owned lab and explicit root coordination. Corrected helper-only `91125f4` is integrated at
`24eac09`, with independent helper/setup/collector source clearance and exact
original/ready/done producer-to-collector checks. That is the historical source-readiness checkpoint. Later actual acceptance and complete teardown passed; exact runtime pins and limits are retained in [the Lab33 report](../docs/agent-reports/babbage-openshell-lab-33-2026-10-13.md).

Root freezes source/helper, starts only the normal gateway/cache/middleware and
registers the documented endpoint-free profile with a visibly synthetic provider.
Use fixed private settings with no token files, clients, worker HTTP server or
automatic suspender. Helper processes themselves require a fresh explicit
allowlist environment; backend child environment configuration alone does not
strip inherited model/provider credentials. Actual CLI/app lifecycle assembly
uses one manager/dispatcher and drains borrowed owners before stores close.

The fixed fixture uses two canonical shared sessions and three actual generated
child records, at most 20 Python cells, eight child controls, one session close
and one original managed VM. Root must supply fresh safe gateway proofs through
the fixed stopped-ready/before/reads-done/after handshake, bound to the exact
source/helper/run/original VM. Thirty no-wake reads use the same initialized
owner, actual retired endpoint and unchanged existing stores. Bracketing gateway
observations prove two observed phases, not continuous VM state. Preserve unknown
original intents for inspection; never repeat a whole run or reseed lost history.
Export only bounded public evidence, scan the synthetic literal in memory and
verify complete owned VM/unit/process/listener/private-state teardown independently.
This fixture proves no authenticated delegation controller, model launcher, grant,
inter-kernel filesystem isolation or scientific result.

## Lab35: installer observations and provisioning events

[`provisioning_observations.py`](provisioning_observations.py) defaults to an inert
description. Explicit `run --head <signed-clean-head> --admit` requires the
reviewed original isolated root setup and source bindings; it is not a retry or
arbitrary-command interface. [Lab35](../docs/agent-reports/babbage-openshell-lab-35-2026-10-13.md)
passed at runtime `d236b6b`/helper `fbbe3b61` in52.285s, with17 checked public
records and independently verified complete teardown. Source/gates and actual
runtime evidence remain pinned separately. PR #121 is delivered at `d2fe8fa` after all four fresh checks passed (run `38510468628`).

One original minimal VM and actual worker/client composition run scalar35 before
python-flint and wheel-only ecdsa0.19.1, under the existing policy/cache. The helper
admits at most16 actual executions/two unique operations/two installs, plus one
explicit known-terminal replay, within1800s and finite operation caps. No operator
answer, model/provider request or host execution is enabled. The normal policy
attaches only the synthetic github-readonly fixture. Unknown effects hold their
original identities and never authorize automatic re-execution.

Original versions, sanitized artifact bytes/scopes and safe EventStore projections
are checked through the actual production interfaces. All three memory-only
client literals are excluded before every post-token public write; the pre-token
marker records count0 separately. Root checks only its original provider-file
literal in memory, then exports checked records and performs exact held-FD owned
teardown. Private settings/provider contents and raw execution output are never
exported or hashed. The helper is intentionally tied to its recorded product
inputs; a later changed product tree requires separate review and repinning.
