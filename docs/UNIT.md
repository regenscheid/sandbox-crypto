# The worker-side unit

The [worker](WORKER.md), the [policy middleware](POLICY-MIDDLEWARE.md) and the [package cache](PACKAGE-CACHE.md) are deployed as one unit with one configuration file in three sections (D101). Their code stays in three packages; only the deployment is one. `sandbox-science-unit` (in the launcher package) reads the file and runs the three services' own commands as one supervised group.

```toml
[unit]                               # optional
log_level = "info"
pass_env = { worker = ["AWS_REGION"] }

[worker]                             # the worker's settings, in place (docs/WORKER.md)
listen = "127.0.0.1:8310"
token_file = "/srv/ss/worker.token"
state_dir = "/srv/ss/worker/state"
# ...

[middleware]                         # the middleware's settings (docs/POLICY-MIDDLEWARE.md)
listen = "127.0.0.1:50061"
state_dir = "/srv/ss/middleware/state"
# ...

[cache]                              # optional: the cache's settings (docs/PACKAGE-CACHE.md)
listen = "127.0.0.1:8390"
cache_dir = "/srv/ss/cache"
# ...
```

```
sandbox-science-unit serve  --config unit.toml [--only worker|middleware|cache ...]
sandbox-science-unit check  --config unit.toml
sandbox-science-unit status --config unit.toml
```

## The file

- A section holds the service's settings exactly as its own settings file would, or only `settings_file = "path"` naming one (relative to the config file). The second form is how an existing deployment moves over without rewriting its files: the unit runs the service on that file as it is.
- Every section needs `listen = "host:port"` (an IPv4 address or a name): the unit waits for the service to take connections there, and its health is that they do. Two services may not share a port. `[worker]` and `[middleware]` are required; `[cache]` is optional, as the cache is in a [Kubernetes pool](KUBERNETES-BUNDLE.md).
- An inline section is written to `runtime_dir` (mode 600, removed when the unit stops) and the service runs on that file, so its relative paths are against `runtime_dir`. It is the config file's directory unless `[unit] runtime_dir` says otherwise. Use absolute paths.
- `[unit]` has `bin_dir` (where the services' commands are; beside the unit's Python by default), `runtime_dir`, `start_timeout` (120 s a service), `stop_timeout` (30 s), `log_level`, `restart` and `pass_env`. A service gets only `PATH`, `HOME`, the locale, proxy and CA variables, and what `pass_env` names for it from the unit's own environment. Never pass a secret that way.
- Unknown sections and keys in `[unit]` are refused.

Each service validates its own settings with its own model when it starts, since those models are in the services' packages and the unit imports none of them. The unit checks what it needs to run them (the sections, the addresses, the commands, the settings files), before it starts any, and a service that refuses its settings stops the unit with its own error.

## How it runs

One process supervises three children, with the launcher's `Supervisor` ([the launcher's parts](QUICKSTART.md)): a session of its own for each, a parent-death signal so none outlives the unit, readiness by address, and stop in reverse order. Hosting the three servers in one process would mean importing them, and sharing one process between a gRPC server and two asyncio ones, for nothing the supervisor does not already give.

- **Order.** Start: cache, middleware, worker, each once the one before takes connections. Stop: worker, middleware, cache, so the worker's last requests still have the other two.
- **Log.** One stream on the unit's output, each line under its service: `[cache] ...`, `[middleware] ...`, `[worker] ...`, and `[unit] ...` for the unit's own. systemd's journal and `kubectl logs` show it as it is.
- **Failure.** A service that stops by itself, or never becomes ready, stops the other two in order, and the unit exits 1 saying which and showing its last lines. systemd or the kubelet then starts the unit again, if they are told to. `[unit] restart = true` starts that service again instead, after 2 s doubling to a minute, and gives up after ten stops in a row.
- **Health.** `status` probes each address and exits 0 only if all answer. A launcher that did not start the unit uses the same check.
- **`--only`** runs one service of the same file. The Kubernetes pool uses it to keep one container for each, so each keeps its own credentials, limits and probes.

## Where it runs

- **babbage.** `scripts/personal_up.sh` starts it as the systemd unit `ss-personal-unit`, before the OpenShell gateway, which checks the middleware when it starts; the launcher then waits for it, uses its worker and watches its health. See [the personal instance](PERSONAL-BABBAGE.md).
- **A launcher config.** `[worker] unit = "unit.toml"` makes `sandbox-science up` start no worker of its own: it waits for the unit named, which must run the worker settings that `sandbox-science prepare` writes (`[worker] settings_file`), then uses and watches it, and never stops it. Start the unit first. Without `unit`, `up` starts the worker alone, as the [quickstart](QUICKSTART.md) describes.
- **Kubernetes.** The renderer makes a ConfigMap `<name>-pool-unit` with this file, each section naming the settings file the service's Secret already projects, and runs `sandbox-science-unit serve --config /etc/sandbox-science/unit.toml --only <service>` in each of the pool Pod's containers. The Pod is the unit: it is ready when all its containers are. One container for all three (`serve` without `--only`) would put the worker's token and TLS key where the cache, which fetches from the internet, could read them, so the bundle does not.

## Moving to it

Starting the three services as separate units or processes, each from its own settings file, is deprecated. It keeps working for one release: the `serve --settings` commands remain, as what the unit runs, and `settings_file` sections take the files you have. After that release the documentation describes only the unit. The labs' scripts (`.agents/labs/`) stay as they are.

A change to the unit's own options follows the [compatibility policy](COMPATIBILITY.md#the-worker-side-unit): a new option has a default that keeps today's behavior.
