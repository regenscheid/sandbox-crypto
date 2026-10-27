# The personal instance on babbage

The gateway URL shown below is a reserved example placeholder. Keep the real endpoint and key
path in the private configuration outside the repository.

The owner's own Sandbox Science on babbage: a [`sandbox-science up`](QUICKSTART.md) instance that runs for research, apart from the labs. It has its own OpenShell gateway, its own [worker-side unit](UNIT.md) (the worker, policy middleware and package cache as one supervised group), its own units, ports and state, and its own config outside the repository. The labs' scripts (`.agents/labs/lab_up.sh`, `lab_down.sh`, the `cm_lab` kit) never start, stop or remove any of it.

Like the labs' scripts, this is babbage's setup: it assumes OpenShell 0.1.5 at `~/.local/opt/openshell-0.1.5/bin`, the images in the local Docker store, and the repository's `.venv`. Install v0.1.5's release binaries there, all from that one release: its VM driver takes `--parent-liveness-stdin` where v0.1.3's took `--parent-liveness-fd`, so a gateway cannot start a driver from another release. The release is one constant, `OPENSHELL_VERSION` in `packages/contracts/src/sandbox_science/contracts/openshell_version.py`: `personal_up.sh` reads it for the default `OS_BIN` and starts nothing when `openshell --version` names another release, and `sandbox-science up` and the runtime manager refuse such a CLI the same way.

## What runs where

[`scripts/personal_up.sh`](../scripts/personal_up.sh) starts it and [`scripts/personal_down.sh`](../scripts/personal_down.sh) stops it. Each is a transient `systemd-run --user` unit, so it outlives the shell that started it, but not a reboot: after one, run `personal_up.sh` again. The unit starts first, since the gateway checks the policy middleware when it starts; the launcher then waits for the unit, uses its worker and watches its health, and never stops it.

| Unit | What | Listens on |
| --- | --- | --- |
| `ss-personal-unit` | `sandbox-science-unit serve --config openshell/unit.toml`: one process that supervises three, started in this order and stopped in reverse; one log with a `[cache]`, `[middleware]` or `[worker]` prefix on each line | |
| | the package cache (the sandboxes reach it as `host.openshell.internal:8390`) | 127.0.0.1:8390 |
| | the policy middleware | 127.0.0.1:50061 |
| | the worker | 127.0.0.1:8310 |
| `ss-personal-gateway` | the OpenShell gateway, with the VM driver | 127.0.0.1:17680 |
| `ss-personal-launcher` | `sandbox-science up` and its parts: | |
| | the GUI | 127.0.0.1:8300 |
| | the PDF viewer's origin, given by `localhost` (another site from the GUI's) | 127.0.0.1:8301 and [::1]:8301 |
| | the control capability service, which the workspace's MCP availability check asks too | 127.0.0.1:8202 (the `[orchestrator]` default) |

