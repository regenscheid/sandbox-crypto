# babbage labs

Scripts for running the platform against a real OpenShell gateway on babbage, as labs 9 to 17 did. Each lab's report is in [`../docs/agent-reports/`](../docs/agent-reports/). These scripts are agents' tooling, not product code: they assume babbage's setup and are kept here so that the next lab does not start from nothing.

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
