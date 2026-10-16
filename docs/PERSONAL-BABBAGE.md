# The personal instance on babbage

The owner's own Sandbox Science on babbage: a [`sandbox-science up`](QUICKSTART.md) instance that runs for research, apart from the labs. It has its own OpenShell gateway, policy middleware and package cache, its own units, ports and state, and its own config outside the repository. The labs' scripts (`.agents/labs/lab_up.sh`, `lab_down.sh`, the `cm_lab` kit) never start, stop or remove any of it.

Like the labs' scripts, this is babbage's setup: it assumes OpenShell 0.1.3 at `~/.local/opt/openshell-0.1.3/bin`, the images in the local Docker store, and the repository's `.venv`.

## What runs where

[`scripts/personal_up.sh`](../scripts/personal_up.sh) starts it and [`scripts/personal_down.sh`](../scripts/personal_down.sh) stops it. Each part is a transient `systemd-run --user` unit, so it outlives the shell that started it, but not a reboot: after one, run `personal_up.sh` again.

| Unit | What | Listens on |
| --- | --- | --- |
| `ss-personal-gateway` | the OpenShell gateway, with the VM driver | 127.0.0.1:17680 |
| `ss-personal-middleware` | the policy middleware | 127.0.0.1:50061 |
| `ss-personal-cache` | the package cache (the sandboxes reach it as `host.openshell.internal:8390`) | 127.0.0.1:8390 |
| `ss-personal-launcher` | `sandbox-science up` and its parts: | |
| | the GUI | 127.0.0.1:8300 |
| | the PDF viewer's origin, given by `localhost` (another site from the GUI's) | 127.0.0.1:8301 and [::1]:8301 |
| | the worker | 127.0.0.1:8310 |
| | the forward to the MCP proxy's sandbox | 127.0.0.1:18300 |

The labs use `openshell-lab-*` units, `~/.local/state/openshell-lab`, and ports 17670, 50051, 8090, 8080, 8100, 8200, 8201 and 18080; none of these is the personal instance's.