The unit is healthy when all three of its addresses take connections (`.venv/bin/sandbox-science-unit status --config ~/.local/state/sandbox-science-personal/openshell/unit.toml`, and the unit's lines in `sandbox-science status`). If one of the three stops, the unit stops the other two and exits, and the launcher stops with "the unit is no longer healthy"; run `personal_down.sh` and `personal_up.sh` again.

The labs use `openshell-lab-*` units, `~/.local/state/openshell-lab`, and ports 17670, 50051, 8090, 8080, 8100, 8200, 8201 and 18080; none of these is the personal instance's.

| Path | What it holds |
| --- | --- |
| `~/.config/sandbox-science/personal.toml` | the launcher's config (mode 600; [below](#the-config)) |
| `~/.config/sandbox-science/personal-policy.yaml` | the project policy: `examples/policy.yaml` with crypto-research-mcp's address in place of its placeholder |
| `~/.local/state/sandbox-science-personal/launcher/` | the launcher's [state directory](QUICKSTART.md#the-state-directory): the control store (`control/`), tokens, the worker's state, `workspace-login`, and `logs/` |
| `~/.local/state/sandbox-science-personal/openshell/` | OpenShell's state: the gateway's database and certificates, the middleware's state and certificates, the package cache, `gateway.toml`, `targets.yaml`, the unit's `unit.toml` and `env.sh` |
| `~/.local/state/sandbox-science-personal/vm/` | the VM driver's state, the sandboxes' disks among it (short, since its sockets' paths must fit in `SUN_LEN`) |

The first `personal_up.sh` creates the OpenShell state: the gateway's certificates and keys, the middleware's own CA and certificate (ten years), the pool ceiling and the policy store. Later runs keep them, and rewrite only the settings files from the script (`unit.toml`, `gateway.toml`, `targets.yaml`). It also registers the provider `github-readonly`, which the example policy binds and OpenShell needs before it creates a research sandbox, with a placeholder value, as the labs do: public GitHub repositories work without it. To give the sandboxes a real fine-grained read-only token instead, `source ~/.local/state/sandbox-science-personal/openshell/env.sh` and run `openshell provider update github-readonly --credential GITHUB_TOKEN` with the token in that variable; only new research sandboxes get it.

The gateway's database holds the registered providers, crypto-research-mcp's Access token among them, with the key that encrypts them beside it. Never copy, archive or query them.

## The config

`~/.config/sandbox-science/personal.toml` holds no secret, only the paths of the model key and of the owner's Cloudflare Access file. It differs from [`examples/sandbox-science.toml`](../examples/sandbox-science.toml) in these settings:

```toml
state_dir = "~/.local/state/sandbox-science-personal/launcher"

[[projects]]
id = "crypto"
profile = "crypto-base"
sessions = ["main"]
skills = ["crypto"]

[model]
url = "https://gateway.example.invalid/v1"
name = "arrcodex-gpt-6-sol"          # every role: the main agent, the reviewer, the watcher, delegated runs
reviewer = "arrcodex-gpt-6-sol"
watcher = "arrcodex-gpt-6-sol"
delegated = "arrcodex-gpt-6-sol"
api = { "arrcodex-gpt-6-sol" = "responses" }
api_key_file = "~/.config/sandbox-science/secrets/litellm-harness.key"
# no daily_cost_limit: the gateway's own budget is the cap

[run_service]
math_checks = "<the repository>/web/workspace/dist-math"
node = "/usr/bin/node"               # Node 22.12 or newer on the units' PATH

[worker]
listen = "127.0.0.1:8310"
pool_id = "babbage-personal"
policy = "personal-policy.yaml"
unit = "~/.local/state/sandbox-science-personal/openshell/unit.toml"   # the worker runs in the unit, started by personal_up.sh

[workspace]
listen = "127.0.0.1:8300"
# The viewer: the next port, given by localhost, another site (no preview_listen needed).
static = "<the repository>/web/workspace/dist"

[skills]
embedding_model = "arr-text-embedding-3-small"
metadata_model = "arrcodex-gpt-6-sol"

[skills.packs.crypto]
git = "https://github.com/regenscheid/crypto-analysis-skills"
ref = "main"

[openshell]                          # cli, python and isolation_tier as the example
package_cache = "http://host.openshell.internal:8390"
# [openshell.env] and [openshell.environments]: the example's, with
# ~/.local/state/sandbox-science-personal/openshell in place of ~/.local/state/openshell-lab

[openshell.environments.profiles]
minimal = "sandbox-science/minimal:<tag>"
crypto-base = "sandbox-science/crypto-base:<tag>"

[mcp_direct]                         # the rest as the example's commented [mcp_direct]
providers = ["crypto-research-mcp"]
provider_env_file = "<the owner's Cloudflare Access file>"
provider_profile = "<the repository>/examples/openshell/profiles/cloudflare-access.yaml"

[mcp_direct.servers.crypto-research-mcp]
credentials = { "CF-Access-Client-Id" = "CF_ACCESS_CLIENT_ID", "CF-Access-Client-Secret" = "CF_ACCESS_CLIENT_SECRET" }
```

- **The context ceiling** defaults to 600,000 tokens ([context management](specs/context-management.md), CM-27). `[run_service] context_ceiling` sets a positive token ceiling, capped by the gateway's model input limit when reported. The folds' narratives are written by the main agent's own alias. This is context occupancy, not a cumulative research token budget.
- **Delegation** is a project setting in the control store, set once after the first start (depth 2 lets a task delegate once more):

  ```sh
  .venv/bin/sandbox-science-harness project delegation \
    --state-dir ~/.local/state/sandbox-science-personal/launcher/control --project crypto --depth 2
  ```

- **Long runs.** A main run makes up to 2,000 model calls by default, and a delegated task up to 300 calls and 5,000,000 tokens; a delegated task at its limit checks in with its parent, a main run at its turn limit pauses and asks you in the chat to continue for another block of as many or to stop, and a runaway guard stops a run that repeats itself ([run service](specs/run-service.md), RS-6f). `[run_service] max_turns` sets the main runs' limit; `project delegation --task-turns N --task-tokens N` the tasks'. A setting written before these defaults rose reads its old task defaults (20 turns, 200,000 tokens) as the new ones.
- **The policy file** is written from the example by `.agents/labs/launcher_config.py`'s `mcp_policy`, as the labs' is.

## Starting, stopping, status

From the repository's root:

```sh
scripts/personal_up.sh        # the unit, then the gateway, then the launcher; waits until it runs
scripts/personal_down.sh      # sandbox-science down, then the unit and the gateway; deletes nothing
.venv/bin/sandbox-science status --config ~/.config/sandbox-science/personal.toml
```

- **Logging in.** The workspace makes a new login address at each start, in the state directory's `workspace-login` (mode 600), never on the screen or in a log: `xdg-open "$(cat ~/.local/state/sandbox-science-personal/launcher/workspace-login)"`. It works until the instance stops. Use the GUI at `http://127.0.0.1:8300/`, not `localhost`, so the PDF viewer works and stays another site (`http://localhost:8301`). A config that still sets `preview_listen = "127.0.0.2:8301"` works too, as another site; drop it to use the default.
- **Stopping** suspends the research sandboxes, which keep their files and packages, and deletes the agent runtime's sandbox, which the next start makes again. `personal_down.sh --keep-sandboxes` leaves the sandboxes, the unit and OpenShell running, so their kernels are there for the next `personal_up.sh`; it refuses to reuse a kept unit if the launcher's config now gives the worker other settings (stop everything, then start).
- **A snapshot before a migration.** Before it starts anything, `personal_up.sh` asks `personal_backup.sh --versions` whether the checkout would migrate a store. If so it takes a snapshot first (`--label before-migration`) and starts nothing if that fails; it also starts nothing when a store is newer than the checkout. `personal_up.sh --skip-snapshot`, or `SS_PERSONAL_SKIP_SNAPSHOT=1`, skips the check and the snapshot. See [before an update](BACKUP-RESTORE.md#before-an-update).
- **Only OpenShell**: `personal_up.sh --openshell-only` starts the unit and the gateway without the launcher. It still reads `personal.toml`, since the unit's worker takes its settings from it.
- **The OpenShell CLI** for this instance: `source ~/.local/state/sandbox-science-personal/openshell/env.sh`, then `openshell sandbox list`. It points the XDG directories at the instance, so `gh` does not work in that shell.
- **Logs.** The parts' own logs are in `~/.local/state/sandbox-science-personal/launcher/logs/` (`worker.log`, `control-service.log`, `workspace.log`, `launcher.log`, …); the launcher's output is in `journalctl --user -u ss-personal-launcher`, the gateway's in `-u ss-personal-gateway`, and the unit's, one stream for the cache, the middleware and the worker with each line prefixed by its service, in `-u ss-personal-unit`. Set `SS_LOG_FORMAT=json` in the launcher's or the unit's environment for JSON lines (the Kubernetes deployment's format); the default is the text line.

The scripts take their settings from the environment: `SS_PERSONAL_ROOT`, `SS_PERSONAL_CONFIG`, `SS_PERSONAL_UNIT`, `SS_PERSONAL_SKIP_SNAPSHOT` (`personal_up.sh` only), the ports, the VMs' size (`VM_VCPUS`, `VM_MEM_MIB`, `VM_DISK_MIB`; 8, 16,384 and 32,768 by default) and `R`, the repository. The defaults are the instance above. They refuse a unit prefix or a root that is the labs'. `personal_up.sh` also refuses to start while `ss-personal-middleware` or `ss-personal-cache` of the earlier layout runs; `personal_down.sh` stops them too.

**Moving from the separate units.** Add the `unit = ...` line above to `[worker]` in `personal.toml`, run `scripts/personal_down.sh` (it stops the earlier `-middleware` and `-cache` units as well), then `scripts/personal_up.sh`. The `middleware.toml` and `cache.toml` it used to write are no longer read; `unit.toml` holds their settings. Nothing else changes: the ports, the state and the sandboxes are the same.

## Updating to a new main

The instance runs from the repository's checkout of `main`, its `.venv` and its web build. Keep that checkout on `main` while the instance runs, and do other work in worktrees.

1. Stop it: `scripts/personal_down.sh`.
2. Update the checkout: `git pull --ff-only`, then `uv sync --locked`, and in `web/workspace`, `npm ci --ignore-scripts && npm run build && npm run build:math`.
3. Rebuild the images whose inputs changed ([images](IMAGES.md)): `images/build.sh minimal`, `crypto-base`, `orchestrator`, then `images/test.sh` for each. Start nothing on an image whose checks fail. Only create or tag `sandbox-science/*` images; the Docker store is never pruned.
4. Put the new tags in `personal.toml`: `[openshell.environments.profiles]` and `[orchestrator] image` and `revision`. A project keeps the sandbox it has: a new profile image reaches new environments only.
   - **Once, on the update that retires the MCP proxy (2026-10-26):** `personal.toml` still has `[mcp_proxy]`, which the launcher now refuses (and `personal_up.sh` stops before starting it, saying so). Rewrite it with `.venv/bin/sandbox-science migrate-mcp-config ~/.config/sandbox-science/personal.toml` (`--print` shows the result first; the original stays as `personal.toml.mcp-proxy.bak`), check that `personal-policy.yaml` sets neither `mcp.proxy.worker_access: true` nor a `worker_tools` list (now refused), ([MCP connectors](MCP-CONNECTORS.md#migrating-from-mcp_proxy)); `personal_up.sh` now writes the unit's middleware settings with `mcp_listings = true`, which the direct path needs. The proxy's sandbox (`ss-pers-mcp-*`) is deleted by `personal_down.sh` before the update, or by the next start.
5. Start it: `scripts/personal_up.sh`. Skill packs are fetched only when asked: `.venv/bin/sandbox-science skills update --config ~/.config/sandbox-science/personal.toml`.

If the protocol between the parts changed, the worker, run service and workspace from the new checkout must all restart together, which steps 1 and 5 do.

## The labs leave it alone

- `lab_up.sh` and `lab_down.sh` name only `~/.local/state/openshell-lab`, `openshell-lab-*` units and the lab's ports. `lab_down.sh` deletes the lab's sandboxes through the lab's gateway alone, with the lab's `env.sh`.
- `lab_down.sh`'s last checks leave out processes in `ss-personal-*` units and the VM driver's socket directory under `/tmp` that they hold, so a running personal instance does not read as a lab left behind. Its check for OpenShell paths looks for `~/.local/state/openshell*`, which the personal paths are not.
- The two gateways are separate, but share the machine, the Docker store and `/tmp`. Running a lab while the personal instance runs has not been exercised; stopping the personal instance first is the safe order.

## Backup and restore

`scripts/personal_backup.sh` snapshots the instance's SQLite stores, artifact and worker blobs, event and policy stores and the two config files into a dated, verified directory (never the OpenShell gateway's database or any key), and `--restore` puts one back. Take one with the instance stopped, between step 1 and step 2 of [updating](#updating-to-a-new-main); `personal_up.sh` takes one itself when the checkout would migrate a store. The [backup and restore runbook](BACKUP-RESTORE.md) has the details.
