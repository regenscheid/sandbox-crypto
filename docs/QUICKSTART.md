# Quickstart: Sandbox Science on one machine

`sandbox-science up` runs a local Sandbox Science from one config file:
- the [worker](WORKER.md), which runs research code in OpenShell sandboxes;
- the [run service](WORKSPACE.md#chat), which runs the main agent when you chat;
- the [workspace](WORKSPACE.md), the GUI, with its PDF viewer's origin.

Everything listens on loopback, so only your own computer can reach it. The steps below are for babbage.

## What you need

- **OpenShell**, with the policy middleware and the package cache. On babbage, `.agents/labs/lab_up.sh` brings up all three under `~/.local/state/openshell-lab`, and `lab_down.sh` removes them.
- **The research images**, built: `images/build.sh minimal`, and `images/build.sh crypto-base` if you want it ([images](IMAGES.md)). Each build prints the image's tag, for the config.
- **The repository's Python environment**: `uv sync` in the checkout. It installs the `sandbox-science` command with the parts' own commands.
- **The web app**, built: `cd web/workspace && npm ci --ignore-scripts && npm run build`.
- **A model gateway and its key**: an OpenAI-compatible gateway, such as LiteLLM, and a file holding its key that only you can read (mode 600). The launcher only passes the file's path to the run service: it never reads the key, and never prints it.

## The config

[`examples/sandbox-science.toml`](../examples/sandbox-science.toml) is a config for babbage's lab. Relative paths in it are against the file, and `~` is your home directory.

| Section | What it says |
| --- | --- |
| `state_dir` | Where the launcher keeps what it makes (see [the state directory](#the-state-directory)); `~/.local/state/sandbox-science` by default |
| `[[projects]]` | Each project: its `id`, the `profile` its environments start from, and the `sessions` to open for chat (`["main"]` by default) |
| `[model]` | The gateway's `url`, the main agent's model `name`, and `api_key_file`; an optional `reviewer` model is for reviews and the workspace's math repairs |
| `[worker]` | Its `listen` address, its `pool_id`, and the project `policy` granted to every project |
| `[workspace]` | Its `listen` address, its `preview_listen` address for the PDF viewer (by default the next port), and `static`, the web app's build |
| `[openshell]` | The worker's own `[openshell]` table ([worker settings](WORKER.md)): the OpenShell CLI and the `env` it needs to reach the gateway, the sandboxes' Python, the isolation tier, the package cache, and `[openshell.environments]` with the profiles' image tags |

`start_timeout` (300 s) and `stop_timeout` (30 s) bound how long each part may take to start and to stop. `bin_dir` says where the parts' commands are, by default beside the launcher's Python.

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
5. It makes each project in the control store, or moves an existing one to the policy, and opens its sessions.
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

The message starts a run of the main agent. The first run of a project has the worker create its environment, an OpenShell sandbox from the project's profile, which takes a minute or two. The panel shows the agent's turns and tool calls as they come, then its answer. [Chat](WORKSPACE.md#chat) has the details: budgets, cancelling and resuming.

## Stopping

Press Ctrl-C where `up` runs, or run `sandbox-science down --config …` from another terminal. The launcher stops the parts in reverse order: the workspace, the run service, then the worker.
- It gives each part `stop_timeout` to stop after SIGTERM, then kills it and whatever it started. A second Ctrl-C stops them at once.
- A part that stops by itself stops the others too, and `up` exits 1 with the end of that part's log.

**What stays running:** the worker leaves its sandboxes running when it stops, by design. Their kernels and files are still there for the next `up`, which reattaches to them ([sandbox daemon and recovery](WORKER.md#sandbox-daemon-and-recovery)).
- With `suspend_after` in `[openshell.environments]`, as in the example, the worker stops a sandbox nobody has used for that long, and starts it again on its next use.
- To remove the sandboxes, use the OpenShell CLI (`openshell sandbox list`, `openshell sandbox delete`).

## Status

```bash
uv run sandbox-science status --config examples/sandbox-science.toml
```

It shows each part's process, and whether its address answers. It exits 0 while Sandbox Science runs and 3 when it does not.

If the launcher itself was killed, its parts keep running. `status` lists them, and `down`, or the next `up`, stops them. The launcher knows them from the record it keeps in the state directory, so an `up` on the same state directory while one runs is refused.

## The state directory

Everything in it is only for your user: the directories are mode 700 and the files mode 600.

| Path | What it holds |
| --- | --- |
| `launcher.lock`, `launcher.json` | The lock `up` holds while it runs, and the record of the parts it started |
| `workspace-login` | The workspace's login address, while it runs |
| `tokens/` | The worker's own token and the run service's and the workspace's tokens for it, limited to the config's projects. They are made once and kept. Delete the directory, with Sandbox Science stopped, to have new ones made |
| `worker/worker.toml`, `worker/state/` | The worker's settings, written at each `up`, and its state directory. Do not delete the state directory while its sandboxes exist: it holds their endpoints |
| `control/` | The control store, with the projects, sessions, runs and documents, that the run service and the workspace share |
| `logs/` | Each part's output (`worker.log`, `run-service.log`, `workspace.log`), and `launcher.log` with the output of the commands `up` runs once |

## When something goes wrong

- **"cannot start"** lists everything missing at once: the web build, a file in `[openshell.environments]`, or a command.
- **A part that does not start** is shown with the end of its log; the whole log is in `logs/`. The worker refuses to start, for example, when the package cache in `[openshell]` is not the one in the targets file, or when another worker uses its state directory.
- **"already runs"**: another `up` uses the same state directory. Stop it with `down`, or give this config another `state_dir`.
- **The chat says the run service is not running**: look in `logs/run-service.log`. A message waits while the run service is stopped, and the next `up` delivers it.