| Path | What it holds |
| --- | --- |
| `~/.config/sandbox-science/personal.toml` | the launcher's config (mode 600; [below](#the-config)) |
| `~/.config/sandbox-science/personal-policy.yaml` | the project policy: `examples/policy.yaml` with crypto-research-mcp's address in place of its placeholder |
| `~/.local/state/sandbox-science-personal/launcher/` | the launcher's [state directory](QUICKSTART.md#the-state-directory): the control store (`control/`), tokens, the worker's state, `workspace-login`, and `logs/` |
| `~/.local/state/sandbox-science-personal/openshell/` | OpenShell's state: the gateway's database and certificates, the middleware's state and certificates, the package cache, `gateway.toml`, `targets.yaml`, and `env.sh` |
| `~/.local/state/sandbox-science-personal/vm/` | the VM driver's state, the sandboxes' disks among it (short, since its sockets' paths must fit in `SUN_LEN`) |

The first `personal_up.sh` creates the OpenShell state: the gateway's certificates and keys, the middleware's own CA and certificate (ten years), the pool ceiling and the policy store. Later runs keep them, and rewrite only the settings files from the script. It also registers the provider `github-readonly`, which the example policy binds and OpenShell needs before it creates a research sandbox, with a placeholder value, as the labs do: public GitHub repositories work without it. To give the sandboxes a real fine-grained read-only token instead, `source ~/.local/state/sandbox-science-personal/openshell/env.sh` and run `openshell provider update github-readonly --credential GITHUB_TOKEN` with the token in that variable; only new research sandboxes get it.

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
url = "https://litellm-api.npages.org/v1"
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

[mcp_proxy]                          # the rest as the example's commented [mcp_proxy]
image = "sandbox-science/mcp-proxy:<tag>"
sandbox = "ss-pers-mcp"
listen = "127.0.0.1:18300"
provider_env_file = "<the owner's Cloudflare Access file>"
provider_profile = "<the repository>/examples/openshell/profiles/cloudflare-access.yaml"
```

- **The context ceiling** is the run service's default, 600,000 tokens ([context management](specs/context-management.md), CM-27): the launcher has no setting for it, and the model's input limit, which the gateway gives, is larger. The folds' narratives are written by the main agent's own alias.
- **Delegation** is a project setting in the control store, set once after the first start (depth 2 lets a task delegate once more):

  ```sh
  .venv/bin/sandbox-science-harness project delegation \
    --state-dir ~/.local/state/sandbox-science-personal/launcher/control --project crypto --depth 2
  ```

- **Long runs.** A main run makes up to 2,000 model calls by default, and a delegated task up to 300 calls and 5,000,000 tokens; at a limit, the run's last call reports what it found, and a runaway guard stops a run that repeats itself ([run service](specs/run-service.md), RS-6f). `[run_service] max_turns` sets the main runs' limit; `project delegation --task-turns N --task-tokens N` the tasks'. A setting written before these defaults rose reads its old task defaults (20 turns, 200,000 tokens) as the new ones.
- **The policy file** is written from the example by `.agents/labs/launcher_config.py`'s `mcp_policy`, as the labs' is.

## Starting, stopping, status

From the repository's root:

```sh
scripts/personal_up.sh        # OpenShell's three units, then the launcher; waits until it runs
scripts/personal_down.sh      # sandbox-science down, then OpenShell's units; deletes nothing
.venv/bin/sandbox-science status --config ~/.config/sandbox-science/personal.toml
```

- **Logging in.** The workspace makes a new login address at each start, in the state directory's `workspace-login` (mode 600), never on the screen or in a log: `xdg-open "$(cat ~/.local/state/sandbox-science-personal/launcher/workspace-login)"`. It works until the instance stops. Use the GUI at `http://127.0.0.1:8300/`, not `localhost`, so the PDF viewer works and stays another site (`http://localhost:8301`). A config that still sets `preview_listen = "127.0.0.2:8301"` works too, as another site; drop it to use the default.
- **Stopping** suspends the research sandboxes, which keep their files and packages, and deletes the MCP proxy's sandbox, which the next start makes again. `personal_down.sh --keep-sandboxes` leaves the sandboxes and OpenShell running, so their kernels are there for the next `personal_up.sh`.
- **Only OpenShell**: `personal_up.sh --openshell-only` starts the three OpenShell units without the launcher.
- **The OpenShell CLI** for this instance: `source ~/.local/state/sandbox-science-personal/openshell/env.sh`, then `openshell sandbox list`. It points the XDG directories at the instance, so `gh` does not work in that shell.
- **Logs.** The parts' own logs are in `~/.local/state/sandbox-science-personal/launcher/logs/` (`worker.log`, `run-service.log`, `workspace.log`, `mcp-forward.log`, `launcher.log`); the units' output is in `journalctl --user -u ss-personal-launcher` (and `-gateway`, `-middleware`, `-cache`).

The scripts take their settings from the environment: `SS_PERSONAL_ROOT`, `SS_PERSONAL_CONFIG`, `SS_PERSONAL_UNIT`, the ports, the VMs' size (`VM_VCPUS`, `VM_MEM_MIB`, `VM_DISK_MIB`; 8, 16,384 and 32,768 by default) and `R`, the repository. The defaults are the instance above. They refuse a unit prefix or a root that is the labs'.

## Updating to a new main

The instance runs from the repository's checkout of `main`, its `.venv` and its web build. Keep that checkout on `main` while the instance runs, and do other work in worktrees.

1. Stop it: `scripts/personal_down.sh`.
2. Update the checkout: `git pull --ff-only`, then `uv sync --locked`, and in `web/workspace`, `npm ci --ignore-scripts && npm run build && npm run build:math`.
3. Rebuild the images whose inputs changed ([images](IMAGES.md)): `images/build.sh minimal`, `crypto-base`, `mcp-proxy`, then `images/test.sh` for each. Start nothing on an image whose checks fail. Only create or tag `sandbox-science/*` images; the Docker store is never pruned.
4. Put the new tags in `personal.toml`: `[openshell.environments.profiles]` and `[mcp_proxy] image`. A project keeps the sandbox it has: a new profile image reaches new environments only.
5. Start it: `scripts/personal_up.sh`. Skill packs are fetched only when asked: `.venv/bin/sandbox-science skills update --config ~/.config/sandbox-science/personal.toml`.

If the protocol between the parts changed, the worker, run service and workspace from the new checkout must all restart together, which steps 1 and 5 do.

## The labs leave it alone

- `lab_up.sh` and `lab_down.sh` name only `~/.local/state/openshell-lab`, `openshell-lab-*` units and the lab's ports. `lab_down.sh` deletes the lab's sandboxes through the lab's gateway alone, with the lab's `env.sh`.
- `lab_down.sh`'s last checks leave out processes in `ss-personal-*` units and the VM driver's socket directory under `/tmp` that they hold, so a running personal instance does not read as a lab left behind. Its check for OpenShell paths looks for `~/.local/state/openshell*`, which the personal paths are not.
- The two gateways are separate, but share the machine, the Docker store and `/tmp`. Running a lab while the personal instance runs has not been exercised; stopping the personal instance first is the safe order.
