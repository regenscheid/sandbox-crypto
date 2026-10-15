# Quickstart: Sandbox Science on one machine

`sandbox-science up` runs a local Sandbox Science from one config file:
- the [worker](WORKER.md), which runs research code in OpenShell sandboxes;
- the [run service](WORKSPACE.md#chat), which runs the main agent when you chat;
- the [workspace](WORKSPACE.md), the GUI, with its PDF viewer's origin;
- optionally, the [MCP proxy](#the-mcp-proxy) in an OpenShell sandbox of its own.

Everything listens on loopback, so only your own computer can reach it. The steps below are for babbage.

## What you need

- **OpenShell**, with the policy middleware and the package cache. On babbage, `.agents/labs/lab_up.sh` brings up all three under `~/.local/state/openshell-lab`, and `lab_down.sh` removes them.
- **The research images**, built: `images/build.sh minimal`, and `images/build.sh crypto-base` if you want it ([images](IMAGES.md)). Each build prints the image's tag, for the config.
- **The repository's Python environment**: `uv sync` in the checkout. It installs the `sandbox-science` command with the parts' own commands.
- **The web app and the math checker**, built: `cd web/workspace && npm ci --ignore-scripts && npm run build && npm run build:math`. The math checks run on Node 22.12 or newer.
- **A model gateway and its key**: an OpenAI-compatible gateway, such as LiteLLM, and a file holding its key that only you can read (mode 600; the launcher refuses one others can read). The launcher only passes the file's path to the run service: it never reads the key, and never prints it.

## The config

[`examples/sandbox-science.toml`](../examples/sandbox-science.toml) is a config for babbage's lab. Relative paths in it are against the file, and `~` is your home directory.

| Section | What it says |
| --- | --- |
| `state_dir` | Where the launcher keeps what it makes (see [the state directory](#the-state-directory)); `~/.local/state/sandbox-science` by default |
| `[[projects]]` | Each project: its `id`, the `profile` its environments start from, and the `sessions` to open for chat (`["main"]` by default). A session is a name, in the project's default environment, or `{ id = "scratch", dedicated = true }`, in an environment of its own. A session keeps the mode it was first opened in, so changing `dedicated` for one that exists makes `up` fail at that session |
| `[model]` | The gateway's `url`, the main agent's model `name`, and `api_key_file`. Optional:<br>- `reviewer`, the model for reviews and the workspace's math repairs, with `review_token_budget` and `review_calls_per_hour`, a review's token budget and the reviewer's hourly call cap ([consistency review](CONSISTENCY-REVIEW.md));<br>- `watcher`, the model for the job watcher's check-ins on long jobs (none by default; it may be the main agent's), with `watcher_calls_per_hour`, `watcher_first_check` and `watcher_interval` (by default 60 calls an hour, 300 s and 1,800 s);<br>- `delegated`, the model delegated runs call (the main agent's by default).<br>- `daily_cost_limit`, what each project's runs may cost over a rolling 24 hours, as the gateway reports it, in its currency (normally US dollars). Unset, as by default, no project has a limit of its own: the gateway's own budget is the cap ([run service](specs/run-service.md), RS-6a).<br>Each limit left out keeps the run service's default |
| `[worker]` | Its `listen` address, its `pool_id`, and the project `policy` granted to every project |
| `[run_service]` | Optional:<br>- `run_share`, the percent of a project's daily cost limit one run may use, when the project has a limit (none by default): a run stops at its next safe point once it has used it, or once the project reaches its limit ([run service](specs/run-service.md), RS-6c);<br>- `math_checks`, the math checker's build (`web/workspace/dist-math`), and `node` (`node` on `PATH` by default). Without `math_checks`, every formula in a document shows "renderer unavailable";<br>- its `pass_env` |
| `[workspace]` | Its `listen` address, its `preview_listen` address for the PDF viewer (by default the next port), and `static`, the web app's build |
| `[openshell]` | The worker's own `[openshell]` table ([worker settings](WORKER.md)): the OpenShell CLI and the `env` it needs to reach the gateway, the sandboxes' Python, the isolation tier, the package cache, and `[openshell.environments]` with the profiles' image tags |
| `[mcp_proxy]` | Optional: the [MCP proxy](#the-mcp-proxy) |

`start_timeout` (300 s) and `stop_timeout` (30 s) bound how long each part may take to start and to stop. `bin_dir` says where the parts' commands are, by default beside the launcher's Python.

The parts get only a few of your environment's variables: `PATH`, `HOME`, `USER`, `LOGNAME`, the locale (`LANG`, `LANGUAGE`, `LC_*`), `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`, the proxies and the CA settings (`SSL_CERT_FILE`, `SSL_CERT_DIR`). An API key you exported in your shell reaches no part.

Each part's table can name more with `pass_env`: `[worker]`, `[run_service]` (a table of its own, with only this) and `[workspace]`. The part gets those variables from your environment, and the other parts do not. The launcher's own OpenShell commands, and the MCP proxy's forward, get the worker's, with `[openshell] env`. **Never pass a secret this way.** The run service and the workspace run in the control zone, which must hold no engine or cluster credentials ([threat model](THREAT-MODEL.md)). The model's key reaches the run service only as its file, `api_key_file`.

The state directory must be yours and mode 700. If one is there already and open to other users, the launcher refuses it, rather than change it.

Before you start, set the `profiles` to the tags `images/build.sh` printed. Set `api_key_file` to your key's file.

## The first run

```bash
uv run sandbox-science up --config examples/sandbox-science.toml
```

The launcher does these steps in order. It stops at the first that fails and says why: a missing file, the web app not built, or a part that did not start, with the end of its log.

1. It checks that every file and command in the config is there.
2. It makes the state directory and the tokens the parts need, and writes the worker's settings.
3. It starts the worker, and waits for its address to take connections.
4. It grants the project policy to every project.
5. It makes each project in the control store, or moves an existing one to the policy, and opens its sessions, the dedicated ones in environments of their own. A project keeps the profile it was made with: a config that names another stops `up` here.
6. It starts the run service, and then the workspace.

When everything is up, it prints:

```
Sandbox Science is running.
  GUI:      http://127.0.0.1:8200/
  Log in:   open the address in /home/you/.local/state/sandbox-science/workspace-login, for example with
            xdg-open "$(cat /home/you/.local/state/sandbox-science/workspace-login)"
            It works until Sandbox Science stops.
  Projects: crypto (sessions main)
  Logs:     /home/you/.local/state/sandbox-science/logs
  Stop:     Ctrl-C, or sandbox-science down --config /…/examples/sandbox-science.toml
```

## Logging in

The workspace makes a new login address each time it starts. The launcher puts it in `workspace-login` in the state directory (mode 600), never on the screen or in a log. Open that address in your browser. It takes you to the GUI and sets your session's cookie, and it works until Sandbox Science stops.

Open the GUI at the address the launcher printed. With `127.0.0.1`, the PDF viewer works; under `localhost`, the browser refuses to frame it ([the PDF viewer's origin](WORKSPACE.md#the-pdf-viewers-origin)).

## A first chat

1. Open the project, here `crypto`, and its session `main`.
2. The session page has a chat panel. It says whether the run service is running.
3. Write a message, for example: "Which Python does the sandbox have? Print it with a shell command."

The message starts a run of the main agent. The first run of a project has the worker create its environment, an OpenShell sandbox from the project's profile, which takes a minute or two; the panel says "Starting the environment…" meanwhile. It then shows the agent's turns and tool calls as they come, then its answer. [Chat](WORKSPACE.md#chat) has the details: budgets, cancelling and resuming.

## Delegation and a cost limit

Each is a project setting, made with the harness against the launcher's control store, the `control/` directory of the state directory ([the state directory](#the-state-directory)):

```sh
D=~/.local/state/sandbox-science/control
sandbox-science-harness project delegation --state-dir "$D" --project crypto --depth 1
sandbox-science-harness project budget --state-dir "$D" --project crypto --daily-cost-limit 5
```

- **Delegation** is off by default. `--depth 1` lets the main agent delegate checks and tasks, and `--depth 2` lets a task delegate once more. The same command sets the cap on runs at once and the children's limits ([projects and sessions](PROJECTS-AND-SESSIONS.md)). `[model] delegated` names the model they call.
- **The cost limit** is off by default: a project has no limit of its own, and the gateway's budget is the cap. `--daily-cost-limit` sets what the project's runs may cost over 24 hours, as the gateway reports it, and `--no-cost-limit` removes it. `[model] daily_cost_limit` sets it for every project from the config.

## Skill packs

Every project has the platform's built-in skills. Other packs are named in the config, and in each project that uses them ([skills](SKILLS.md)):

```toml
[[projects]]
id = "crypto"
profile = "minimal"
skills = ["crypto"]

[skills]
embedding_model = "sandbox-embedding"   # optional: the gateway's embedding alias

[skills.packs.crypto]
git = "https://github.com/regenscheid/crypto-analysis-skills"   # or archive = "pack.tar.gz"
ref = "main"                                                     # optional: the default branch
# path = "skills"                                                # optional: the skills' directory
```

- **`up`** imports a pack the control store has none of, on this computer with your git, and pins it in each project that names it. After that it fetches nothing and keeps the pins, so the skills change only when you ask. A local repository's path is against the config file. `[skills] pass_env` gives git variables such as `SSH_AUTH_SOCK` for an ssh URL.
- **`up --update-skills`**, or **`sandbox-science skills update [--pack NAME]`** with or without an instance running, fetches the packs again from their refs and moves the projects' pins. It prints, for each project, the skills added, removed and changed since the pin it left. Runs that started before keep the skills they have.
- **`embedding_model`** has the import embed the skills, and the run service compare them with what the agent searches for and the user asks by meaning as well as words. The launcher gives the import and the run service the model key file's path, and never opens it. When embedding fails at import, `up` says so and goes on: the run service makes the vectors in the background, and runs compare by words until they exist.
- Taking a pack out of a project's `skills` does not unpin it: exclude its skills with `sandbox-science-harness skills settings --project P --expected N --exclude SOURCE/NAME`, against `control/` as above, where `N` is the revision `skills settings --project P` shows.

## The MCP proxy

With `[mcp_proxy]` in the config, `up` also runs the [MCP proxy](MCP-CONNECTORS.md#the-mcp-proxy-as-built) in an OpenShell sandbox of its own. The main agent of one project then gets the tools of the MCP servers named:

```toml
[mcp_proxy]
image = "sandbox-science/mcp-proxy:d27e41a"        # as images/build.sh mcp-proxy tagged it
providers = ["crypto-research-mcp"]                # OpenShell providers with the servers' credentials
servers = ["crypto-research-mcp"]                  # servers in the policy whose tools the agent gets
credentials = { "crypto-research-mcp.CF_ACCESS_CLIENT_ID" = "CF_ACCESS_CLIENT_ID", "crypto-research-mcp.CF_ACCESS_CLIENT_SECRET" = "CF_ACCESS_CLIENT_SECRET" }
# project = "crypto"                               # by default the only project
# policy, targets, ceiling                         # by default the worker's policy, targets and pool ceiling
# sandbox = "ss-mcp-proxy"                         # its name's start, at most 12 characters
# listen = "127.0.0.1:18080"                       # where the run service reaches it
# provider_env_file = "/path/to/the/owners/cloudflare.env"                # registers crypto-research-mcp from it
# provider_profile = "examples/openshell/profiles/cloudflare-access.yaml"    # needed with provider_env_file
# index = ["crypto-research-mcp/e-print-mcp"]      # servers the tool index lists tool by tool; by default all
# about = { "crypto-research-mcp/e-print-mcp" = "the IACR Cryptology ePrint Archive: search its papers, records, full text and PDFs" }
```

- **The providers** hold the servers' credentials. You create them yourself ([what the owner does](MCP-CONNECTORS.md#crypto-research-mcp-a-cloudflare-access-service-token)), or `up` registers `crypto-research-mcp` from `provider_env_file`. The launcher never sees the credentials. If a provider is still missing, `up` refuses before it starts anything.
- **`provider_env_file`** names the owner's file with crypto-research-mcp's Access service token. This is babbage's default for now (the owner, 2026-10-15). Only a config outside the repository names the real file, such as a lab's ([labs](../.agents/labs/README.md)): whoever writes the config also chooses the OpenShell CLI that gets the values.
  - When the gateway has no such provider, as a new lab gateway doesn't, `up` registers it from the file before it starts anything.
  - The registration runs in a process of its own (`sandbox_science.launcher.access_provider`), with the parts' usual environment and `[openshell] env` alone. The launcher never opens the file, and the values reach only the OpenShell command that registers them, and OpenShell's own encrypted store ([from the owner's file](MCP-CONNECTORS.md#crypto-research-mcp-a-cloudflare-access-service-token)).
  - `up --refresh-provider` registers it again even if it exists, as after a rotation.
  - It needs `provider_profile`, the provider's OpenShell profile, which `up` imports when the gateway has none.
- **`about` and `index`** shape the index of the servers' tools that the main agent gets instead of every tool's schema at every call ([an index, and tools loaded on demand](MCP-CONNECTORS.md#the-harnesss-client)). `about` says what each server is for, at most 200 characters and not blank, so the agent picks the right one; the index marks it as the operator's. A key is one of `servers`, or `SERVER/BEHIND` for a server behind a portal, the one a tool's name starts with, such as `crypto-research-mcp/e-print-mcp`, so one server's words never reach another's tools. `index` names the servers whose tools are listed one by one, at least one when given; the agent finds the others' with a query. The example config says both for crypto-research-mcp.
- **`credentials`** is the proxy's `[credentials]` table: which environment variable holds each provider credential that a server's headers name ([running it](MCP-CONNECTORS.md#running-it)).
- **What `up` does** is the [steps in running it](MCP-CONNECTORS.md#running-it):
  1. It compiles the sandbox's policy.
  2. It creates the sandbox with the providers.
  3. It uploads the proxy's files. The proxy's one caller is the run service, by its token's digest.
  4. It binds the project to its policy.
  5. It forwards `listen` to the proxy. The forward starts again whenever it exits, as it does when the sandbox stops: after 2 s, then twice as long each time it keeps stopping, up to a minute. After ten times in a row, or if something else has taken `listen` meanwhile, the launcher gives up and stops everything, so the run service never sends its token to anyone else.
- **The targets file** needs an `mcp_proxy` section, as babbage's lab targets have.
- **Its name** is `sandbox` and six random hex digits, new at each `up`, such as `ss-mcp-proxy-3fa91c`, so it is never another launcher's, or one made by hand.
- **On stopping**, the launcher deletes the sandbox, after stopping the run service and before the worker. It deletes only the sandbox it made, by the name it recorded before making it and the ID it recorded once it was ready. If the launcher was killed, `down` or the next `up` deletes the sandbox it left.

## Stopping

Press Ctrl-C where `up` runs, or run `sandbox-science down --config …` from another terminal. Closing the terminal or the SSH session that runs `up` (SIGHUP) stops it the same way, as do SIGTERM and SIGQUIT. The launcher stops the parts in reverse order: the workspace, the run service, the MCP proxy's forward and sandbox if there are any, then the research sandboxes, then the worker.
- It gives each part `stop_timeout` to stop after SIGTERM, then kills what is left of the part's session. A second Ctrl-C stops them at once.
- A part that stops by itself stops the others too, and `up` exits 1 with the end of that part's log.

**The research sandboxes:** these are the sandboxes the worker made for the config's projects. A stop you ask for (Ctrl-C, `down`, SIGTERM, SIGHUP or SIGQUIT) suspends them before the worker stops, through the worker's own suspension, the one `suspend_after` uses ([idle suspension](WORKER.md#idle-suspension)).
- **What a suspension keeps.** A suspended sandbox keeps its files and installed packages; its kernels end. The next use, after the next `up`, starts it again.
- **What it never touches.** Another project's sandbox, or one the worker did not make, is never suspended. The launcher asks the worker with a token limited to the config's projects.
- **A sandbox in use keeps running.** One running a background job, or with a kernel still computing (its CPU sampled twice, a second apart), is not suspended. `down` names each sandbox that may still be running and exits 1, and the services are stopped all the same.
  - The same happens when a suspension fails or takes more than three minutes.
  - A second Ctrl-C stops waiting for the suspension: the launcher says the sandboxes were not checked, and `down` later finishes the job.
  - Each sandbox the worker lists is asked, even one it records as suspended, and the answer is OpenShell's phase. A stop that failed earlier is therefore tried again.
- **Keeping them running.** `down --keep-sandboxes` stops the services and leaves the sandboxes running, as they were. Their kernels are still there for the next `up`, which reattaches to them ([sandbox daemon and recovery](WORKER.md#sandbox-daemon-and-recovery)).
- **After a part died.** When a part stopped by itself and took the rest down, the sandboxes are not suspended. The launcher says which ones still run, and `sandbox-science down` suspends them.
- **`down` with the services stopped.** `down` starts the worker alone to suspend the sandboxes, and stops it again. It does this when OpenShell says one still runs, or, after a launcher that was killed, always, since the last list may be stale.
  - While it runs, `status` says so and exits 4, and another `down` waits for it.
  - A stop signal makes it stop waiting, stop the worker and say what may still run.
- With `suspend_after` in `[openshell.environments]`, as in the example, the worker also suspends a sandbox nobody has used for that long, while Sandbox Science runs.
- To delete the sandboxes, use the OpenShell CLI (`openshell sandbox list`, `openshell sandbox delete`).

## Status

```bash
uv run sandbox-science status --config examples/sandbox-science.toml
```

It shows each part's process, whether its address answers, and each research sandbox, running or suspended:
- While the services run, the worker lists the sandboxes.
- When they are stopped, the launcher asks OpenShell about the ones the worker last listed. It keeps that list, refreshed every half minute while Sandbox Science runs. If OpenShell cannot answer, `status` says the sandboxes could not be checked, and names them.

`status` exits 0 while Sandbox Science runs as it should, 3 when it does not run, and 4 when something is wrong:
- a part stopped or silent;
- one left running by a launcher that was killed;
- research sandboxes running, or not checked, with the services stopped.

The parts die with the launcher, even when it is killed outright. One that ignores the SIGTERM it gets then keeps running: `status` lists it, and `down`, or the next `up`, stops it. The launcher knows a part from the record it keeps in the state directory, by its process ID, its start time and the boot it started in, so after a reboot, or with another state directory's parts, it stops nothing of anyone else's. An `up` on the same state directory while one runs is refused.

## The state directory

Everything in it is only for your user: the directories are mode 700 and the files mode 600.

| Path | What it holds |
| --- | --- |
| `launcher.lock`, `launcher.json` | The lock `up` holds while it runs, and the record of the parts it started |
| `workspace-login` | The workspace's login address, while it runs |
| `tokens/` | The worker's own token and the run service's and the workspace's tokens for it, limited to the config's projects. They are made once and kept. Delete the directory, with Sandbox Science stopped, to have new ones made |
| `worker/worker.toml`, `worker/state/` | The worker's settings, written at each `up`, and its state directory. Do not delete the state directory while its sandboxes exist: it holds their endpoints |
| `control/` | The control store, with the projects, sessions, runs and documents, that the run service and the workspace share |
| `sandboxes.json`, `stopped.json` | The research sandboxes as last listed, and what the last stop did to them, for `down` to say |
| `logs/` | Each part's output (`worker.log`, `run-service.log`, `workspace.log`, `mcp-forward.log`), and `launcher.log` with the output of the commands `up` runs once |
| `mcp-proxy/` | With `[mcp_proxy]`: the proxy's compiled policy, the files uploaded to its sandbox, and the record of the sandbox the launcher made |

## When something goes wrong

- **"cannot start"** lists everything missing at once: the web build, a file in `[openshell.environments]`, or a command.
- **A part that does not start** is shown with the end of its log; the whole log is in `logs/`. The worker refuses to start, for example, when the package cache in `[openshell]` is not the one in the targets file, or when another worker uses its state directory.
- **"already runs"**: another `up` uses the same state directory. Stop it with `down`, or give this config another `state_dir`.
- **"something else listens on …"**: another program holds an address in the config. The launcher checks each address before it starts the part, so a part never talks to something else that answers there.
- **The chat says the run service is not running**: look in `logs/run-service.log`. A message waits while the run service is stopped, and the next `up` delivers it.
