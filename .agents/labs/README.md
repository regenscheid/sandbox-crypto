# babbage labs

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
